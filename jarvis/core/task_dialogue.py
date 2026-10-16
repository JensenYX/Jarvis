import re
from jarvis.core.schemas import Decision, TaskStatus

ACTIVE = {"QUEUED", "RUNNING", "NEEDS_INPUT"}


def bound_reference(text, memory):
    if re.search("昨天|上周|上次|上个月|yesterday|last week|last session", text, re.I):
        return None
    markers = re.findall(
        "第([一二三四五12345])(?:个|项)?任务|\\b(first|second|third|fourth|fifth)\\s+(?:task|one)\\b",
        text,
        re.I,
    )
    if len(markers) == 1:
        cn, en = markers[0]
        index = (
            int(cn)
            if cn.isdigit()
            else "一二三四五".index(cn) + 1
            if cn
            else ["first", "second", "third", "fourth", "fifth"].index(en.lower()) + 1
        )
        if not re.search("任务(?:是)?\\s*[:：]|new task|新任务", text, re.I):
            found = [
                r
                for r in memory["rows"]
                if r["task_id"] in memory["batch"] and r["request_index"] == index
            ]
            if len(found) == 1:
                return found[0]["task_id"]
    if re.search(
        "(?:还剩|还差|还有)(?:下)?一[个项](?:任务)?|(?:one|the) (?:task )?(?:left|remaining)|remaining task",
        text,
        re.I,
    ):
        pending = [
            r
            for r in memory["rows"]
            if r["task_id"] in memory["batch"]
            and r["status"] not in {"COMPLETED", "DELIVERED", "CANCELLED", "SUPERSEDED"}
        ]
        if len(pending) == 1:
            return pending[0]["task_id"]
    return None


def card(task, result=None):
    files = result.artifacts if result else []
    if not files:
        files = [
            a
            for row in task.observations
            if row.get("task_id") == task.task_id
            for a in row.get("artifacts", [])
        ]
    return {
        "task_id": task.task_id,
        "version": task.version,
        "objective": task.objective[:100],
        "status": task.status.value,
        "created_at": task.created_at,
        "request_group_id": task.request_group_id,
        "request_index": task.request_index,
        "request_count": task.request_count,
        "last_mentioned_at": task.last_mentioned_at or task.created_at,
        "origin_session_id": task.origin_session_id,
        "files": [a.get("title", a.get("filename", ""))[:60] for a in files],
        "file_names": [a.get("filename", "") for a in files],
        "delivery_hold_reason": task.delivery_hold_reason,
        "delivery_held_at": task.delivery_held_at,
        "progress": task.progress,
    }


