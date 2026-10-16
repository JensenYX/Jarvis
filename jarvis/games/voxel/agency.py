import math
from .survival_intent import context, brief


async def objective(
    controller, text, snapshot, previous=None, project=None, kind=None, reasoning=None
):
    game = context(snapshot)
    if previous and previous.get("status") in {"CANCELLED", "COMPLETED"}:
        previous = None
    if project and project.status in {"CANCELLED", "COMPLETED"}:
        project = None
    q = {
        "goal": {
            "type": "choice",
            "instructions": "Resolve current requested goal from this utterance, active task and construction question. Help me find needed materials refers to the project. A generic take over should preserve an unfinished goal if one exists. Do not replace explicit moving/swimming commands with old mining.",
            "criteria": {
                "SURVIVE": "General autonomous gameplay without a specific exploration or resource goal",
                "GATHER": "Collect useful materials, user did not name a particular resource",
                "FIND_WOOD": "Find trees and collect wood",
                "FIND_STONE": "Collect stone/cobblestone with a harvesting tool",
                "MATERIALS": "Obtain the materials for the current construction and resume it",
                "FORWARD": "Move forward along current direction",
                "ASCEND": "Reach higher ground or a hilltop, climb progressively",
                "SURFACE": "Swim upward until breathing air",
                "ASHORE": "Leave the water for solid land",
                "EVADE": "Avoid/retreat from current attacker",
                "EXPLORE": "Explore this area freely; preserve this exploration goal without unrelated crafting",
                "CONTINUE": "Continue previous overall goal",
            },
        },
        "autonomy": {
            "type": "noul",
            "instructions": "Does the user grant continuing gameplay choices across movement, finding resources, crafting and responding to danger? 替我玩几分钟/代打/帮我建房并找材料 is YES. A single move/jump/surface is a bounded goal, NO.",
        },
        "distance": {
            "type": "choice",
            "instructions": "Resolve goal extent. 爬到山顶 requires DESTINATION with multiple supported climbs; one successful jump is insufficient. 跳上这一格/上这个台阶 is ONE. Explore this area means DESTINATION with meaningful new ground. This is not duration.",
            "criteria": {
                "ONE": "Explicitly one step/one ledge",
                "SHORT": "A short forward advance, no exact destination",
                "DESTINATION": "A destination, hilltop or surrounding area, requiring sustained progress",
            },
        },
        "alter_terrain": {
            "type": "noul",
            "instructions": "Does the requested navigation include making a way when necessary? Climb to a summit/开路上去/搭台阶/遇到障碍自己想办法 implies local obstacle clearing or placing carried blocks. Explicit 不要挖/只走/别改地形 means NO. Merely surfacing or one jump means NO.",
        },
    }
    state = {
        "request": text,
        "previous_task": brief(previous),
        "project": {
            "title": project.title,
            "requirements": project.requirements,
            "question": project.question,
        }
        if project
        else None,
    }
    if not project:
        q["goal"]["criteria"].pop("MATERIALS")
    if not previous or previous.get("agent_goal") not in q["goal"]["criteria"]:
        q["goal"]["criteria"].pop("CONTINUE")
    if kind == "TRAVEL":
        q["goal"]["criteria"] = {
            k: v
            for k, v in q["goal"]["criteria"].items()
            if k
            in {
                "FORWARD",
                "ASCEND",
                "SURFACE",
                "ASHORE",
                "EVADE",
                "EXPLORE",
                "CONTINUE",
            }
        }
        state.pop("project", None)
    a = await controller.judge_typed(
        "voxel_agent_objective", state, q, deadline_ms=2500
    )
    goal = a["goal"]["choice"]
    if goal == "CONTINUE":
        goal = (previous or {}).get("agent_goal") or "SURVIVE"
    terrain = a.get("alter_terrain", {}).get("noul", 0)
    terrain_allowed = terrain >= 0.75
    if goal in {"ASCEND", "EXPLORE", "FORWARD"}:
        means = await controller.judge_typed(
            "voxel_navigation_means",
            {"current_request": text, "goal": goal},
            {
                "restriction": {
                    "type": "noul",
                    "instructions": "Does this CURRENT request explicitly forbid changing terrain or limit you to walking on an existing path? YES for do not dig/place/alter terrain/only walk. Merely climb the hill, take me up or explore is NO: it delegates an outcome and does not restrict normal means. Judge whether a restriction was stated, not whether the human named each intermediate action.",
                }
            },
            deadline_ms=2000,
        )
        a["means"] = means
        restricted = means["restriction"]["noul"]
        terrain_allowed = restricted <= 0.2
        if 0.2 < restricted < 0.8 and reasoning:
            from .judgment import choose

            reviewed = await choose(
                reasoning,
                "Resolve permitted means only; explicit restrictions override implied steps.",
                {"request": text, "goal": goal},
                {
                    "LOCAL_WORK": "Navigate with necessary small local obstacle/step changes",
                    "WALK_ONLY": "No digging/placing",
                    "DISCUSS": "No execution requested",
                },
            )
            a["means_review"] = reviewed
            terrain_allowed = reviewed["choice"] == "LOCAL_WORK"
    return {
        "agent_goal": goal,
        "autonomy": a["autonomy"]["noul"] >= 0.8,
        "travel_distance": 1 if a["distance"]["choice"] == "ONE" else 3,
        "extent": a["distance"]["choice"],
        "navigation_authorized": terrain_allowed,
        "judgment": a,
        "completion_request": text,
    }


