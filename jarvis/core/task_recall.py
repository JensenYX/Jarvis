import re
from jarvis.core.tools import terms


def retrieval_terms(text):
    stop = set("的我你吗得之还个这那是了要把给帮能可不么一") | set(
        "i we our it its this that they could would can please you me my the a an to for of and with about what how now today want help tell give think remember previous previously earlier which have has had did do designed made tasks task all not 之前 现在 一下 一个 这个 那个 任务 帮我 给我 你能 能不能 可以 什么 怎么 告诉".split()
    )
    tokens = terms(text) - stop
    for marker, pattern in {
        "kind:game": "\\bgames?\\b|\\bsnake\\b|游戏|贪吃蛇",
        "kind:code": "\\bcode\\b|\\bprograms?\\b|代码|程序|\\.py\\b",
        "kind:python": "\\bpython\\b|\\.py\\b",
        "kind:weather": "\\bweather\\b|天气|气温",
        "kind:news": "\\bnews\\b|新闻",
    }.items():
        if re.search(pattern, text, re.I):
            tokens.add(marker)
    return tokens


def candidates(query, rows, limit=12):
    wanted = retrieval_terms(query)

    def rank(row):
        text = (
            row["objective"]
            + " "
            + " ".join(row.get("files", []) + row.get("file_names", []))
        )
        overlap = wanted & retrieval_terms(text)
        return (
            sum((4 if token.startswith("kind:") else 1 for token in overlap)),
            row["created_at"],
        )

    return sorted(rows, key=rank, reverse=True)[:limit]


async def matching_tasks(controller, query, memory, context):
    rows = candidates(query, memory.get("catalog", memory["rows"]))
    threshold = controller.config["jev"].get("task_match_probability", 0.8)

    async def judge(batch):
        state = {
            "input": query,
            "collection": "These are saved work records for the current user. Find records relevant to the lookup, using the produced files as evidence.",
        }
        ordered_projects = [
            r for r in memory.get("catalog", memory["rows"]) if r.get("files")
        ]
        positions = {r["task_id"]: i for i, r in enumerate(ordered_projects)}
        relative = bool(re.search("\\bbefore\\b|前一个|之前的那个|更早的", query, re.I))
        if relative:
            relevant_positions = {
                positions[r["task_id"]] for r in rows if r["task_id"] in positions
            }
            state["project_order"] = [
                {"order": i, "files": r["files"], "file_names": r.get("file_names", [])}
                for i, r in enumerate(ordered_projects)
                if i in relevant_positions
            ]
        questions = {
            f"T{i}": {
                "instructions": {
                    "question": "Is this record relevant to the specific subject the user wants to recall in input? Judge independently from the work and produced files, across languages. Ignore request boilerplate and respect exclusions. File extensions identify programming languages. Several saved records may qualify."
                    + (
                        " Resolve before/previous against project_order: select the preceding project, not the named anchor itself."
                        if relative
                        else ""
                    ),
                    "candidate": {
                        "request": row["objective"],
                        "files": row.get("files", []),
                        "file_names": row.get("file_names", []),
                        "status": row["status"],
                        **(
                            {"order": positions.get(row["task_id"])} if relative else {}
                        ),
                    },
                },
                "criteria": {
                    "true": "The saved work or file matches the requested subject or type.",
                    "false": "Unrelated subject or no evidence of the named work.",
                },
            }
            for i, row in enumerate(batch)
        }
        from jarvis.core.jev import estimate_tokens

        if (
            len(batch) > 1
            and estimate_tokens({"state": state, "questions": questions}) + 64
            > controller.config["jev"]["max_context_tokens"]
        ):
            middle = len(batch) // 2
            return await judge(batch[:middle]) + await judge(batch[middle:])
        scores = await controller.relevance("task_recall_match", state, questions)
        return [
            (row, scores[f"T{i}"])
            for i, row in enumerate(batch)
            if scores[f"T{i}"] >= threshold
        ]

    matches = await judge(rows) if rows else []
    return sorted(matches, key=lambda item: item[0]["created_at"], reverse=True)


def file_label(artifact, language):
    title = artifact.get("title") or artifact.get("filename", "file")
    if language == "en" and re.search("[\\u3400-\\u9fff]", title):
        filename = artifact.get("filename", "")
        if filename:
            stem, _, suffix = filename.rpartition(".")
            title = (stem or filename).replace("_", " ").replace("-", " ")
            title = re.sub("(?i)\\bquicksort\\b", "quick sort", title)
    return title[:80]
