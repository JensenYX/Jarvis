import asyncio
import copy
import json
from .blueprint import parse_blueprint

PROJECT_ROUTES = {
    "PROJECT_NEW": "Ask the COMPANION to build a new structure, e.g. 帮我造个房子/帮我搭面墙. Polite requests count. 我自己搭/我想自己建 describes human play and belongs to GOAL/ADVICE, never this route.",
    "PROJECT_CHANGE": "Answer the pending design question or revise this project, including short answers, corrections and ASR homophones.",
    "PROJECT_START": "Start/resume the current construction, also for a specified time (继续建一分钟/代建一分钟). Delegate remaining details and proceed. Use even without a blueprint if an existing project brief must be finished.",
    "PROJECT_STATUS": "Ask about an existing companion project progress, remembered requirements or design. If no project and human is building themselves, feedback belongs to ADVICE.",
}


def brief(project):
    if project is None:
        return None
    p = project.context(compact=True)
    p["requirements"] = {
        k: v.get("value") if isinstance(v, dict) else v
        for k, v in p["requirements"].items()
    }
    if p["pending_question"]:
        p["pending_question"] = {"question": p["pending_question"]["question"]}
    return {
        k: p[k]
        for k in (
            "title",
            "status",
            "requirements",
            "pending_question",
            "site",
            "blueprint",
            "completed_count",
            "execution_requested",
        )
    }


def intent_questions():
    return {
        "project_authorize": {
            "type": "noul",
            "instructions": "Does this human REQUEST the companion to DO the construction, rather than only give advice? 帮我建个小屋 is YES even if design details must first be discussed; missing details do not negate the request. Include start/resume requests; advice/capability questions, quoted/negated orders are NO. Judge user intent, not physical readiness.",
        },
        "delegate_design": {
            "type": "noul",
            "instructions": "Does the human explicitly let the companion decide unspecified design details (你决定/看着办)? Missing information alone is not delegation.",
        },
        "keep_original_request": {
            "type": "noul",
            "instructions": "Is this a design answer/revision that preserves the already-authorized building request? Advice-only or human takeover is NO. A stored plan without live authorization does not itself grant permission.",
        },
        "project_update": {
            "type": "noul",
            "instructions": "Does the current turn supply or CHANGE a concrete design requirement, including a short answer to the pending question? A request to start without new design details, a status question or general advice is NO.",
        },
        "project_hold": {
            "type": "noul",
            "instructions": "Does the human currently withhold execution (先看方案/先别动手/只讨论/我自己搭)? This can accompany design changes: preserve the changes while keeping control with the human. A future takeover or quoted negation is NO.",
        },
        "supply": {
            "type": "noul",
            "instructions": "Does this requested building job include normal gathering/crafting of needed materials? 帮我建个房子/你全权负责 implicitly includes preparing resources unless user says only use inventory or I will bring materials. 只出方案/别采集 is NO.",
        },
    }


def checked_requirements(value, project):
    if isinstance(value, list):
        if any((not isinstance(row, dict) for row in value)):
            raise ValueError("Invalid requirements")
        value = {
            str(row.get("key") or f"要求{i + 1}"): row for i, row in enumerate(value)
        }
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError("Invalid requirements")
    sources = {r["source_event_id"]: r["text"] for r in project.requests}
    existing = project.requirements
    result = {}
    for key, row in value.items():
        if not isinstance(key, str) or len(key) > 50 or (not isinstance(row, dict)):
            raise ValueError("Invalid requirement")
        if (
            not isinstance(row.get("value"), (str, int, float, bool))
            or len(str(row["value"])) > 180
        ):
            raise ValueError("Invalid requirement value")
        source = row.get("source_event_id")
        quote = row.get("quote")
        matching = [
            key
            for key, text in sources.items()
            if isinstance(quote, str) and quote and (quote in text)
        ]
        if source not in sources and len(matching) == 1:
            source = matching[0]
        if source is None:
            result[key] = {"value": row["value"], "source": "model_default"}
        elif (
            source in sources
            and isinstance(quote, str)
            and (0 < len(quote) <= 180)
            and (quote in sources[source])
        ):
            result[key] = {"value": quote, "source_event_id": source, "quote": quote}
        elif key in existing and all(
            (
                row.get(k) == existing[key].get(k)
                for k in ("value", "source_event_id", "quote")
            )
        ):
            result[key] = copy.deepcopy(existing[key])
        else:
            raise ValueError("Requirement has no exact user evidence")
    return result