def view(runtime):
    rows = {r["task_id"]: r for r in runtime.task_catalog}
    rows.update(
        {
            t.task_id: card(t, runtime.results.get((t.task_id, t.version)))
            for t in runtime.tasks.values()
        }
    )
    ordered = sorted(rows.values(), key=lambda r: r["created_at"])
    batches = [
        r
        for r in ordered
        if r.get("request_count", 1) > 1 and r.get("request_group_id")
    ]
    batch_id = batches[-1]["request_group_id"] if batches else None
    batch = sorted(
        [r for r in ordered if batch_id and r.get("request_group_id") == batch_id],
        key=lambda r: r["request_index"],
    )
    active = [
        r for r in ordered if r["task_id"] in runtime.tasks and r["status"] in ACTIVE
    ]
    focus = runtime.task_focus_id
    relevant = set(getattr(runtime, "matched_task_ids", []))
    projects = [r for r in ordered if r.get("files")]
    neighbors = []
    for i, r in enumerate(projects):
        if r["task_id"] in relevant or r["task_id"] == focus:
            neighbors.extend(projects[max(0, i - 2) : i + 1])
    candidates = []
    for row in (
        active
        + batch
        + [r for r in ordered if r["task_id"] == focus]
        + [r for r in ordered if r["task_id"] in relevant]
        + neighbors[::-1]
        + ordered[::-1]
    ):
        if row not in candidates:
            candidates.append(row)
    candidates = candidates[:12]
    if runtime.config["runtime"].get("task_interaction_v2"):
        current = [
            r for r in ordered if r.get("origin_session_id") == runtime.session_id
        ]
        query = next(
            (h["text"] for h in reversed(runtime.history) if h.get("role") == "user"),
            "",
        )
        historical = bool(
            re.search(
                "昨天|上周|上次|更早|之前|以前|yesterday|last session|previous|before|earlier",
                query,
                re.I,
            )
        )
        matched = [r for r in reversed(ordered) if r["task_id"] in relevant]
        if current and (not historical):
            candidates = list(reversed(current)) + matched[:2]
        else:
            candidates = (
                matched + active + list(reversed(current)) + list(reversed(ordered))
            )
        if re.search(
            "第[一二三四五12345]|first task|second task|remaining|还剩|还差|还有一个",
            query,
            re.I,
        ):
            candidates = batch + candidates
        if runtime.config["runtime"].get("targeted_task_recall"):
            spoken = runtime.dialogue_focus.get("task_ids", []) or [
                runtime.dialogue_focus.get("task_id")
            ]
            candidates = [r for r in ordered if r["task_id"] in spoken] + candidates
        candidates = list({r["task_id"]: r for r in candidates}.values())[:8]
    aliases = {f"T{i + 1}": r["task_id"] for i, r in enumerate(candidates)}
    labels = {identifier: key for key, identifier in aliases.items()}
    state_rows = []
    for row in candidates:
        state_rows.append(
            {
                "ref": labels[row["task_id"]],
                "request": row["objective"],
                "status": row["status"],
                "files": row.get("files", []),
                "created_at": row["created_at"],
                "chronological_order": ordered.index(row) + 1,
                "focused": row["task_id"] == focus,
                "current_session": row.get("origin_session_id") == runtime.session_id,
                "batch_item": row["request_index"] if row in batch else None,
            }
        )
    return {
        "rows": candidates,
        "aliases": aliases,
        "state": state_rows,
        "focus": focus,
        "batch": [r["task_id"] for r in batch],
        "active": [r["task_id"] for r in active],
        "recent": [r["task_id"] for r in ordered[-5:]][::-1],
        "current": [
            r["task_id"]
            for r in ordered
            if r.get("origin_session_id") == runtime.session_id
        ],
        "catalog": ordered,
    }


