import asyncio
import json
from .survival_intent import context, brief

GOALS = {
    "FIND_WOOD",
    "FIND_STONE",
    "EXPLORE",
    "ASCEND",
    "SURVIVE",
    "EVADE",
    "ASHORE",
    "CRAFT",
    "PLACE_BENCH",
    "MATERIALS",
}


def state(mode, snapshot, request, task=None):
    game = context(snapshot)
    p = mode.building.project if mode.building else None
    project = (
        p.context(compact=True)
        if p and p.status not in {"CANCELLED", "COMPLETED"}
        else None
    )
    available = GOALS if project else GOALS - {"MATERIALS"}
    if not any(
        (
            r.get("rise", 0) > 0 and (r.get("supported") or r.get("blockers"))
            for r in game.get("navigation", [])
        )
    ):
        available = available - {"ASCEND"}
    return {
        "request": request,
        "game": game,
        "memory": mode.memory.context(),
        "current_task": brief(task),
        "construction_project": project,
        "local_recipe_facts": snapshot["view"].get("survival", {}).get("recipes", []),
        "native_material_recipes": snapshot["view"]
        .get("construction", {})
        .get("materials", []),
        "known_facilities": list(
            getattr(getattr(mode, "operations", None), "facilities", {}).values()
        )[-12:],
        "recent_completed_stages": (task or {}).get("completed_stages", [])[-12:],
        "environment": snapshot["view"].get("environment", {}),
        "available_goals": sorted(available),
    }


async def propose(mode, request, snapshot, task=None):
    if not mode.bridge.recipe_catalog:
        await mode.bridge.load_recipes()
    explicit = await requested_product(mode, request, snapshot)
    if explicit:
        return {
            "summary": "我先"
            + explicit["label"]
            + "，缺少的原料会先采集和加工，完成后再继续你的后续目标。",
            "steps": [explicit],
            "question": None,
            "review": {"explicit_product": True},
        }
    try:
        async with asyncio.timeout(12):
            return await propose_reasoned(mode, request, snapshot, task)
    except Exception as error:
        mode.r.emit("voxel.agenda.reasoning_unavailable", reason=type(error).__name__)
        return await propose_typed(mode, request, snapshot, task)