async def intake(controller, project, delegate=False):
    state = {
        "user_requests": [r["text"] for r in project.requests[-8:]],
        "requirements": project.requirements,
        "delegated": delegate,
    }
    a = await controller.judge_typed(
        "voxel_design_intake",
        state,
        {
            "details": {
                "type": "noul",
                "instructions": "Do user_requests specify a concrete size, material/style, shape or delegate design details? A bare 帮我建个房子 only identifies house and has NO design details. 三乘三/木屋/两格高 or 你决定 provides detail. Ignore requests from outside this project.",
            },
            "next": {
                "type": "choice",
                "instructions": "Choose the useful NEXT design step using only user_requests and requirements of THIS project. Do not repeat answered questions. A house with no design details needs STYLE_SIZE. A specific wall/line or explicitly delegated design can be planned directly.",
                "criteria": {
                    "PLAN": "Enough information to draft, or remaining details are delegated",
                    "STYLE_SIZE": "No scale or style choice for an open-ended house request",
                    "CONFLICT": "Current explicit requirements contradict each other and need clarification",
                    "SITE": "User demands extension over cliff/void with no foundation; clarify a supported site or platform first",
                },
            },
        },
        deadline_ms=1800,
    )
    key = a["next"]["choice"]
    if delegate and key == "STYLE_SIZE":
        return None
    if not delegate and a["details"]["noul"] < 0.3 and (key == "PLAN"):
        key = "STYLE_SIZE"
    questions = {
        "STYLE_SIZE": "想建多大的房子、什么风格？也可以让我按现有材料来设计。",
        "CONFLICT": "这几个建造要求有冲突，你希望优先保留哪一个？",
        "SITE": "你说要从悬崖向外延展，需要先有支撑平台。是先搭平台，还是把房子建在旁边的实地上？",
    }
    return (
        {"key": key, "question": questions[key], "reason": "需要用户决定的建造要求"}
        if key in questions
        else None
    )