async def resolve(controller, transcript, epoch, context):
    memory = context["task_dialogue"]
    v2 = controller.config["runtime"].get("task_interaction_v2")
    targeted = controller.config["runtime"].get("targeted_task_recall")
    spoken = context.get("conversation_focus", {})
    spoken_ids = spoken.get("task_ids", [])
    if (
        targeted
        and spoken.get("purpose") == "recall"
        and spoken_ids
        and re.fullmatch(
            "\\s*(?:which one|which ones|what is it called|哪个|哪一个|哪几个|叫什么名字)[?？。.!！\\s]*",
            transcript.text,
            re.I,
        )
    ):
        return Decision(
            utterance_id=transcript.utterance_id,
            revision=transcript.revision,
            decision_epoch=epoch,
            route="CONTROL",
            control="TASK_MATCHES",
            confidence=1,
            raw={
                "task_ids": spoken_ids,
                "scope": "spoken_reference",
                "task_dialogue": True,
            },
        )
    state = {
        "input": transcript.text,
        "history": context.get("history", [])[-3:],
        "tasks": memory["state"],
        "focused_task": next(
            (k for k, v in memory["aliases"].items() if v == memory["focus"]), None
        ),
    }
    if controller.config["runtime"].get("conversation_v3"):
        state["conversation_focus"] = context.get("conversation_focus", {})
    held = (
        [
            r
            for r in memory.get("catalog", memory["rows"])
            if r.get("delivery_hold_reason") == "user_interrupted"
        ]
        if targeted
        else []
    )
    last_held = max(held, key=lambda r: r.get("delivery_held_at", 0)) if held else None
    if controller.config["runtime"].get("playback_control_v2"):
        interrupted = context.get("interrupted_playback", {})
        observed = next(
            (
                r
                for r in memory.get("catalog", memory["rows"])
                if r["task_id"] == interrupted.get("task_id")
                and r["version"] == interrupted.get("version")
                and r.get("delivery_hold_reason")
            ),
            None,
        )
        if observed:
            last_held = observed
    actions = {
        "RECALL": "Recall/list earlier requests or identify a saved project; includes 'what did I ask you to do?'.",
        "PROGRESS": "Report actual current execution status; includes 'what are you working on?' and 'how is it going?'.",
        "RESULT": "Read an existing task's completed answer; resume/repeat interrupted speech without redoing work.",
        "EXECUTE": "Do/retry/continue execution of unfinished work, or handle the remaining/forgotten batch item. Not replaying speech or a new goal.",
        "REFINE": "Change requirements of an existing task/file.",
        "REPLACE": "Replace the existing goal with a different goal.",
        "CANCEL": "Cancel existing background work.",
        "STOP_SPEAKING": "Stop speech only.",
        "NONE": "No task operation.",
    }
    if v2:
        actions.update(
            ANSWER="Answer the actual current everyday question. Repeating dinner advice is a request for an answer, not a task inventory.",
            NEW_TASK="A self-contained new lookup or artifact request, not an operation on an existing task.",
            HISTORY="Delete/clear saved conversation, task history or personal memory; never cancellation or inventory.",
        )
    if controller.config["runtime"].get("conversation_v3"):
        actions["ANSWER"] = (
            "Continue the conversation, explain the last answer, answer a follow-up or acknowledge a correction/personal fact. Includes but I am allergic to that, why, can you explain, and what restriction did you mention. No task/file operation."
        )
    targets = {
        "RECENT": "General history of recent requests, no single one specified.",
        "ACTIVE": "All currently queued/running tasks; generic progress question.",
        "BATCH": "All items of the most recent multi-task request.",
        "UNFINISHED": "The unfinished/failed items of that batch.",
        "NONE": "No matching referent.",
        **{
            k: f"Task {k} in tasks; use request, file title, chronology and status."
            for k in memory["aliases"]
        },
    }
    if v2:
        targets["CURRENT"] = (
            "The tasks requested in THIS conversation, all of them in request order."
        )
        if memory["current"] and (
            not re.search(
                "之前|以前|上次|昨天|previous|earlier|last session|yesterday",
                transcript.text,
                re.I,
            )
        ):
            targets.pop("RECENT", None)
            if not re.search(
                "第[一二三四五12345]|批|剩|还有|first|second|batch|remaining",
                transcript.text,
                re.I,
            ):
                targets.pop("BATCH", None)
                targets.pop("UNFINISHED", None)
        if not memory["active"]:
            targets.pop("ACTIVE", None)
    pending = [
        r["task_id"]
        for r in memory["rows"]
        if r["task_id"] in memory["batch"]
        and r["status"] not in {"COMPLETED", "DELIVERED", "CANCELLED", "SUPERSEDED"}
    ]
    for name, ids in [
        ("ACTIVE", memory["active"]),
        ("BATCH", memory["batch"]),
        ("UNFINISHED", pending),
        ("RECENT", memory["recent"]),
    ]:
        if len(ids) == 1:
            targets.pop(name, None)
    questions = {
        "action": {
            "instructions": "Identify the operation in input ONLY, not the operation in earlier history. 'What was the first task?' and a fragment naming an earlier task after a recall/clarification are RECALL. 'Do the first one' and 'you still owe me a task' are EXECUTE. Recalling prior work is supported even with multiple tasks. PROGRESS is distinct from reading a full result.",
            "criteria": actions,
        },
        "target": {
            "instructions": "Resolve the user's reference. batch_item is the ORIGINAL spoken number, never completion order. A remaining batch item refers to UNFINISHED. For game progress prefer the RUNNING game, not an old saved file. 'This game' uses focus. 'The game BEFORE X' selects a DIFFERENT game's title with the closest LOWER chronological_order than X; prefer the focused/latest X if its title recurs. General history/progress selects RECENT/ACTIVE, not NONE. Never invent an item.",
            "criteria": targets,
        },
    }
    analysis_id = context.get("battle_analysis_task_id")
    if analysis_id in memory["aliases"].values():
        analysis_ref = next(
            (k for k, v in memory["aliases"].items() if v == analysis_id)
        )
        state["current_battle_analysis"] = {
            "ref": analysis_ref,
            "meaning": "This tracked task explains the current Pokemon battle or an earlier recommendation. It is separate from playing moves.",
        }
        questions["action"]["instructions"] += (
            " A request to stop this analysis AND do another task INSTEAD is REPLACE, not NEW_TASK or CANCEL alone. An independent additional request that leaves this analysis running is NEW_TASK."
        )
        questions["target"]["instructions"] += (
            " References to 刚才那一手的分析 / current analysis mean current_battle_analysis.ref. A replacement targets the old analysis; the new weather/file objective is not an existing target."
        )
    binding = bound_reference(transcript.text, memory)
    group_binding = None
    if (
        v2
        and (not targeted)
        and re.search(
            "逐[项个]|一个一个|这几个任务|所有(?:的)?任务|各[个项]任务|all (?:of )?(?:the |these |my )?(?:tasks|results)|one by one",
            transcript.text,
            re.I,
        )
        and (
            not re.search(
                "之前|昨天|上次|previous|yesterday|last session", transcript.text, re.I
            )
        )
    ):
        group_binding = "CURRENT" if memory["current"] else "RECENT"
    if v2 and (not binding):
        names = re.findall("《([^》]{2,40})》", transcript.text)
        if len(names) == 1:
            exact = [r for r in memory["rows"] if names[0] in r["objective"]]
            live = [
                r
                for r in exact
                if next(
                    (
                        s
                        for s in memory["state"]
                        if memory["aliases"][s["ref"]] == r["task_id"]
                    )
                )["current_session"]
            ]
            if len(live) == 1:
                binding = live[0]["task_id"]
            elif exact and (
                not re.search(
                    "前一个|更早|上次|昨天|之前的那个|before|previous|earlier|last session|yesterday",
                    transcript.text,
                    re.I,
                )
            ):
                binding = max(exact, key=lambda r: r["created_at"])["task_id"]
    if binding:
        state["source_reference"] = next(
            (k for k, v in memory["aliases"].items() if v == binding)
        )
        questions.pop("target")
    elif group_binding:
        state["source_reference"] = group_binding
        questions.pop("target")
    if targeted and (not binding):
        questions["recall_scope"] = {
            "instructions": "If input refers to saved tasks, what scope is actually requested? Respect negation: games, not all tasks means SUBJECT. All games is also SUBJECT. Which one/name after identifying a project means FOCUS. Use input and conversation_focus.",
            "criteria": {
                "SUBJECT": "Only tasks matching a named subject/type, such as games, Python code, or a particular project.",
                "FOCUS": "A short follow-up about the object(s) in the last spoken answer, without a new subject.",
                "OVERVIEW": "Explicitly general task inventory, all results, a numbered batch, or general progress; no subject filter.",
                "NONE": "Not a reference to saved work.",
            },
        }
        if controller.config["runtime"].get("singular_result_recall"):
            questions["recall_scope"]["instructions"] += (
                " When the user identifies the latest/just-requested SINGLE task of a subject, use LATEST_MATCH, not the whole subject collection."
            )
            questions["recall_scope"]["criteria"]["LATEST_MATCH"] = (
                "The newest single matching task: 刚才北京新闻那个任务, the latest weather result. Not all games, several tasks, or an explicitly older task."
            )
    if last_held:
        state["interrupted_announcement"] = {
            "request": last_held["objective"],
            "files": last_held.get("files", []),
            "state": "The user interrupted this result; it is saved and not being read automatically.",
        }
        questions["interrupted_replay"] = {
            "instructions": "Does input explicitly ask to HEAR interrupted_announcement again or continue its speech? Resolve the subject from the supplied request. YES includes 把刚才被我打断的结果重新说一遍. NO for changing/rerunning work, another subject, complaints, negation, or a hypothetical replay.",
            "criteria": {
                "YES": "Read this saved interrupted announcement now.",
                "NO": "No explicit request to hear this interrupted announcement.",
            },
        }
    selected, confidence, _ = await controller.judge(
        "task_reference", state, questions, allow_fallback=False
    )
    if binding:
        selected["target"] = state["source_reference"]
        confidence["target"] = 1.0
    elif group_binding:
        selected["target"] = group_binding
        confidence["target"] = 1.0
    action, target = (selected["action"], selected["target"])
    score = min(confidence["action"], confidence["target"])
    base = dict(
        utterance_id=transcript.utterance_id,
        revision=transcript.revision,
        decision_epoch=epoch,
        route="CONTROL",
        confidence=score,
        raw={
            "choices": selected,
            "confidences": confidence,
            "task_dialogue": True,
            "source_bound_task": binding,
        },
    )
    if (
        last_held
        and selected.get("interrupted_replay") == "YES"
        and (
            confidence.get("interrupted_replay", 0)
            >= controller.config["jev"]["route_accept"]
        )
    ):
        base["confidence"] = confidence["interrupted_replay"]
        base["raw"].update(
            task_ids=[last_held["task_id"]], scope="interrupted_announcement"
        )
        return Decision(**base, control="RESULTS")
    if v2 and action == "HISTORY":
        from jarvis.core.history_control import resolve as resolve_history

        return await resolve_history(controller, transcript, epoch, context)
    if (
        v2
        and action in {"ANSWER", "NEW_TASK"}
        and (confidence["action"] >= controller.config["jev"]["route_accept"])
    ):
        base.update(
            route="FAST" if action == "ANSWER" else "WORKER",
            confidence=confidence["action"],
        )
        return Decision(**base)
    if (
        action == "STOP_SPEAKING"
        and confidence["action"] >= controller.config["jev"]["control_accept"]
    ):
        return Decision(**base, control=action)
    if target in memory["aliases"]:
        ids = [memory["aliases"][target]]
    elif target in {"ACTIVE", "BATCH", "RECENT", "CURRENT"}:
        ids = memory[target.lower()]
    elif target == "UNFINISHED":
        ids = pending
    else:
        ids = []
    mutation = action in {"EXECUTE", "REFINE", "REPLACE", "CANCEL"}
    threshold = controller.config["jev"][
        "control_accept" if mutation else "route_accept"
    ]
    read_reference = action in {"RECALL", "RESULT", "PROGRESS"} or (
        action in {"NONE", "ANSWER", "NEW_TASK"}
        and confidence["action"] < threshold
        and (selected.get("recall_scope") in {"SUBJECT", "FOCUS"})
    )
    if targeted and (not binding) and read_reference:
        scope = selected.get("recall_scope")
        general = scope == "OVERVIEW" and confidence.get("recall_scope", 0) >= threshold
        if general and target in {"CURRENT", "RECENT", "ACTIVE", "BATCH"}:
            confidence["target"] = max(confidence["target"], confidence["recall_scope"])
            score = min(confidence["action"], confidence["target"])
            base["confidence"] = score
        if not general:
            focused = context.get("conversation_focus", {})
            focus_ids = focused.get("task_ids", []) or (
                [focused["task_id"]] if focused.get("task_id") else []
            )
            if (
                scope == "FOCUS"
                and confidence.get("recall_scope", 0) >= threshold
                and focus_ids
            ):
                matches = [
                    (r, confidence["recall_scope"])
                    for r in memory["catalog"]
                    if r["task_id"] in focus_ids
                ]
            else:
                from jarvis.core.task_recall import matching_tasks

                matches = await matching_tasks(
                    controller, transcript.text, memory, context
                )
            if (
                controller.config["runtime"].get("singular_result_recall")
                and scope == "LATEST_MATCH"
                and (confidence.get("recall_scope", 0) >= threshold)
            ):
                matches = sorted(
                    matches, key=lambda pair: pair[0]["created_at"], reverse=True
                )[:1]
            ids = [r["task_id"] for r, _ in matches]
            base["raw"].update(
                task_ids=ids,
                scope="matching_subject",
                matches=[
                    {"task_id": r["task_id"], "probability": p} for r, p in matches
                ],
            )
            base["confidence"] = (
                min([confidence["action"]] + [p for _, p in matches]) if matches else 0
            )
            if not matches:
                return Decision(**base, control="TASK_NOT_FOUND")
            if action == "RESULT" and confidence["action"] >= threshold:
                return Decision(**base, control="RESULTS")
            if (
                action == "PROGRESS"
                and len(ids) == 1
                and (confidence["action"] >= threshold)
            ):
                return Decision(
                    **base,
                    control="PROGRESS",
                    target_task_id=ids[0],
                    target_version=matches[0][0]["version"],
                )
            return Decision(**base, control="TASK_MATCHES")
    if (
        v2
        and ids
        and (action in {"RESULT", "RECALL"})
        and (confidence["action"] < threshold)
        and (confidence["target"] >= threshold)
    ):
        choice, probability, _ = await controller.judge(
            "result_or_inventory",
            {"input": transcript.text},
            {
                "content": {
                    "instructions": "Does the user ask to hear the actual answers/results, or only recall/list which tasks were requested and their status? 逐项告诉我结果 and tell me each result request ANSWERS. What tasks did I ask for requests INVENTORY.",
                    "criteria": {
                        "ANSWERS": "Speak the substantive completed answers, one at a time if multiple.",
                        "INVENTORY": "Only identify the tasks/status, or no clear request for substantive answers.",
                    },
                }
            },
            allow_fallback=False,
        )
        if choice["content"] == "ANSWERS" and probability["content"] >= threshold:
            action = "RESULT"
            score = min(probability["content"], confidence["target"])
            base["confidence"] = score
            base["raw"]["result_content_verified"] = True
    if score < threshold or action == "NONE" or (not ids):
        if (
            targeted
            and (not mutation)
            and (
                not (
                    selected.get("recall_scope") == "OVERVIEW"
                    and confidence.get("recall_scope", 0) >= threshold
                )
            )
        ):
            base["raw"].update(task_ids=[], scope="unresolved_reference")
            return Decision(**base, control="TASK_NOT_FOUND")
        if v2 and action == "NONE":
            return Decision(**base, control="UNRESOLVED")
        if (
            mutation
            and confidence["action"] < threshold
            and (confidence["target"] >= threshold)
            and ids
        ):
            base["raw"].update(
                task_ids=ids, scope="identified_task_uncertain_operation"
            )
            return Decision(**base, control="TASK_SUMMARY")
        if action == "EXECUTE" and target == "UNFINISHED" and (not pending):
            base["raw"].update(task_ids=memory["batch"], scope="no_unfinished_items")
            return Decision(**base, control="TASK_SUMMARY")
        if v2 and action == "PROGRESS":
            ids = memory["current"] or memory["active"] or memory["recent"]
        elif action == "PROGRESS":
            ids = memory["active"] or memory["batch"] or memory["recent"]
        elif mutation:
            ids = memory["batch"] or memory["active"] or memory["recent"]
        else:
            ids = memory["recent"]
        base["raw"].update(task_ids=ids, scope="candidates" if mutation else "recent")
        return Decision(**base, control="TASK_CHOICES" if mutation else "TASK_SUMMARY")
    base["raw"].update(task_ids=ids, scope=target)
    if v2 and action == "RESULT":
        return Decision(**base, control="RESULTS")
    if len(ids) != 1 or target in {"RECENT", "ACTIVE", "BATCH", "CURRENT"}:
        if mutation:
            return Decision(**base, control="TASK_CHOICES")
        return Decision(**base, control="TASK_SUMMARY")
    row = next((r for r in memory["rows"] if r["task_id"] == ids[0]))
    return Decision(
        **base, control=action, target_task_id=ids[0], target_version=row["version"]
    )