async def requested_product(mode, request, snapshot):
    requested = await mode.r.controller.judge_typed(
        "voxel_named_craft_presence",
        {"request": request},
        {
            "named": {
                "type": "noul",
                "instructions": "Does this current request explicitly ask to craft/make a named portable item or tool? A house is a structure, not a portable item. Merely play, gather, explore, or do not make a table is NO.",
            }
        },
        deadline_ms=1800,
    )
    if requested["named"]["noul"] < 0.6:
        return None
    groups = {}
    for row in mode.bridge.recipe_catalog.values():
        namespace = row["item_id"].split(":")[0]
        groups.setdefault(namespace, {})[row["item_id"]] = row["name"].split("\n")[0]
    categories = {k: ", ".join(list(v.values())[:3]) for k, v in groups.items()}
    has = await mode.r.controller.judge_typed(
        "voxel_requested_craft_category",
        {"current_request": request},
        {
            "category": {
                "type": "choice",
                "instructions": "Only if the CURRENT request explicitly asks to craft a named item, choose its native recipe category. Generic play/explore/build-a-house and negated crafting are NONE. The choices describe product families, not execution readiness.",
                "criteria": {"NONE": "No explicit named item to craft", **categories},
            }
        },
        deadline_ms=2200,
    )
    category = has["category"]["choice"]
    if category not in groups:
        return None
    products = groups[category]
    if len(products) > 48:
        entries = list(products.items())
        batches = {
            str(i // 40): dict(entries[i : i + 40]) for i in range(0, len(entries), 40)
        }
        batch = await mode.r.controller.judge_typed(
            "voxel_requested_craft_group",
            {"request": request},
            {
                "group": {
                    "type": "choice",
                    "instructions": "Choose the group containing the requested item; NONE if absent.",
                    "criteria": {
                        "NONE": "Not represented",
                        **{k: ", ".join(v.values()) for k, v in batches.items()},
                    },
                }
            },
            deadline_ms=2200,
        )
        products = batches.get(batch["group"]["choice"], {})
    a = await mode.r.controller.judge_typed(
        "voxel_agenda_requested_product",
        {"current_request": request, "inventory": context(snapshot)["inventory"]},
        {
            "product": {
                "type": "choice",
                "instructions": "Does the CURRENT user request explicitly require making one named tool/station before other autonomous play? Select that product only when the request says to make it, not when merely mentioning/forbidding it. “先做一把木镐，材料不足先采集，然后探索” must keep the wooden pick as the objective until inventory contains it. Generic takeover/exploration, negated crafting, or a product already satisfied in inventory -> NONE. Replanning suffixes do not erase the original explicit objective.",
                "criteria": {
                    "NONE": "No unfulfilled explicit crafting objective",
                    **products,
                },
            }
        },
        deadline_ms=2200,
    )
    key = a["product"]["choice"]
    if key not in products or a["product"]["confidence"] < 0.7:
        return None
    if context(snapshot)["inventory"].get(key, 0) > 0:
        return None
    return {
        "goal": "CRAFT",
        "item_id": key,
        "label": "制作" + products[key],
        "quantity": 1,
        "quantity_mode": "stock",
    }


async def propose_typed(mode, request, snapshot, task=None):
    data = state(mode, snapshot, request, task)
    game = data["game"]
    options = {}
    goals = [("EXPLORE", "探索周围地形"), ("FIND_WOOD", "收集一些木头")]
    if any(
        (
            r.get("rise", 0) > 0 and (r.get("supported") or r.get("blockers"))
            for r in game.get("navigation", [])
        )
    ):
        goals.append(("ASCEND", "爬上高处观察"))
    for goal, label in goals:
        options[goal] = {
            "goal": goal,
            "label": label,
            "quantity": 10 if goal == "EXPLORE" else 4 if goal == "FIND_WOOD" else None,
        }
    if any(("pick_" in i for i in game["inventory"])):
        options["FIND_STONE"] = {
            "goal": "FIND_STONE",
            "label": "采集一些石料",
            "quantity": 4,
        }
    if game["in_water"]:
        options["ASHORE"] = {"goal": "ASHORE", "label": "先上岸", "quantity": None}
    if any(
        (
            e.get("relation") == "hostile_species" and e.get("distance", 99) < 5
            for e in game["entities"]
        )
    ):
        options["EVADE"] = {
            "goal": "EVADE",
            "label": "先避开身边的敌人",
            "quantity": None,
        }
    if game["inventory"].get("mcl_crafting_table:crafting_table") and (
        not game["workbench"]
    ):
        options["PLACE_BENCH"] = {
            "goal": "PLACE_BENCH",
            "label": "放好已有工作台",
            "quantity": None,
        }
    for i, row in enumerate(snapshot["view"].get("survival", {}).get("recipes", [])):
        if row["item_id"].startswith(
            ("mcl_tools:", "mcl_crafting_table:", "mcl_torches:")
        ) or row["item_id"] in {
            "mcl_core:wood",
            "mcl_core:junglewood",
            "mcl_core:stick",
        }:
            if "crafting_table" in row["item_id"] and (
                game["workbench"] or data["known_facilities"]
            ):
                continue
            if row["item_id"].startswith("mcl_tools:") and game["inventory"].get(
                row["item_id"]
            ):
                continue
            options["C" + str(i)] = {
                "goal": "CRAFT",
                "label": "合成" + row["name"].split("\n")[0],
                "item_id": row["item_id"],
                "quantity": game["inventory"].get(row["item_id"], 0) + row["count"],
            }
    a = await mode.r.controller.judge_typed(
        "voxel_agenda_stage",
        {
            "request": request,
            "player": game["player"],
            "inventory": game["inventory"],
            "terrain": game["nodes"][:12],
            "navigation": game.get("navigation"),
            "memory": data["memory"],
            "recent_completed": data["recent_completed_stages"],
            "stalled_previous_goal": (task or {}).get("agent_goal")
            if (task or {}).get("stalled_s", 0) >= 12
            else None,
        },
        {
            "stage": {
                "type": "choice",
                "instructions": "Choose the next useful finite goal for this delegated session. Explicit exploration means explore, not crafting. Escaping a confined hole may need climbing/clearing. Use actual inventory, avoid repeating recent preparation or an already-failed goal. Resource quantities are desired stock, so do not gather if already sufficient. Choose crafted tools only for a useful next purpose. This is your gameplay plan, not user authorization.",
                "criteria": options,
            },
            "amount": {
                "type": "choice",
                "instructions": "If choosing a resource stage, choose a useful desired total stock for the next goal based on present inventory. If exploring, choose a useful new-ground distance. This is a modest next stage within the existing authorization, not a user-stated count.",
                "criteria": {
                    str(n): f"{n} stock units or blocks of exploration"
                    for n in (2, 4, 8, 12, 16)
                },
            },
        },
        deadline_ms=3000,
    )
    row = options[a["stage"]["choice"]]
    row["quantity_mode"] = (
        "stock" if row["goal"] in {"CRAFT", "FIND_WOOD", "FIND_STONE"} else "distance"
    )
    if row["goal"] in {"EXPLORE", "FIND_WOOD", "FIND_STONE"}:
        row["quantity"] = int(a["amount"]["choice"])
    return {
        "summary": "我先"
        + row["label"]
        + "，完成后根据新地形和材料继续安排；你随时可以接手。",
        "steps": [row],
        "question": None,
        "review": a,
    }


async def propose_reasoned(mode, request, snapshot, task=None):
    data = state(mode, snapshot, request, task)
    if not mode.bridge.recipe_catalog:
        await mode.bridge.load_recipes()
    catalog = list(mode.bridge.recipe_catalog.values())
    data["craft_products"] = {
        r["item_id"]: r["name"]
        for r in catalog
        if r["item_id"] in {x["item_id"] for x in data["native_material_recipes"]}
        or r["item_id"].startswith(
            ("mcl_tools:", "mcl_crafting_table:", "mcl_torches:")
        )
        or r["item_id"] == "mcl_core:stick"
    }
    reply = await mode.reasoning.complete(
        [
            {
                "role": "system",
                "content": "Plan this delegated VoxeLibre survival session. Return JSON {summary:string,steps:[{goal,label,item_id,quantity}],question:string|null}. Use 2-4 short steps for a general takeover, up to 6. summary is 1-2 concise Chinese sentences describing what you will do first and next. Goals only from available_goals. Each step label is short Chinese. quantity MUST be an integer 1..32 for FIND_WOOD/FIND_STONE/CRAFT/EXPLORE; null for other goals. Resource and craft quantities mean desired TOTAL stock after the stage (existing inventory counts). EXPLORE quantity means new terrain distance in blocks, not action count: choose 8 or more for a meaningful exploration. CRAFT item_id MUST be an exact craft_products key matching the label, never invent a Minecraft ID. Base priorities on inventory, visible terrain, recent task and explicit memory. A cancelled house must not be resumed. General takeover permits broad progress, not endless continuation of an old digging corridor. Use carried materials before gathering more. Limit gathering to useful amounts, then craft or explore. No needless workbench for planks. Choose gameplay for the actual surroundings: escaping a confined hole, climbing for a view, exploring terrain, finding varied useful resources, or crafting a tool for a concrete need. There is no mandatory workbench sequence. FIND_STONE requires an owned pickaxe or an explicit preceding crafting stage for a pickaxe. Avoid collecting a large pile while leaving the user waiting. Use current or known_facilities workbenches; do not manufacture replacements just because the camera moved. PLACE_BENCH is needed only when no usable placed table is known and a 3x3 craft is planned. The given known facilities are historical observations, not guaranteed still present. An explicit exploration request should only explore or climb, not craft. Do not invent recipe ingredients or coordinates. Player preferences/exclusions govern tactics. Missing style details for an explicitly requested structure may warrant ONE question; generic takeover lets you choose a sensible plan. Plans are future intent, not completed actions. Do not claim unreachable distant resources, guaranteed safety or unseen homes.",
            },
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        json_mode=True,
        max_tokens=1200,
        reasoning_effort="none",
        latency_sensitive=True,
    )
    choice = reply["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Incomplete gameplay plan")
    value = json.loads(choice["message"]["content"])
    mode.r.emit("voxel.agenda.proposed", proposal=value)
    steps = value.get("steps")
    if (
        not isinstance(steps, list)
        or not 1 <= len(steps) <= 6
        or (not isinstance(value.get("summary"), str))
    ):
        raise ValueError("Invalid gameplay agenda")
    for index, row in enumerate(steps):
        if row.get("goal") not in data["available_goals"] or not isinstance(
            row.get("label"), str
        ):
            raise ValueError("Unknown gameplay goal")
        row["quantity_mode"] = (
            "stock"
            if row["goal"] in {"FIND_WOOD", "FIND_STONE", "CRAFT"}
            else "distance"
        )
        if row.get("quantity") is not None and (
            type(row["quantity"]) is not int or not 1 <= row["quantity"] <= 32
        ):
            raise ValueError("Invalid plan amount")
        if (
            row["goal"] in {"FIND_WOOD", "FIND_STONE", "CRAFT", "EXPLORE"}
            and row.get("quantity") is None
        ):
            amount = await mode.r.controller.judge_typed(
                "voxel_agenda_amount",
                {
                    "request": request,
                    "step": row,
                    "next_steps": steps[index + 1 :],
                    "inventory": data["game"]["inventory"],
                    "native_recipes": data["native_material_recipes"],
                },
                {
                    "amount": {
                        "type": "choice",
                        "instructions": "Choose a modest finite amount for this delegated stage, enough to advance the next stage. This is your gameplay choice, not a claimed user quote. Craft units respect native recipe yields. Do not gather a huge pile before doing anything else.",
                        "criteria": {
                            str(i): f"{i} output units / movement steps"
                            for i in (1, 2, 3, 4, 6, 8, 12, 16)
                        },
                    }
                },
                deadline_ms=1300,
            )
            row["quantity"] = int(amount["amount"]["choice"])
        if row["goal"] == "CRAFT" and (not isinstance(row.get("item_id"), str)):
            raise ValueError("Craft product required")
        if row["goal"] == "CRAFT":
            options = {r["item_id"]: r["name"] for r in catalog}
            if row["item_id"] not in options:
                raise ValueError("Unknown craft product; use native catalog keys")
            outputs = [r["count"] for r in catalog if r["item_id"] == row["item_id"]]
            if outputs:
                import math

                batch = min(outputs)
                row["quantity"] = math.ceil(row["quantity"] / batch) * batch
        if row["goal"] == "MATERIALS" and (not data["construction_project"]):
            raise ValueError("No live construction project")
    tools = data["game"]["tools"]
    has_pick = any(("pick_" in str(r.get("item_id")) for r in tools)) or any(
        ("pick_" in i for i in data["game"]["inventory"])
    )
    for row in steps:
        if row["goal"] == "CRAFT" and "pick_" in row.get("item_id", ""):
            has_pick = True
        if row["goal"] == "FIND_STONE" and (not has_pick):
            raise ValueError("Stone harvesting needs a pickaxe preparation stage")
    bench = "mcl_crafting_table:crafting_table"
    owned = data["game"]["inventory"].get(bench, 0) > 0
    placed = bool(data["game"]["workbench"])
    expanded = []
    for row in steps:
        if row["goal"] == "CRAFT" and row.get("item_id") == bench:
            owned = True
        if row["goal"] == "PLACE_BENCH" and (not placed) and (not owned):
            if not any((r["item_id"] == bench for r in catalog)):
                raise ValueError("Workbench recipe unavailable")
            expanded.append(
                {
                    "goal": "CRAFT",
                    "item_id": bench,
                    "label": "制作所需工作台",
                    "quantity": 1,
                    "quantity_mode": "stock",
                }
            )
            owned = True
        if row["goal"] == "PLACE_BENCH":
            placed = True
            owned = False
        expanded.append(row)
    steps = expanded
    value["steps"] = steps
    judgment = await mode.r.controller.judge_typed(
        "voxel_agenda_review",
        {
            "request": request,
            "memory": data["memory"],
            "proposal": value,
            "inventory": data["game"]["inventory"],
        },
        {
            "faithful": {
                "type": "noul",
                "instructions": "Does the proposed agenda serve the current human request and respect explicit preferences and cancelled goals? Generic takeover permits sensible choices, but never revives an abandoned goal.",
            },
            "coherent": {
                "type": "noul",
                "instructions": "Do these bounded goals form a useful coherent progression, with gathering followed by use/exploration rather than repeating pointless collection? Judge plan intent, not physical execution readiness.",
            },
        },
        deadline_ms=1500,
    )
    if judgment["faithful"]["noul"] < 0.65 or judgment["coherent"]["noul"] < 0.6:
        raise ValueError("Gameplay plan needs revision")
    for row in steps:
        if row["goal"] == "CRAFT" and (
            not row["label"].startswith(("合成", "制作", "做", "加工"))
        ):
            row["label"] = "合成" + row["label"]
    summary = (
        "我先"
        + steps[0]["label"]
        + (
            "，接着" + "、".join((row["label"] for row in steps[1:3]))
            if len(steps) > 1
            else ""
        )
        + "。"
    )
    if len(steps) > 3:
        summary += "然后" + steps[3]["label"] + "；有变化会调整。"
    return {
        "summary": summary[:240],
        "steps": steps,
        "question": value.get("question"),
        "review": judgment,
    }


async def advice(mode, request, snapshot):
    known = (
        dict(getattr(mode.operations, "item_knowledge", {})) if mode.operations else {}
    )
    known.update(
        {
            r["item_id"]: r
            for r in snapshot["view"].get("survival", {}).get("item_help", [])
        }
    )
    docs = list(known.values())[-40:]
    if docs:
        options = {r["item_id"]: r["name"] for r in docs}
        relevant = await mode.r.controller.judge_typed(
            "voxel_advice_document",
            {
                "question": request,
                "task": brief(mode.operations.task) if mode.operations else None,
            },
            {
                "item": {
                    "type": "choice",
                    "instructions": "Does this question ask for practical use/care of one of these items/plants? Select the relevant registered documentation. For a general what-should-I-do or where-to-find-food question choose NONE.",
                    "criteria": {
                        "NONE": "Needs other live strategy or observations",
                        **options,
                    },
                }
            },
            deadline_ms=1800,
        )
        item = relevant["item"]["choice"]
        doc = next((r for r in docs if r["item_id"] == item), None)
        if (
            doc
            and relevant["item"]["confidence"] >= 0.65
            and (doc.get("usage") or doc.get("description"))
        ):
            return doc.get("usage") or doc["description"]
    try:
        async with asyncio.timeout(5):
            return await reasoned_advice(mode, request, snapshot)
    except Exception as error:
        mode.r.emit("voxel.advice.reasoning_unavailable", reason=type(error).__name__)
        game = context(snapshot)
        options = {}
        stage = (
            (mode.operations.task or {}).get("stage_goal") or {}
            if mode.operations
            else {}
        )
        if stage.get("label"):
            options["TASK"] = "可以接着" + stage["label"] + "，完成后再安排下一步。"
        for i, r in enumerate(game["travel"]):
            if r["kind"] == "JUMP_MOVE":
                options["M" + str(i)] = (
                    "可以先登上眼前这个台阶，看看高处有没有更合适的路线。"
                )
            elif r["kind"] == "MOVE":
                options["M" + str(i)] = (
                    "可以沿眼前能走的地面继续探索，观察新的地形和资源。"
                )
        for i, r in enumerate(game["nodes"]):
            if r.get("diggable") and any(
                (w in r["item_id"] for w in ("tree", "stone", "cobble"))
            ):
                options["R" + str(i)] = (
                    "可以先收集眼前的"
                    + r["name"].split("\n")[0]
                    + "，补充后续制作需要的材料。"
                )
        if game["workbench"]:
            options["BENCH"] = "附近已有工作台，可以打开看看现有材料能做什么工具。"
        if game["in_water"]:
            options["LAND"] = "先上到可见的岸边恢复呼吸，再安排采集或探索。"
        outcome = (
            (mode.operations.task or {}).get("outcome") if mode.operations else None
        )
        if outcome and (mode.operations.task or {}).get("status") not in {
            "COMPLETED",
            "CANCELLED",
        }:
            options["OUTCOME"] = (
                "当前目标是"
                + outcome["objective"].rstrip("。")
                + "。"
                + outcome["strategy"]
            )
        if not options:
            options["OBSERVE"] = "还需要观察附近的可走地面和资源，再确定具体下一步。"
        a = await mode.r.controller.judge_typed(
            "voxel_grounded_suggestion",
            {
                "question": request,
                "player": game["player"],
                "inventory": game["inventory"],
                "current_step": stage,
                "memory": mode.memory.context(),
            },
            {
                "suggestion": {
                    "type": "choice",
                    "instructions": "Choose one useful suggestion answering the current next-step question. Use present terrain/resources, prior goals and preferences. These are offered observed possibilities, not a compulsory order. Prefer a concrete new direction if the previous task is complete. Never claim an action was executed.",
                    "criteria": options,
                }
            },
            deadline_ms=2500,
        )
        return options[a["suggestion"]["choice"]]


async def reasoned_advice(mode, request, snapshot):
    data = state(
        mode, snapshot, request, mode.operations.task if mode.operations else None
    )
    reply = await mode.reasoning.complete(
        [
            {
                "role": "system",
                "content": "Answer this player in concise natural Chinese, at most 2 short sentences under 55 Chinese characters total. First give ONE concrete useful next action and why, then one sensible next goal or short relevant preference question. Do not call it the only option. Use current inventory, native recipe facts, active plan, remembered style/exclusions and cancelled goals. Do not resurrect cancelled tasks. Two logs can be processed into planks in personal crafting; a workbench is not needed for planks. State ingredients only from supplied recipes. Advice is for the human unless they ask for takeover; never claim an action executed. A moving camera does not invalidate an inventory-based suggestion. A remembered placed workbench must not be recommended as a new craft again merely because it is outside the current view. Consider exploration, vantage points, routes, food, tool upgrades or the user goal according to current terrain and inventory. Do not always funnel every question into crafting. Do not recite IDs, implementation, safety disclaimers, or generic look-around advice when carried materials offer useful progress. If facts really are missing, give the precise observation that would resolve it.",
            },
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        max_tokens=260,
        reasoning_effort="none",
        latency_sensitive=True,
    )
    c = reply["choices"][0]
    text = c["message"].get("content", "").strip()
    if c.get("finish_reason") != "stop" or not text or len(text) > 500:
        raise ValueError("Incomplete gameplay suggestion")
    return text