async def _propose(
    worker,
    controller,
    project,
    snapshot,
    materials,
    history,
    delegate=False,
    repair=None,
):
    from .coach import decision_game

    observed = snapshot["view"].get("survival", {}).get("nodes", [])
    raw_inventory = {
        r["item_id"]: r["count"] for r in snapshot["view"]["self"]["inventory_summary"]
    }
    available = {r["item_id"] for r in observed} | set(raw_inventory)
    native_materials = {
        r["item_id"]: r
        for r in snapshot["view"].get("construction", {}).get("materials", [])
    }
    palette_options = {}
    for key, name in materials.items():
        sources = [
            r
            for r in native_materials.get(key, {}).get("sources", [])
            if any((i in available for i in r.get("ingredients", [])))
        ]
        palette_options[key] = {
            "name": name,
            "owned": raw_inventory.get(key, 0),
            "recipe_from_observed_resources": sources,
        }
    floors = snapshot["view"].get("construction", {}).get("terrain", [])
    supported = {
        (r["position"]["x"], r["position"]["y"], r["position"]["z"]): r["clearance"]
        for r in floors
    }
    footprints = []
    for width in range(3, 7):
        for depth in range(3, 7):
            if any(
                (
                    all(
                        (
                            supported.get((x + dx, y, z + dz), 0) >= 3
                            for dx in range(width)
                            for dz in range(depth)
                        )
                    )
                    for x, y, z in supported
                )
            ):
                footprints.append([width, depth])
    messages = [
        {
            "role": "system",
            "content": "整理本次VoxeLibre建造要求，仅输出紧凑JSON {title,requirements,questions,geometry_request}，此步不画蓝图、不执行。requirements至多8个字段，每项{value,source_event_id,quote}。用户要求必须引用requests中真实的source_event_id，quote必须是该句原话的子串。只记录尺寸、材料、样式等真正设计要求；保留先前未被修改的要求和来源，最新改口覆盖旧要求。模型默认用source_event_id:null,quote:null。questions=[]；只有无法从全部requests解决的关键歧义才返回一个{key,question,reason}中文问题。用户说你决定就自行确定未指定细节，不重复问已回答的问题。geometry_request用一句简短英文描述最终几何需求（尺寸、墙高、屋顶、具体材料ID、门洞）；只含设计，不含来源ID或对话。选择owned或recipe_from_observed_resources有证据的材料。空背包且细节由你决定时，采用当地可采资源能完成的小型住所，单一材料即可，勿虚构用户指定木种或圆石。尺寸参考observed_supported_footprints；根据采集成本选择节省材料又能进入的方案。地面已存在，默认No floor。用户只说小房子且让你决定时，优先紧凑、少材料的可进入住所，避免把未指定的5x5或更大当成要求；找地和清理的时间也计入成本。",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "project": project.context(),
                    "game": decision_game(snapshot),
                    "allowed_materials": palette_options,
                    "observed_supported_footprints": footprints,
                    "delegate_unspecified_details": delegate,
                    "previous_attempt_error": repair,
                },
                ensure_ascii=False,
            ),
        },
    ]
    async with asyncio.timeout(18):
        response = await worker.complete(
            messages, json_mode=True, max_tokens=4500, reasoning_effort="none"
        )
    choice = response["choices"][0]
    worker.emit(
        "voxel.design.generated",
        finish_reason=choice.get("finish_reason"),
        usage=response.get("usage"),
    )
    if choice.get("finish_reason") != "stop":
        raise ValueError("Blueprint response incomplete")
    data = json.loads(choice["message"]["content"])
    if not isinstance(data, dict):
        raise ValueError("Design object required")
    worker.emit("voxel.design.proposed", proposal=data)
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        raise ValueError("Invalid questions")
    questions = [
        {k: str(q.get(k, ""))[:200] for k in ("key", "question", "reason")}
        for q in questions[:2]
        if isinstance(q, dict) and isinstance(q.get("question"), str)
    ]
    requirements = checked_requirements(data.get("requirements", {}), project)
    if not questions:
        request = "\n".join((r["text"] for r in project.requests[-8:]))
        normalized = str(data.get("geometry_request") or request)[:900]
        inventory = {
            r["item_id"]: r["count"]
            for r in snapshot["view"]["self"]["inventory_summary"]
            if r["item_id"] in materials
        }
        use = await controller.judge_typed(
            "voxel_structure_use",
            {"user_request": request},
            {
                "material": {
                    "type": "choice",
                    "instructions": "Select the primary block matching the user material. When species is unspecified prefer matching owned blocks, then blocks craftable via recipe_from_observed_resources. Nearby jungle logs produce jungle planks, not oak. Preserve explicitly named missing materials. Do not substitute dirt for wood.",
                    "criteria": palette_options,
                },
                "secondary": {
                    "type": "choice",
                    "instructions": "Select a second material ONLY when user explicitly requests two materials; NONE for a generic wooden house or unspecified decoration.",
                    "criteria": {
                        "NONE": "No second explicitly requested material",
                        **palette_options,
                    },
                },
                "preview": {
                    "type": "noul",
                    "instructions": "Does the user specifically ask to see a design/blueprint or discuss geometry even before obtaining materials? A bare request to build the house is NO; 先出方案/没材料也先给我看设计 is YES.",
                },
                "shelter": {
                    "type": "noul",
                    "instructions": "Is this a house/room with roof and an interior the player should enter, rather than only a wall, path, sculpture or platform?",
                },
            },
            deadline_ms=1500,
        )
        selected_materials = {use["material"]["choice"]}
        if use["secondary"]["choice"] != "NONE":
            selected_materials.add(use["secondary"]["choice"])
        missing = [
            materials[m].split("\n")[0]
            for m in sorted(selected_materials)
            if not inventory.get(m, 0)
        ]
        if (
            missing
            and use["preview"]["noul"] < 0.8
            and (not (project.authorization or {}).get("supply"))
        ):
            return {
                "title": str(data.get("title", "一起建造"))[:100],
                "requirements": requirements,
                "question": None,
                "plan": None,
                "usable": False,
                "review": use,
                "needs_materials": missing,
            }
        geometry_state = {
            "request": normalized,
            "materials_for_this_design": sorted(selected_materials),
        }
        previous = None
        for attempt in range(2):
            geometry_prompt = (
                geometry_state["request"]
                + "\nThe verified palette overrides any material defaults in that description. Use only these block IDs: "
                + ", ".join(sorted(selected_materials))
                + "."
            )
            if previous:
                geometry_prompt += "\nFix this invalid geometry: " + json.dumps(
                    previous, ensure_ascii=False
                )
            async with asyncio.timeout(18 if not previous else 24):
                reply = await worker.complete(
                    [
                        {
                            "role": "system",
                            "content": 'Return compact JSON {title,palette,size_xyz,boxes} in at most 16 boxes. ALL coordinates and size_xyz use the SAME AXIS ORDER [x,y,z] = [width,height,depth]. palette maps one-letter symbols to allowed block IDs only. Each box={min:[x,y,z],max:[x,y,z],material:symbol}; inclusive bounds. Every max[i] must be less than size_xyz[i]. Later material "." carves empty cells; do NOT put air in palette. Ground is below y=0. House needs two-block-high doorway connected to empty interior, roof above, no floor unless requested. No commentary.',
                        },
                        {"role": "user", "content": geometry_prompt},
                    ],
                    json_mode=True,
                    max_tokens=3000,
                    reasoning_effort="none" if not previous else "low",
                )
            choice = reply["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Geometry response incomplete")
            geometry = json.loads(choice["message"]["content"])
            worker.emit("voxel.design.geometry_proposed", blueprint=geometry)
            if (
                isinstance(geometry.get("size_xyz"), list)
                and len(geometry["size_xyz"]) == 3
            ):
                x, y, z = geometry.pop("size_xyz")
                geometry["size"] = [x, z, y]
            try:
                plan = parse_blueprint(geometry, selected_materials, project.site)
            except ValueError as error:
                if attempt:
                    raise
                previous = {
                    "blueprint": geometry,
                    "error": str(error),
                    "allowed_materials": sorted(selected_materials),
                }
                worker.emit("voxel.design.geometry_repair", reason=str(error))
                continue
            facts = plan.geometry_facts()
            if use["shelter"]["noul"] < 0.5 or (
                facts["roofed_reachable_interior_columns"] > 0
                and all(
                    (n >= 3 for n in facts["perimeter_blocks_lower_layers"].values())
                )
            ):
                break
            previous = {
                "blueprint": geometry,
                "facts": plan.geometry_facts(),
                "error": "No connected two-block-high doorway to a roofed interior. Correct the rows.",
            }
            worker.emit("voxel.design.geometry_repair", facts=plan.geometry_facts())
        else:
            raise ValueError(
                "House lacks a reachable roofed interior after geometry repair"
            )
        data["blueprint"] = geometry
        worker.emit("voxel.design.geometry", blueprint=geometry)
    plan = (
        parse_blueprint(data["blueprint"], materials, project.site)
        if data.get("blueprint")
        else None
    )
    state = {
        "requests": [
            {k: r[k] for k in ("text", "source_event_id")}
            for r in project.requests[-8:]
        ],
        "requirements": requirements,
        "questions": questions,
        "delegated": delegate,
    }
    if plan:
        state.update(
            dimensions={
                "width": plan.size[0],
                "depth": plan.size[1],
                "total_height": plan.size[2],
            },
            materials=[
                {"item_id": i, "name": materials[i]} for i in set(plan.palette.values())
            ],
            geometry={
                **plan.geometry_facts(),
                "layers": plan.layers,
                "palette": plan.palette,
            },
        )
    qs = {
        "faithful": {
            "type": "noul",
            "instructions": "Memory only: do requirements preserve user-stated requirements with exact quotes and the latest changes? Older raw quotes are historical and latest changes override them. Model defaults are separate. Ignore geometry here.",
        },
        "question": {
            "type": "choice",
            "instructions": "Which question is essential before this design? NONE if answered or delegated. Do not ask a fixed checklist.",
            "criteria": {
                "NONE": "No essential question",
                **{str(i): q["question"] for i, q in enumerate(questions)},
            },
        },
    }
    if plan:
        qs.update(
            {
                "shelter": {
                    "type": "noul",
                    "instructions": "Does the current human request require a habitable house/room with a roof and an interior a player can enter? A wall/line/platform/statue alone does not require an interior.",
                },
                "dimensions": {
                    "type": "noul",
                    "instructions": "Do dimensions agree with the latest explicit requested size/height? Unspecified dimensions may be chosen. Allow ASR homophones in this building context. Compare numeric dimensions only.",
                },
                "materials": {
                    "type": "noul",
                    "instructions": "Do named materials satisfy the latest user material choice? A request for wood without a species allows available wood/planks. Unspecified materials may use defaults. Interpret plausible ASR homophones such as 原石 for 圆石. Older material requests are superseded. Material only.",
                },
                "usable": {
                    "type": "noul",
                    "instructions": "Using calculated reachable interior/headroom and supplied geometry, is this structure usable for the requested purpose? House: roofed_reachable_interior_columns>0 proves a two-high entrance to covered interior. Code already checked that; do not negate it just from a short title. Walls/platforms do not need an interior.",
                },
                "contradiction": {
                    "type": "noul",
                    "instructions": "Does geometry violate any OTHER explicit requirement, e.g. no floor or a requested opening? YES only when contradicted. Ignore material/height already judged separately. A default for an unspecified detail is not a violation.",
                },
            }
        )
    judgment = await controller.judge_typed(
        "voxel_design_review", state, qs, deadline_ms=1800
    )
    if (
        plan
        and judgment.get("shelter", {}).get("noul", 0) >= 0.5
        and (plan.geometry_facts()["roofed_reachable_interior_columns"] == 0)
    ):
        raise ValueError(
            "House has no roofed interior with two air blocks reachable from an entrance. Fix the actual layer strings; do not fill the interior with a pillar or block the entry."
        )
    uncertain = [
        k
        for k in ("faithful", "dimensions", "materials", "usable")
        if k in judgment and judgment[k]["noul"] < 0.8
    ]
    if judgment.get("contradiction", {}).get("noul", 0) > 0.5:
        uncertain.append("contradiction")
    review = None
    if uncertain:
        async with asyncio.timeout(15):
            response = await worker.complete(
                [
                    {
                        "role": "system",
                        "content": "Verify only the listed uncertain_checks for this read-only building design. Return JSON {faithful:boolean,usable:boolean,reason:string}. Give a concise verdict, not a long geometric derivation. Use supplied calculated dimensions/openings; y=0 is above existing ground, two air cells mean headroom. Latest user request overrides old wording; defaults may fill unspecified details. A pending design question with no geometry is faithful but not yet usable. You cannot authorize or execute actions.",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"uncertain_checks": uncertain, **state}, ensure_ascii=False
                        ),
                    },
                ],
                json_mode=True,
                max_tokens=500,
                reasoning_effort="none",
            )
        choice = response["choices"][0]
        worker.emit(
            "voxel.design.review_generated",
            finish_reason=choice.get("finish_reason"),
            usage=response.get("usage"),
        )
        if choice.get("finish_reason") != "stop":
            raise ValueError("Design verification incomplete")
        review = json.loads(choice["message"]["content"])
        if (
            not isinstance(review, dict)
            or type(review.get("faithful")) is not bool
            or type(review.get("usable")) is not bool
        ):
            raise ValueError("Invalid design verification")
        worker.emit(
            "voxel.design.reviewed",
            review=review,
            jev=judgment,
            uncertain_checks=uncertain,
        )
        if not review["faithful"]:
            raise ValueError("Design does not match the player brief")
    index = judgment["question"]["choice"]
    question = (
        questions[int(index)]
        if index in {str(i) for i in range(len(questions))}
        else None
    )
    return {
        "title": str(data.get("title", "一起建造"))[:100],
        "requirements": requirements,
        "question": question,
        "plan": plan,
        "usable": review["usable"]
        if review
        else bool(plan and judgment["usable"]["noul"] >= 0.8),
        "review": judgment,
        "reasoning_review": review,
    }