async def select_step(
    controller, snapshot, task, actions, recent, visited, targets=(), memory=()
):
    game = context(snapshot)
    player = game["player"]
    pos = player["position"]
    yaw = task.get("start_yaw", player.get("yaw") or 0)
    target_rows = [r for r in game["nodes"] if r["item_id"] in targets]
    remembered = [r for r in memory if r.get("item_id") in targets]
    options = {}
    view_key = f"{round(pos['x'] / 3)},{round(pos['z'] / 3)}"
    viewed = task.get("search_views", {}).get(view_key, [])
    for i, a in enumerate(actions):
        row = {k: v for k, v in a.items() if k not in ("action_id", "target_index")}
        target = a.get("target")
        if target:
            row["distance_from_start"] = round(
                sum(((target[k] - task["start_position"][k]) ** 2 for k in ("x", "z")))
                ** 0.5,
                2,
            )
            row["forward_progress"] = round(
                -(target["x"] - pos["x"]) * math.sin(yaw)
                + (target["z"] - pos["z"]) * math.cos(yaw),
                2,
            )
            from .locomotion import cell

            row["visited"] = visited.get(cell(target), 0)
            if target_rows:
                row["distance_to_wanted_resource"] = round(
                    min(
                        (
                            sum(
                                (
                                    (target[k] - r["position"][k]) ** 2
                                    for k in ("x", "y", "z")
                                )
                            )
                            ** 0.5
                            for r in target_rows
                        )
                    ),
                    2,
                )
            elif remembered:
                row["distance_to_last_seen_resource"] = round(
                    min(
                        (
                            sum(
                                (
                                    (target[k] - r["position"][k]) ** 2
                                    for k in ("x", "y", "z")
                                )
                            )
                            ** 0.5
                            for r in remembered
                        )
                    ),
                    2,
                )
            if task.get("collect_positions"):
                row["distance_to_uncollected_drop"] = round(
                    min(
                        (
                            sum(((target[k] - r[k]) ** 2 for k in ("x", "z"))) ** 0.5
                            for r in task["collect_positions"]
                        )
                    ),
                    2,
                )
            if task.get("return_site"):
                row["distance_to_build_site"] = round(
                    sum(((target[k] - task["return_site"][k]) ** 2 for k in ("x", "z")))
                    ** 0.5,
                    2,
                )
            if game["shore"]:
                row["distance_to_visible_shore"] = round(
                    min(
                        (
                            math.hypot(
                                target["x"] - r["position"]["x"],
                                target["z"] - r["position"]["z"],
                            )
                            for r in game["shore"]
                        )
                    ),
                    2,
                )
        if a["kind"] == "DIG":
            row["wanted_resource"] = a.get("item_id") in targets
            row["harvest_note"] = (
                "breaks but yields NO resource with current tool"
                if a.get("harvest") is False
                else "normal harvest available"
            )
            row["floor_below_player"] = a["target"]["y"] < pos["y"]
        if a["kind"] == "SWIM_UP":
            row["meaning"] = (
                "Hold jump to swim upward now; keep doing it until head is in air."
            )
        if a["kind"] == "JUMP_MOVE":
            row["meaning"] = (
                "Jump and step onto a one-block ledge; actually raises position."
            )
        if a["kind"] == "LOOK":
            sector = round(a.get("yaw", 0) % (2 * math.pi) / (math.pi / 2)) % 4
            row["already_observed_here"] = sector in viewed
            row["meaning"] = (
                "Turn the camera to observe another direction; visible resources are only in the current view."
            )
        options[str(i)] = row
    state = {
        "request": task["request"],
        "goal": task.get("agent_goal"),
        "constraints": task.get("constraints", []),
        "continuation_strategy": task.get("recovery"),
        "player": player,
        "in_water": game["in_water"],
        "dry_land": game["dry_land"],
        "visible_shore": game["shore"],
        "inventory": game["inventory"],
        "visible_resources": target_rows[:8],
        "wanted_item_ids": list(targets),
        "resource_memory": list(memory)[-6:],
        "construction_materials_needed": task.get("materials_needed"),
        "workbench": game.get("workbench"),
        "current_plan_step": task.get("stage_goal"),
        "enemies": game["entities"],
        "recent_actions": list(recent)[-4:],
    }
    if task.get("agent_goal") in {
        "SURFACE",
        "ASHORE",
        "FORWARD",
        "EVADE",
        "RETURN_SITE",
    }:
        options = {
            k: r
            for k, r in options.items()
            if r["kind"] in {"MOVE", "JUMP_MOVE", "SWIM", "SWIM_UP", "JUMP", "LOOK"}
        }
        state.pop("resource_memory", None)
        state.pop("visible_resources", None)
    if task.get("agent_goal") in {"ASCEND", "EXPLORE"}:
        options = {
            k: r
            for k, r in options.items()
            if r["kind"] not in {"CRAFT", "DIG", "PLACE", "ATTACK"}
        }
    from .locomotion import choose_route, route_actions

    resource_ready = any(
        (a["kind"] == "DIG" and a.get("item_id") in targets for a in actions)
    ) or bool(task.get("collect_positions"))
    if task.get("agent_goal") == "MATERIALS":
        resource_ready = resource_ready or any(
            (
                a["kind"] == "CRAFT"
                and a.get("item_id") in (task.get("materials_needed") or {})
                for a in actions
            )
        )
    safe = player.get("head_medium") != "drowning_medium" and (
        not any(
            (
                e.get("relation") == "hostile_species" and e.get("distance", 99) < 4
                for e in game["entities"]
            )
        )
    )
    if safe and (
        task.get("agent_goal") in {"EXPLORE", "ASCEND", "FORWARD", "RETURN_SITE"}
        or (
            task.get("agent_goal") in {"FIND_WOOD", "FIND_STONE", "MATERIALS"}
            and (not resource_ready)
        )
    ):
        route = await choose_route(
            controller, game, task, actions, visited, targets, memory
        )
        viable = route_actions(route, actions)
        if viable:
            choices = {
                str(i): {
                    k: v for k, v in a.items() if k not in {"action_id", "target_index"}
                }
                for i, a in enumerate(actions)
                if a in viable
            }
            answer = await controller.judge_typed(
                "voxel_navigation_action",
                {
                    "request": task["request"],
                    "goal": task.get("agent_goal"),
                    "waypoint": route,
                    "current_position": pos,
                    "held": player.get("wielded"),
                    "recent_actions": list(recent)[-4:],
                    "continuation_strategy": task.get("recovery"),
                },
                {
                    "action": {
                        "type": "choice",
                        "instructions": "Advance this chosen adjacent waypoint. If available MOVE/JUMP_MOVE reaches it, do that now. Otherwise remove its exposed blockers one by one, select a faster appropriate tool if available, or place the offered step support. Unknown space is not air; every effect is reobserved. Do not keep changing waypoint after each dig. SELECT is preparation, not progress. WAIT only if no useful safe option.",
                        "criteria": {
                            "WAIT": "No currently useful safe option",
                            **choices,
                        },
                    }
                },
                deadline_ms=1300,
            )
            answer["navigation"] = task.get("navigation", {})
            return answer
    if task.get("agent_goal") in {"CRAFT", "PLACE_BENCH"}:
        options = {
            k: v
            for k, v in options.items()
            if v["kind"] in {"CRAFT", "PLACE", "SELECT"}
        }
        products = {r.get("item_id") for r in options.values() if r["kind"] == "CRAFT"}
        recipes = [
            r
            for r in snapshot["view"].get("survival", {}).get("recipes", [])
            if r.get("item_id") in products
        ]
        return await controller.judge_typed(
            "voxel_agenda_craft",
            {
                "current_plan_step": task.get("stage_goal"),
                "inventory": game["inventory"],
                "native_legality": "Every offered craft has ingredients for ONE batch now. Stage quantity is total desired stock, not required input count; several batches may be needed.",
                "native_recipes": recipes,
                "recent_actions": list(recent)[-3:],
            },
            {
                "action": {
                    "type": "choice",
                    "instructions": "Choose the next native-validated batch of the desired item or its required ingredient. One log can yield several planks: do not compare logs count to desired plank count or demand the entire stage be done in one action. For PLACE_BENCH select carried workbench then PLACE. Each offered option advances this exact craft step; no unrelated recipes.",
                    "criteria": options
                    or {"BLOCKED": "No native craft/dependency or placement available"},
                }
            },
            deadline_ms=1800,
        )
    if task.get("agent_goal") in {"FIND_WOOD", "FIND_STONE"} and game.get("drops"):
        drops = [r for r in game["drops"] if r["item_id"] in targets]
        if drops:
            motions = {
                k: v
                for k, v in options.items()
                if v["kind"] in {"MOVE", "JUMP_MOVE", "SWIM"}
            }
            if motions:
                return await controller.judge_typed(
                    "voxel_pickup_motion",
                    {
                        "goal": task.get("stage_goal", task["request"]),
                        "visible_drops": drops,
                        "player_position": pos,
                    },
                    {
                        "action": {
                            "type": "choice",
                            "instructions": "Collect already dropped needed resources. Choose safe offered movement minimizing distance_to_uncollected_drop; the drop may have slid away from the mined block. Do not turn away to search while the required drop is visible.",
                            "criteria": motions,
                        }
                    },
                    deadline_ms=1200,
                )
    if task.get("agent_goal") == "ASHORE":
        stage = await controller.judge_typed(
            "voxel_shore_phase",
            {
                "head_medium_now": player.get("head_medium"),
                "breath_now": player.get("breath"),
                "standing_dry_now": game["dry_land"],
            },
            {
                "phase": {
                    "type": "choice",
                    "instructions": "For getting ashore, what is needed NOW? drowning_medium means AIR even after a previous swim succeeded. Breathable head with wet feet means SHORE, horizontal travel and landing. Only standing dry means SETTLE.",
                    "criteria": {
                        "AIR": "Head is underwater: swim upward to air",
                        "SHORE": "Head breathing, feet not on dry ground: move to shore",
                        "SETTLE": "Already on dry ground: observe stable support",
                    },
                }
            },
            deadline_ms=1000,
        )
        phase = stage["phase"]["choice"]
        if phase == "AIR":
            air = {k: r for k, r in options.items() if r["kind"] == "SWIM_UP"}
            if air:
                options = air
        elif phase == "SHORE":
            options = {k: r for k, r in options.items() if r["kind"] != "SWIM_UP"}
        a = await controller.judge_typed(
            "voxel_shore_step",
            {
                "goal": "Reach and stand on dry solid land; surfacing alone is not completion",
                "player": player,
                "in_water": game["in_water"],
                "grounded": game["grounded"],
                "dry_land": game["dry_land"],
                "phase": phase,
                "visible_shore": game["shore"],
                "recent_actions": list(recent)[-3:],
            },
            {
                "action": {
                    "type": "choice",
                    "instructions": "Choose the next legal motion toward dry land. If head is underwater, SWIM_UP restores air. With breathable head, swim horizontally reducing distance_to_visible_shore, then MOVE/JUMP_MOVE with reaches_solid_land. Do not keep SWIM_UP once breathing. If no shore is visible, explore unvisited swimming cells to observe more. Repeated failed moves are obstacles: try another legal direction. WAIT only for new observation.",
                    "criteria": {
                        **(
                            {"WAIT": "No useful currently legal motion"}
                            if phase != "AIR" or not options
                            else {}
                        ),
                        **options,
                    },
                }
            },
            deadline_ms=1200,
        )
        return a
    if task.get("agent_goal") in {"EVADE", "SURFACE"}:
        escape = {
            k: r
            for k, r in options.items()
            if r["kind"] in {"MOVE", "JUMP_MOVE", "SWIM_UP", "SWIM"}
        }
        if escape:
            a = await controller.judge_typed(
                "voxel_urgent_motion",
                {
                    "goal": task["agent_goal"],
                    "player": player,
                    "enemies": game["entities"],
                },
                {
                    "action": {
                        "type": "choice",
                        "instructions": "Immediate goal: SURFACE choose SWIM_UP; EVADE choose safe movement maximizing distance_to_nearest_enemy. Do not attack, look around or wait for another plan. Only current legal motion.",
                        "criteria": escape,
                    }
                },
                deadline_ms=1000,
            )
            return {"action": a["action"]}
    if task.get("agent_goal") == "MATERIALS":
        needs = task.get("materials_needed") or {}
        wanted = [
            a
            for a in options.values()
            if a["kind"] == "CRAFT" and a.get("item_id") in needs
        ]
        for row in wanted:
            row["needed_now"] = max(
                0, needs[row["item_id"]] - game["inventory"].get(row["item_id"], 0)
            )
        for row in options.values():
            if row["kind"] == "CRAFT":
                row["is_requested_build_material"] = row.get("item_id") in needs
        state = {
            "goal": "Prepare only these construction materials then return to build",
            "needed": needs,
            "inventory": game["inventory"],
            "player": player,
            "visible_resources": target_rows[:8],
            "enemies": game["entities"],
            "recent_actions": list(recent)[-3:],
        }
        state["native_recipe_facts"] = {
            "logs_to_planks_needs_workbench": False,
            "dropped_resources": task.get("collect_positions", [])[-8:],
        }
        state["offered_crafts"] = [
            {"item_id": v.get("item_id"), "needed_now": v.get("needed_now")}
            for v in options.values()
            if v["kind"] == "CRAFT" and v.get("is_requested_build_material")
        ]
        state["legal_harvest_options"] = [
            {"item_id": v.get("item_id"), "target": v.get("target")}
            for v in options.values()
            if v["kind"] == "DIG" and v.get("wanted_resource")
        ]
        phases = await controller.judge_typed(
            "voxel_material_phase",
            state,
            {
                "phase": {
                    "type": "choice",
                    "instructions": "Choose how to make progress toward the actual needed item IDs. PROCESS when an offered native CRAFT produces a still-needed material from current inventory: these are EXECUTABLE one-batch recipes now, and preparing the entire bill at once is unnecessary. GATHER only if legal_harvest_options has a reachable wanted resource. Seeing distant or unreachable resources is not a legal harvest now. PICKUP only for actual uncollected drop evidence. SEARCH otherwise. Do not wait for all logs before processing owned logs, or keep circling an unreachable tree instead of processing useful materials. Do not choose unrelated workbench/tools.",
                    "criteria": {
                        "PROCESS": "Craft required material from carried resources",
                        "GATHER": "Harvest visible wanted resources",
                        "PICKUP": "Walk to dropped resources",
                        "SEARCH": "Look/explore for further resources",
                    },
                }
            },
            deadline_ms=1500,
        )
        phase = phases["phase"]["choice"]
        if phase == "SEARCH":
            search = await controller.judge_typed(
                "voxel_resource_search",
                {
                    "goal": "Find remaining building resources",
                    "current_visible_resources": target_rows[:6],
                    "last_seen_resources": remembered[-6:],
                    "view_directions_seen_here": viewed,
                    "current_yaw": player.get("yaw"),
                    "recent_actions": list(recent)[-4:],
                },
                {
                    "method": {
                        "type": "choice",
                        "instructions": "No resource in the CURRENT view does not prove the area is empty. If other directions here have not been observed, LOOK_AROUND before walking away from a grove. APPROACH when a last_seen resource is known. EXPLORE after observing the local directions without a useful target.",
                        "criteria": {
                            "LOOK_AROUND": "Observe an unseen camera direction here",
                            "APPROACH": "Approach a remembered resource",
                            "EXPLORE": "Move to inspect another nearby area",
                        },
                    }
                },
                deadline_ms=1200,
            )
            method = search["method"]["choice"]
            if method == "LOOK_AROUND":
                looks = {
                    k: v
                    for k, v in options.items()
                    if v["kind"] == "LOOK" and (not v.get("already_observed_here"))
                }
                if looks:
                    a = await controller.judge_typed(
                        "voxel_resource_look",
                        {
                            "goal": "Observe nearby resources in a direction not yet examined"
                        },
                        {
                            "action": {
                                "type": "choice",
                                "instructions": "Choose an available camera direction to observe; this does not move or spend items.",
                                "criteria": looks,
                            }
                        },
                        deadline_ms=1200,
                    )
                    return a
        viable = {}
        for key, row in options.items():
            if phase == "PROCESS":
                keep = row["kind"] == "CRAFT" and row.get("is_requested_build_material")
            elif phase == "GATHER":
                keep = (
                    row["kind"] == "DIG"
                    and row.get("wanted_resource")
                    or row["kind"] in {"MOVE", "JUMP_MOVE", "LOOK"}
                    or (row["kind"] == "SELECT" and row.get("tool"))
                )
            else:
                keep = row["kind"] in {"MOVE", "JUMP_MOVE", "LOOK"}
            if keep:
                viable[key] = row
        safe_for_materials = player.get("head_medium") != "drowning_medium" and (
            not any(
                (
                    e.get("relation") == "hostile_species" and e.get("distance", 99) < 5
                    for e in game["entities"]
                )
            )
        )
        if viable and safe_for_materials:
            choice = await controller.judge_typed(
                "voxel_material_action",
                {
                    "phase": phase,
                    "needed": needs,
                    "inventory": game["inventory"],
                    "targets": target_rows[:6],
                    "resource_memory": remembered[-6:],
                    "recent_actions": list(recent)[-3:],
                },
                {
                    "action": {
                        "type": "choice",
                        "instructions": "Choose one step for the phase. GATHER prefers an offered wanted DIG now; PICKUP choose smallest distance_to_uncollected_drop; SEARCH approach known target distance_to_last_seen_resource or unvisited cells; PROCESS select required-material craft. WALK choices use distances. No unrelated tasks.",
                        "criteria": {"WAIT": "No safe progress", **viable},
                    }
                },
                deadline_ms=1800,
            )
            return {"action": choice["action"], "phase": phases["phase"]}
    a = await controller.judge_typed(
        "voxel_agent_step",
        state,
        {
            "priority": {
                "type": "choice",
                "instructions": "What matters now? head_medium=breathable and breath10 means NO drowning: do not choose AIR. head_medium=drowning_medium with falling breath means AIR. At hp<=6 with nearby hostile creature choose ESCAPE before resource work. Otherwise GOAL. Use actual values; missing data is not evidence of drowning.",
                "criteria": {
                    "AIR": "Get head into breathable air",
                    "ESCAPE": "Retreat from nearby enemy or escape water",
                    "GOAL": "Advance the current user goal",
                },
            },
            "action": {
                "type": "choice",
                "instructions": "Choose a useful action for this goal. AIR: SWIM_UP. Evade: move farther from enemies. FORWARD: positive progress. ASCEND: JUMP_MOVE rise1. ASHORE: reaches_solid_land or swim toward visible shore. RETURN_SITE: move reducing distance_to_build_site; resources are already sufficient. Resource: DIG wanted harvestable blocks, prefer exposed faces over walking floor; approach targets or drops. LOOK then explore unvisited ground if unseen. Never spin indefinitely. FIND_WOOD/FIND_STONE preserve raw outputs. MATERIALS: CRAFT missing construction materials from logs; no decorations. If stone but no pick: planks, sticks, workbench, place it, then pick from native recipes. Low breath overrides mining. Respect exclusions.",
                "criteria": {
                    "WAIT": "Short wait for new observation/cooldown",
                    "BLOCKED": "A material/tool/geometry issue prevents all useful progress",
                    **options,
                },
            },
        },
        deadline_ms=1800,
    )
    if a["priority"]["choice"] in {"AIR", "ESCAPE"}:
        moves = {
            k: r
            for k, r in options.items()
            if r["kind"] in {"SWIM_UP", "SWIM", "MOVE", "JUMP_MOVE"}
        }
        if moves:
            b = await controller.judge_typed(
                "voxel_agent_escape",
                {
                    "priority": a["priority"]["choice"],
                    "player": player,
                    "enemies": game["entities"],
                },
                {
                    "action": {
                        "type": "choice",
                        "instructions": "Choose the immediate survival action: AIR means swim upward. ESCAPE means move away from visible attackers / towards supported land. Only listed native legal actions.",
                        "criteria": moves,
                    }
                },
                deadline_ms=1200,
            )
            a["action"] = b["action"]
    chosen = options.get(a["action"]["choice"])
    if a["action"]["choice"] == "BLOCKED" and task.get("agent_goal") == "MATERIALS":
        alternatives = {
            k: v
            for k, v in options.items()
            if v["kind"] in {"MOVE", "JUMP_MOVE", "LOOK", "SWIM", "SWIM_UP"}
            or (v["kind"] == "DIG" and v.get("wanted_resource"))
            or (v["kind"] == "CRAFT" and v.get("is_requested_build_material"))
        }
        if alternatives:
            revised = await controller.judge_typed(
                "voxel_material_blocker_review",
                {
                    "needed": task.get("materials_needed"),
                    "inventory": game["inventory"],
                    "known_resources": remembered[-8:],
                    "drops": task.get("collect_positions", [])[-8:],
                    "fact": "Native logs-to-planks recipe uses inventory 2x2; no workbench required.",
                },
                {
                    "action": {
                        "type": "choice",
                        "instructions": "Try an available productive option before declaring a blocker. Missing logs means approach visible/remembered tree or collect dropped logs; absent tree means explore. Already collected planks need no workbench. WAIT only if all listed options cannot help.",
                        "criteria": {
                            "WAIT": "No useful current action",
                            **alternatives,
                        },
                    }
                },
                deadline_ms=1800,
            )
            a["action"] = revised["action"]
            chosen = options.get(a["action"]["choice"])
    if (
        chosen
        and chosen["kind"] == "CRAFT"
        and (task.get("agent_goal") == "MATERIALS")
        and (not chosen.get("is_requested_build_material"))
    ):
        check = await controller.judge_typed(
            "voxel_material_dependency",
            {
                "user_goal": task["request"],
                "required_materials": task.get("materials_needed"),
                "inventory": game["inventory"],
                "proposed_craft": chosen,
            },
            {
                "needed": {
                    "type": "noul",
                    "instructions": "Is this craft actually NECESSARY to produce the requested building materials? Logs→planks needs no workbench or tool. Making sticks, buttons, another workbench or tools before a plain plank house is NOT necessary. Do not add unrelated preparation to this user goal.",
                }
            },
            deadline_ms=1200,
        )
        if check["needed"]["noul"] < 0.8:
            alternatives = {
                k: v
                for k, v in options.items()
                if v["kind"] != "CRAFT" or v.get("is_requested_build_material")
            }
            revised = await controller.judge_typed(
                "voxel_material_step",
                {
                    "inventory": game["inventory"],
                    "required": task.get("materials_needed"),
                    "visible_resources": target_rows[:6],
                },
                {
                    "action": {
                        "type": "choice",
                        "instructions": "Advance material collection: choose available logs→required planks craft, collect dropped logs, mine needed logs, or approach them. No unrelated craft.",
                        "criteria": {"WAIT": "Observe briefly", **alternatives},
                    }
                },
                deadline_ms=1800,
            )
            a["action"] = revised["action"]
    return a