def status_text(task, language="zh"):
    en = language == "en"
    if task.status == TaskStatus.QUEUED:
        return "queued" if en else "排队等待处理"
    if task.status == TaskStatus.RUNNING:
        phase = task.progress.get("phase")
        tool = task.progress.get("tool")
        names = {
            "get_weather": ("weather", "天气数据"),
            "search_news": ("news", "新闻来源"),
            "web_search": ("web search", "网页搜索"),
            "fetch_url": ("a page", "网页"),
            "read_file": ("the original file", "原文件"),
            "edit_file": ("the file changes", "文件修改"),
            "create_file": ("the new file", "新文件"),
        }
        label = names.get(tool, ("a tool", "工具"))[0 if en else 1]
        if phase == "tool_running":
            return f"checking {label}" if en else f"正在处理{label}"
        if phase == "tool_failed":
            return (
                f"the last {label} call failed; still working"
                if en
                else f"刚才{label}调用失败，仍在处理"
            )
        if task.progress.get("completed_tools", 0):
            return (
                "tool results received; preparing the answer"
                if en
                else "已取得工具结果，正在整理回答"
            )
        return (
            "generating the answer; no result yet" if en else "正在生成，还没有最终结果"
        )
    return (
        {
            TaskStatus.COMPLETED: "completed",
            TaskStatus.DELIVERED: "completed",
            TaskStatus.FAILED: "not completed; can retry",
            TaskStatus.NEEDS_INPUT: "waiting for your input",
            TaskStatus.CANCELLED: "cancelled",
            TaskStatus.SUPERSEDED: "replaced",
        }
        if en
        else {
            TaskStatus.COMPLETED: "已完成",
            TaskStatus.DELIVERED: "已完成",
            TaskStatus.FAILED: "未完成，可以重试",
            TaskStatus.NEEDS_INPUT: "等待你补充信息",
            TaskStatus.CANCELLED: "已取消",
            TaskStatus.SUPERSEDED: "已被新版本替代",
        }
    ).get(task.status, "unknown" if en else "状态未知")


def label(task, result=None):
    if result and result.artifacts:
        return result.artifacts[0].get("title", task.objective)[:45]
    return task.objective.split("\n")[0].strip()[:65]