async def propose(
    worker, controller, project, snapshot, materials, history, delegate=False
):
    return await compact_design(
        worker, controller, project, snapshot, materials, delegate
    )


async def compact_design(
    worker, controller, project, snapshot, materials, delegate=False
):
    from .survival_intent import context

    game = context(snapshot)
    requirements = dict(project.requirements)
    for i, r in enumerate(project.requests[-8:]):
        requirements[f"用户要求{i + 1}"] = {
            "value": r["text"][:180],
            "quote": r["text"][:180],
            "source_event_id": r["source_event_id"],
        }
    state = {
        "requests": [r["text"] for r in project.requests[-8:]],
        "requirements": project.requirements,
        "delegated_details": delegate,
        "inventory": game["inventory"],
        "nearby_materials": list({r["item_id"] for r in game["nodes"]}),
        "palette": materials,
        "prior_design": project.blueprint,
        "native_material_recipes": snapshot["view"]
        .get("construction", {})
        .get("materials", []),
    }
    prompt = "Design the structure requested by this VoxeLibre player. Return JSON {title,palette,layers,summary}. Use actual user requests, latest corrections override old ones. Unspecified details may be decided when delegated. palette maps one-letter symbols to exact offered block IDs. Each layer {y:integer,rows:[strings]} is a horizontal x-z slice: each row is z, each character is x. All rows in all layers must have the same width and depth. Dots are empty air and must not appear in palette. Specify each occupied y-level starting at y=0; identical repeated layers may use through_y. Coordinates are relative, not world positions. A house needs walls, a roof and an empty two-high entrance connected to an empty interior. Existing ground is below y=0; no floor unless requested. For an unspecified small house choose compact dimensions and few materials. Do not refuse because land is crowded: a separate stage finds or prepares land. Use carried/locally craftable material unless the user named another. Never invent user requirements. summary is a concise Chinese description of your proposed design. Prefer one carried material for an unspecified small shelter. A second absent decorative roof material adds unnecessary mining; do not choose it without a design reason or explicit request. Return compact rows, no prose outside JSON. Height includes the roof unless the user explicitly says WALL height. Total height N uses levels y=0..N-1; roof at N-1, never at N. A 2-block wall with a roof has layers y=0,1,2. Empty interior and door need dots on both y=0 and y=1; the roof covers the interior at y=2."
    error = None
    for attempt in range(2):
        kwargs = {"planning": True} if hasattr(worker, "retry_primary_at") else {}
        reply = await worker.complete(
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {**state, "repair": error}, ensure_ascii=False
                    ),
                },
            ],
            json_mode=True,
            max_tokens=2200,
            reasoning_effort="none",
            **kwargs,
        )
        c = reply["choices"][0]
        if c.get("finish_reason") != "stop":
            error = "Incomplete JSON. Return fewer boxes and no explanations."
            continue
        content = c["message"]["content"]
        try:
            geometry = json.loads(content)
        except json.JSONDecodeError as failure:
            worker.emit("voxel.design.invalid_json", error=str(failure)[:160])
            error = {
                "problem": "Invalid JSON: " + str(failure)[:140],
                "previous_output": content[:1600],
            }
            continue
        worker.emit("voxel.design.geometry_proposed", blueprint=geometry)
        if isinstance(geometry.get("palette"), dict):
            geometry["palette"] = {
                k: v for k, v in geometry["palette"].items() if k != "."
            }
        if "size_xyz" in geometry:
            x, y, z = geometry.pop("size_xyz")
            geometry["size"] = [x, z, y]
        try:
            plan = parse_blueprint(geometry, materials, project.site)
        except (KeyError, ValueError, TypeError) as failure:
            error = str(failure)[:180]
            continue
        facts = plan.geometry_facts()
        checks = await controller.judge_typed(
            "voxel_compact_design_review",
            {
                "requests": state["requests"],
                "dimensions": {
                    "width": plan.size[0],
                    "depth": plan.size[1],
                    "total_height": plan.size[2],
                },
                "materials": list(plan.palette.values()),
                "geometry": facts,
            },
            {
                "shelter": {
                    "type": "noul",
                    "instructions": "Does this request call for a house/room with habitable covered interior, rather than a wall/platform alone?",
                },
                "faithful": {
                    "type": "noul",
                    "instructions": "Does the design match the latest requested dimensions/material/style? A wall height2 plus roof means total_height3. Unspecified dimensions may be chosen. Judge the explicit user requirements, not whether land/materials are ready.",
                },
            },
            deadline_ms=2200,
        )
        if (
            checks["shelter"]["noul"] >= 0.5
            and facts["roofed_reachable_interior_columns"] == 0
        ):
            w, d, h = plan.size
            openings = {}
            for x in range(w):
                for z in range(d):
                    if (x in {0, w - 1}) != (z in {0, d - 1}):
                        openings[f"{x},{z}"] = {
                            "x": x,
                            "z": z,
                            "side": "west"
                            if x == 0
                            else "east"
                            if x == w - 1
                            else "front"
                            if z == 0
                            else "back",
                        }
            choice = await controller.judge_typed(
                "voxel_design_entrance_repair",
                {
                    "requests": state["requests"],
                    "dimensions": plan.size,
                    "layers": plan.layers,
                    "defect": facts,
                },
                {
                    "entrance": {
                        "type": "choice",
                        "instructions": "The drafted house has no usable two-high entrance. Choose a perimeter column for the doorway matching the player requirements; that column and its inside neighbor will be kept empty at y0 and y1. This is a geometry design choice, not an execution instruction. NONE if the requirements forbid an opening here.",
                        "criteria": {"NONE": "Need a different geometry", **openings},
                    }
                },
                deadline_ms=2200,
            )
            selected = openings.get(choice["entrance"]["choice"])
            if selected:
                x, z = (selected["x"], selected["z"])
                ix = 1 if x == 0 else w - 2 if x == w - 1 else x
                iz = 1 if z == 0 else d - 2 if z == d - 1 else z
                layers = []
                for y in range(h):
                    rows = [["."] * w for _ in range(d)]
                    for c in plan.cells:
                        if c["y"] == y:
                            rows[c["z"]][c["x"]] = next(
                                (
                                    k
                                    for k, v in plan.palette.items()
                                    if v == c["item_id"]
                                )
                            )
                    if y < 2:
                        rows[z][x] = "."
                        rows[iz][ix] = "."
                    layers.append({"y": y, "rows": ["".join(r) for r in rows]})
                geometry = {**geometry, "layers": layers}
                geometry.pop("boxes", None)
                plan = parse_blueprint(geometry, materials, project.site)
                facts = plan.geometry_facts()
                worker.emit(
                    "voxel.design.entrance_repaired",
                    selection=choice,
                    blueprint=plan.public(),
                )
            if facts["roofed_reachable_interior_columns"] == 0:
                error = {
                    "problem": "A house needs empty covered interior and a connected two-high entrance",
                    "geometry": geometry,
                }
                continue
        if checks["faithful"]["noul"] < 0.75:
            if checks["faithful"]["noul"] < 0.35:
                error = {
                    "problem": "The design does not match explicit requested dimensions/material/style. Correct the mismatch.",
                    "requests": state["requests"],
                    "calculated_dimensions": {
                        "width": plan.size[0],
                        "depth": plan.size[1],
                        "height_including_roof": plan.size[2],
                    },
                    "previous_design": geometry,
                }
                continue
            from .judgment import choose

            v = await choose(
                worker,
                "Verify only explicit requirements against calculated geometry; defaults for unspecified choices are allowed.",
                {
                    "requests": state["requests"],
                    "design": plan.public(),
                    "facts": facts,
                },
                {
                    "MATCH": "Matches requested structure",
                    "REVISE": "Violates explicit requirements",
                },
            )
            if v["choice"] == "REVISE":
                error = v["reason"]
                continue
        return {
            "title": str(geometry.get("title", "建造方案"))[:100],
            "requirements": requirements,
            "question": None,
            "plan": plan,
            "usable": True,
            "review": checks,
        }
    raise ValueError(
        "Design could not satisfy the requested geometry: " + str(error)[:180]
    )


async def legacy_propose(
    worker, controller, project, snapshot, materials, history, delegate=False
):
    error = None
    for attempt in range(2):
        try:
            result = await _propose(
                worker,
                controller,
                project,
                snapshot,
                materials,
                history,
                delegate,
                error,
            )
            if result["plan"] and (not result["question"]) and (not result["usable"]):
                raise ValueError(
                    "Geometry is unusable: check connected entrance and two-block interior headroom; preserve explicit dimensions"
                )
            return result
        except ValueError as failure:
            if attempt:
                raise
            error = str(failure)[:240]
            worker.emit("voxel.design.repair", reason=error)
