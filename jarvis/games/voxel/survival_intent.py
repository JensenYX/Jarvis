from __future__ import annotations
import asyncio
import json
import math
from jarvis.games.intent import duration_candidates, number
import re
from .coach import dialogue

ROUTES = {
    "AGENT": "Delegate any outcome needing choices/preparation across actions: play, find/obtain items or food, cultivate/plant/harvest crops, use/eat an item, choose a suitable place, hunt, gather resources. The companion chooses prerequisites and pursues the full result. A requested outcome is not limited to enumerated example tasks.",
    "TRAVEL": "Ask companion to move, jump, climb, escape, swim, surface or get ashore: 往前走/跳上去/游上去/浮起来/躲开. This is actual action, not game chat and not resuming an old digging task.",
    "DIG": "Remove/dig/dismantle specified blocks, or clear a passage. Resource searching/collecting as an objective is AGENT.",
    "PLACE": "Put one or several ordinary building blocks at an explicitly specified place, including 放一块泥土/放置/种沙土 as plausible ASR. Growing seeds, using tools, or asking the companion to find a suitable place is a delegated outcome, AGENT. A structure with geometry is BUILD.",
    "CRAFT": "Manufacture inventory items/tools/materials via a recipe. 做工作台/箱子/炉子 is crafting. ASR 和成木稿/木搞 plausibly means 合成木镐 in game context.",
    "COMBAT": "Fight/attack a creature, protect the player or take over a fight. 打这只/你来打 in a creature context is combat, not digging blocks.",
    "RESUME": "Short start/continue/yes referring to the CURRENT task WITHOUT naming a goal. 继续探索 explicitly names exploration: route AGENT/TRAVEL if current task is something else or absent; do not resume a cancelled house or unresolved mining target. The latest task owns truly short replies.",
    "REVISE": "Change the current removal/crafting/combat task target, amount, exclusions or scope.",
    "RECLAIM": "Stop actions now; human takes over (我自己来/I will fight myself now), or asks for advice only.",
    "CANCEL": "Cancel/abandon/remove a named TASK or building PLAN (取消丛林小屋任务/这个计划不要了). Retire its goal, do not demolish existing blocks. Stop playing temporarily is RECLAIM; demolish an actual structure is DIG.",
    "STATUS": "Ask task progress or remembered target/constraints.",
    "ADVICE": "Ask how, which tool/recipe, or for an opinion; no action requested.",
    "BUILD": "Construct/place a world structure (house/wall/roof/pillars). Manufacturing a portable crafting table/chest/furnace is CRAFT.",
    "OTHER": "Other game fact/chat, ordinary assistant or background speech.",
}


def context(snapshot):
    v = (snapshot or {}).get("view", {})
    p = v.get("self", {})
    s = v.get("survival", {})
    inv = {}
    for row in p.get("inventory_summary", []):
        inv[row["item_id"]] = inv.get(row["item_id"], 0) + row["count"]
    return {
        "phase": (snapshot or {}).get("phase"),
        "player": {
            k: p.get(k)
            for k in (
                "position",
                "eye_position",
                "hp",
                "hp_max",
                "hunger",
                "wielded",
                "breath",
                "breath_max",
                "head_medium",
                "yaw",
            )
        },
        "inventory": inv,
        "focus": v.get("focus"),
        "nodes": [
            {
                k: r[k]
                for k in (
                    "target_id",
                    "item_id",
                    "name",
                    "position",
                    "focused",
                    "diggable",
                    "dig_ms",
                    "harvest",
                    "reason",
                )
                if k in r
            }
            for r in s.get("nodes", [])[:48]
        ],
        "entities": s.get("entities", [])[:8],
        "tools": s.get("tools", [])[:36],
        "workbench": s.get("workbench"),
        "target_states": (s.get("task") or {}).get("target_states", []),
        "path": (s.get("task") or {}).get("path"),
        "in_water": s.get("in_water", False),
        "travel": s.get("travel", []),
        "grounded": s.get("grounded", False),
        "dry_land": s.get("dry_land", False),
        "shore": s.get("shore", []),
        "drops": s.get("drops", []),
        "navigation": s.get("navigation", []),
    }


def brief(task):
    if not task:
        return None
    result = {
        k: task.get(k)
        for k in (
            "id",
            "kind",
            "status",
            "request",
            "goal",
            "agent_goal",
            "project_id",
            "constraints",
            "quantity",
            "completed",
            "pending_question",
            "confirmation",
            "remaining_ms",
            "agenda_index",
            "stage_goal",
            "stalled_s",
        )
    }
    if task.get("agenda"):
        result["agenda"] = task["agenda"].get("steps")
    if task.get("outcome"):
        result["outcome"] = {
            k: task["outcome"].get(k)
            for k in ("objective", "strategy", "success_checks", "milestones")
        }
    if task.get("completed_stages"):
        result["completed_stages"] = [
            {k: r[k] for k in ("goal", "label", "position") if k in r}
            for r in task["completed_stages"][-6:]
        ]
    if task.get("progress"):
        result["progress"] = {
            k: task["progress"][k]
            for k in ("max_radius", "highest_ground", "position")
            if k in task["progress"]
        }
    scope = task.get("scope") or {}
    result["scope"] = {k: v for k, v in scope.items() if k != "targets"}
    if scope.get("targets"):
        result["scope"].update(
            target_count=len(scope["targets"]), targets=scope["targets"][:8]
        )
    return result


async def interpret(controller, text, snapshot, task=None, history=(), project=None):
    durations = duration_candidates(text)
    amounts = {}
    for m in re.finditer(
        "([零〇一二两三四五六七八九十百\\d]+)\\s*(?:块|个|根|把|只|次|blocks?|items?|sticks?|hits?)",
        text,
        re.I,
    ):
        value = number(m[1])
        if value is not None and 1 <= value <= 128:
            amounts[f"Q{len(amounts)}"] = {"span": m[0], "count": int(value)}
    game = context(snapshot)
    referents = {
        "game": "VoxeLibre survival sandbox",
        "focus": game["focus"],
        "visible_blocks": [r["name"] for r in game["nodes"][:8]],
        "visible_creatures": [r["kind"] for r in game["entities"]],
        "in_water": game["in_water"],
        "head_medium": game["player"].get("head_medium"),
    }
    state = {
        "utterance": text,
        "referents": referents,
        "task": brief(task),
        "history": dialogue(list(history)),
        "construction_project": {
            "id": project.project_id,
            "title": project.title,
            "blueprint_title": (project.blueprint or {}).get("title"),
            "request": [r["text"] for r in project.requests[-3:]],
            "status": project.status,
            "question": project.question,
        }
        if project
        else None,
    }
    qs = {
        "route": {
            "type": "choice",
            "instructions": "Interpret the CURRENT human utterance, with prior question/task and plausible ASR homophones. Classify requested behavior, not current physical feasibility. A polite request counts. First-person 我自己做 is human activity; quoted or negated orders do not authorize.",
            "criteria": ROUTES,
        },
        "authorize": {
            "type": "noul",
            "instructions": "Is the CURRENT utterance a request to YOU to perform game actions, including a short start/continue/yes answering task.confirmation or the latest execution question? Polite 帮我/请你让我上岸 asks you to act. Short replies inherit the current task, not a stale construction question. An old request alone is not fresh authority. NO for advice, quotes, hypotheticals, negation or the human acting. Ignore physical readiness.",
        },
        "reclaim": {
            "type": "noul",
            "instructions": "Is the human asking YOU to STOP acting, returning control to the HUMAN now? YES: 我自己来/先别动/只给建议不要操作/I will fight myself now. NO: 你来接手/你来解决/帮我代打 (these delegate TO YOU), future takeover, quotes, negated takeover or stopping speech only. Pay attention to who will act.",
        },
        "change": {
            "type": "noul",
            "instructions": "Does the utterance change or fill task requirements, targets, amount, exceptions or style (including an answer to pending_question)? A pure start/status/reclaim is NO.",
        },
        "scope": {
            "type": "choice",
            "instructions": "Interpret completion scope: 打死/消灭一只 means finish defeating that creature, GOAL; 打一下 means ONCE. A number of output items is GOAL. Choose TIMED only for explicit duration. Choose based on current user wording and pending task, not inventory.",
            "criteria": {
                "GOAL": "Finish the stated goal: 打死/消灭 means defeat, not one hit; craft requested units, remove target group",
                "ONCE": "Exactly one explicit hit/action (打一下/一次), not one creature to defeat",
                "TIMED": "Explicit companion duration",
                "ONGOING": "Continue defending/acting until the human takes over",
                "NONE": "No action or unsupported/unclear limit",
            },
        },
        "duration": {
            "type": "choice",
            "instructions": "Select literal duration applying to companion work now, not quoted/negated text or a future start delay. NONE if absent or unclear.",
            "criteria": {"NONE": "No supported literal duration", **durations},
        },
        "handoff": {
            "type": "choice",
            "instructions": "Classify only a NEW handoff requested by CURRENT utterance, not who currently owns controls. An old 我自己来 in history never turns later questions or commands into a new HUMAN handoff. Questions such as 什么时候天亮/现在我该做什么 mean NONE. 开始/继续/是的 can delegate the task in the latest question. 后面我自己来 requests HUMAN now. 血少了就先退开 is a future condition.",
            "criteria": {
                "HUMAN": "Human takes over now; stop assistant actions",
                "COMPANION": "Companion should act now",
                "PREVIEW": "Only show/explain proposed actions before execution",
                "CONDITION": "A condition for future tactics, not current handoff",
                "NONE": "No handoff",
            },
        },
    }
    qs["autoplay"] = {
        "type": "noul",
        "instructions": "Does the CURRENT utterance delegate playing with general ongoing gameplay decisions (帮我代打五分钟/替我玩一会/你来全权负责)? YES even if a narrow dig task existed. NO for merely continue that exact task, collect a named resource, explicit one step, advice, negation or quoted requests.",
    }
    qs["clear_and_move"] = {
        "type": "noul",
        "instructions": "Is the PRIMARY requested end goal a cleared passage to walk through? 开条路然后往前走/挖通并走过去 are YES. A destination goal such as climb to the hilltop, explore or get ashore with optional digging/placing as a MEANS is NO: keep TRAVEL/AGENT so it can choose terrain changes en route. Merely walking/swimming is NO.",
    }
    qs["resource_collection"] = {
        "type": "noul",
        "instructions": "Is the goal to OBTAIN/GATHER resources for inventory (采集一些材料/多开采一些材料/收集石头/找树砍木头), rather than demolish/remove a specific obstruction or structure? Gathering is a continuing search, tool and pickup task even when phrased 开采. Do not classify clearing a path, demolishing a wall, advice or negated requests as resource collection.",
    }
    qs["cancel_target"] = {
        "type": "choice",
        "instructions": "Only if the current utterance CANCELS a task/plan (not blocks), which known task is meant? Match semantic titles/requests/blueprint title. A hut is a construction project even while a craft task is newest. NONE if no cancellation or materially ambiguous. Existing blocks stay intact.",
        "criteria": {
            "NONE": "No clear cancellation",
            "PROJECT": "The known construction project",
            "TASK": "Current survival/crafting/travel/agent task",
            "ALL": "Explicitly all current tasks/plans",
        },
    }
    if project and project.question:
        qs["project_reply"] = {
            "type": "noul",
            "instructions": "Does the CURRENT utterance answer the pending construction design question or delegate its unspecified details? 你来定/随便你 when asked style/size is a design answer, not a new survival delegation. An explicit new action such as 上岸/停止 does not answer the design question. Use the pending question only when it is what the user is answering.",
        }
    if task:
        qs["continuation"] = {
            "type": "choice",
            "instructions": "Does this utterance start/resume the same current task, state a new/change goal, or neither? A short 开始/继续 or yes to the execution question refers to the CURRENT task. Explicit 建房 refers to construction; explicit 上岸 is a new travel goal if the current task is building. Do not infer a new goal from stale project history.",
            "criteria": {
                "SAME": "Start/resume exactly current task, optionally with a new duration",
                "CHANGE": "New goal or changed requirements",
                "NONE": "Advice/status/reclaim/non-command",
            },
        }
    if amounts:
        qs["amount"] = {
            "type": "choice",
            "instructions": "Select the literal amount of NEW requested work (blocks, item units or hits), excluding negated/prior amounts and time. NONE when no amount applies.",
            "criteria": {"NONE": "No applicable count", **amounts},
        }
    current_q = {k: qs.pop(k) for k in ("authorize", "reclaim", "handoff", "autoplay")}
    current_q["handoff"]["instructions"] = (
        "Who does the CURRENT utterance ask to act? 我/我自己/let ME is the human speaker. 你/帮我/替我/for me addresses the companion. Later explicit delegation overrides an older stop. HUMAN only for a present stop/takeover, never a question about darkness or a complaint. NONE if no handoff."
    )
    current_q["reclaim"]["instructions"] = (
        "Does this current utterance ask the assistant to stop controlling now, so the HUMAN speaker takes over? 我自己来/后面让我玩/Let me take control/暂停代打 are YES. Questions, complaints, 帮我代打 and asking the assistant to choose are NO. Ignore all previous control ownership."
    )
    current_q["autoplay"]["instructions"] = (
        "Is this user asking the ASSISTANT to play autonomously for them now? You play for me/帮我代打/你决定怎么玩 is YES. I will play/let me play/我来玩 is NO. A specific one-off command, quoted/negated delegation, advice or just continue a task is NO."
    )
    current_q["speech_act"] = {
        "type": "choice",
        "instructions": "What is the CURRENT utterance doing? COMMAND means game world actions, not speaking. 只给建议不要接手 is RECLAIM: stop gameplay, can still discuss. 停手 is RECLAIM. Chinese 把木头加工成木板/只拆这面墙后面别碰 directly requests world action. A restriction on other objects does not cancel that action. Questions about darkness/next steps are QUESTION. 开条路然后往前走 includes clearing. Use task_referent only for short replies.",
        "criteria": {
            "COMMAND": "Ask companion to perform named game action(s)",
            "RESUME": "Short start/yes/continue of pending task, no new named target",
            "QUESTION": "Question, complaint or advice discussion without execution",
            "RECLAIM": "Stop companion gameplay or human takes over now",
            "CANCEL": "Cancel/abandon a task or plan",
            "SILENCE": "Stop speaking only",
            "CONDITION": "Future conditional constraint without a present command",
        },
    }
    current_q["question_target"] = {
        "type": "choice",
        "instructions": "What information does the current question ask for, if any? Asking your current goal/plan/progress is STATUS and never changes that goal. Asking the next recommended action is ADVICE. A polite can-you-help-me DO something is ACTION, not a status question.",
        "criteria": {
            "STATUS": "Current task goal, plan, progress or reason for being stuck",
            "ADVICE": "Suggestion for what the human could do next",
            "FACT": "Game state or item/mechanic explanation",
            "ACTION": "Requests an operation or is not an information question",
        },
    }
    current_q["outcome_required"] = {
        "type": "noul",
        "instructions": "Does the CURRENT request ask the companion to achieve an open-ended result or use an item, where choosing a place, searching, collecting or preparation belongs to the companion? YES for find food, plant seeds, harvest crops, use/eat an item, or find a suitable place to put it. NO for status/advice, short resume, stop/cancel, a precisely targeted ordinary block placement/removal, crafting a named portable product, a direct movement destination, or building a geometric structure (those have their own executors).",
    }
    current_state = {
        "utterance": text,
        "task_referent": {
            "request": (task or {}).get("request"),
            "pending_question": (task or {}).get("pending_question"),
            "confirmation": (task or {}).get("confirmation"),
        },
    }
    operation_q = {
        "operation": {
            "type": "choice",
            "instructions": "What is the affirmative CURRENT request in these words? Classify its END RESULT. Navigation to a hill, surface, shore or new terrain remains NAVIGATE even if an older task was digging. Opening a wall is OPEN, removing the entire wall is REMOVE. A restriction such as do not craft does not negate the different requested action. No previous command is authority. Questions about what YOU are doing are STATUS. Human first-person plans are DISCUSS unless asking the companion for help.",
            "criteria": {
                "NAVIGATE": "Companion should move to a destination or explore",
                "OPEN": "Companion should open a traversable passage through an obstacle",
                "REMOVE": "Companion should remove specified objects",
                "GATHER": "Companion should obtain materials/resources",
                "BUILD": "Companion should build or revise a world structure",
                "CRAFT": "Companion should make a portable item/tool",
                "PLACE": "Companion should place an ordinary block at a specified place",
                "INTERACT": "Companion should use an item or achieve a result through finding a place/preparing/planting/harvesting/eating",
                "FIGHT": "Companion should fight",
                "PLAY": "General delegated gameplay with no named end result",
                "STATUS": "Question about current goal/progress/plan",
                "STOP": "Stop companion actions or human takes over now",
                "CANCEL": "Cancel/abandon a task",
                "RESUME": "Short continuation without a new named goal",
                "FACT": "Question about current inventory, health, held item or item/mechanic purpose",
                "DISCUSS": "Advice, hypothetical, quoted command, or human will act",
            },
        }
    }
    answer, control, current_operation = await asyncio.gather(
        controller.judge_typed("voxel_survival_intent", state, qs, deadline_ms=3500),
        controller.judge_typed(
            "voxel_current_control", current_state, current_q, deadline_ms=3500
        ),
        controller.judge_typed(
            "voxel_current_operation",
            {"current_words": text},
            operation_q,
            deadline_ms=2500,
        ),
    )
    answer.update({k: control[k] for k in current_q if k in control})
    answer["raw_control"] = dict(control)
    act = answer.get("speech_act", {})
    if act.get("choice") == "RECLAIM" and (
        answer["route"].get("choice")
        in {"AGENT", "TRAVEL", "BUILD", "DIG", "CRAFT", "PLACE"}
        or (
            answer.get("handoff", {}).get("choice") == "COMPANION"
            and answer["handoff"].get("confidence", 0) >= 0.8
        )
    ):
        review = await controller.judge_typed(
            "voxel_actor_review",
            {"current_words": text},
            {
                "actor": {
                    "type": "choice",
                    "instructions": "只根据当前这句话，接下来要操作游戏的是谁？“你接手/替我玩/你决定怎么玩”让陪玩操作；“我自己来/让我玩/停手”让用户操作。不要把前半句不再做某项工作误解为拒绝后半句的新委托。",
                    "criteria": {
                        "COMPANION": "当前委托陪玩行动",
                        "HUMAN": "当前让陪玩停止、用户自己操作",
                        "DISCUSS": "当前只是讨论或询问",
                    },
                }
            },
            deadline_ms=1200,
        )
        answer["actor_review"] = review
        if (
            review["actor"]["choice"] == "COMPANION"
            and review["actor"]["confidence"] >= 0.8
        ):
            act = answer["speech_act"] = {
                "choice": "COMMAND",
                "confidence": review["actor"]["confidence"],
            }
    if (
        answer.get("autoplay", {}).get("noul", 0) >= 0.8
        and answer.get("handoff", {}).get("choice") == "COMPANION"
        and (answer["handoff"].get("confidence", 0) >= 0.8)
    ):
        act = answer["speech_act"] = {
            "choice": "COMMAND",
            "confidence": answer["handoff"]["confidence"],
        }
    if act.get("confidence", 0) >= 0.7:
        action = act["choice"]
        if action in {"RECLAIM", "CANCEL"}:
            answer["route"] = {**answer["route"], "choice": action}
            answer["reclaim"] = {"noul": 1}
            answer["autoplay"] = {"noul": 0}
            answer["authorize"] = {"noul": 0}
            answer["handoff"] = {"choice": "HUMAN", "confidence": act["confidence"]}
        elif action == "QUESTION":
            if answer["route"]["choice"] in {"RECLAIM", "AGENT", "RESUME", "REVISE"}:
                answer["route"] = {
                    **answer["route"],
                    "choice": "ADVICE"
                    if answer["route"].get("probabilities", {}).get("ADVICE", 0) > 0.2
                    else "OTHER",
                }
            answer["reclaim"] = {"noul": 0}
            answer["authorize"] = {"noul": 0}
            answer["handoff"] = {"choice": "NONE", "confidence": 1}
        elif action == "COMMAND":
            answer["authorization_reviewed"] = True
            answer["reclaim"] = {"noul": 0}
            answer["handoff"] = {"choice": "COMPANION", "confidence": act["confidence"]}
            if answer["route"]["choice"] in {"REVISE", "RESUME", "RECLAIM"}:
                options = {
                    k: v
                    for k, v in answer["route"].get("probabilities", {}).items()
                    if k
                    in {"DIG", "CRAFT", "TRAVEL", "AGENT", "BUILD", "PLACE", "COMBAT"}
                }
                if options:
                    answer["route"] = {
                        **answer["route"],
                        "choice": max(options, key=options.get),
                    }
    answer["duration_ms"] = durations.get(answer["duration"]["choice"], {}).get(
        "duration_ms"
    )
    if not durations and answer.get("scope", {}).get("choice") == "TIMED":
        answer["scope"] = {**answer["scope"], "choice": "GOAL"}
    answer["quantity"] = amounts.get(answer.get("amount", {}).get("choice"), {}).get(
        "count"
    )
    answer["effective_route"] = effective_route(answer)
    question = answer.get("question_target", {})
    if (
        question.get("choice") in {"STATUS", "ADVICE"}
        and question.get("confidence", 0) >= 0.85
    ):
        answer["effective_route"] = question["choice"]
        answer["authorize"] = {"noul": 0}
        answer["authorization_reviewed"] = False
        answer["change"] = {"noul": 0}
        answer["reclaim"] = {"noul": 0}
        answer["autoplay"] = {"noul": 0}
    if answer["effective_route"] == "RESUME" and (
        not task or task.get("agent_goal") != "EXPLORE"
    ):
        reply = await controller.judge_typed(
            "voxel_named_continuation",
            {"utterance": text, "current_task": brief(task)},
            {
                "meaning": {
                    "type": "choice",
                    "instructions": "Does this reply name a NEW specific activity despite starting with continue? 继续探索 explicitly means exploration, not an old mining/building task. 继续/开始 alone means SAME only with a current task. Do not infer an unstated activity.",
                    "criteria": {
                        "SAME": "Short continuation of exactly current task",
                        "EXPLORE": "Explicitly names exploring/walking around",
                        "OTHER": "Different named activity or unclear",
                    },
                }
            },
            deadline_ms=2000,
        )
        if (
            reply["meaning"]["choice"] == "EXPLORE"
            and reply["meaning"]["confidence"] >= 0.65
        ):
            answer["effective_route"] = "AGENT"
            answer["named_objective"] = "EXPLORE"
    if (
        answer.get("clear_and_move", {}).get("noul", 0) >= 0.75
        and answer.get("speech_act", {}).get("choice") == "COMMAND"
    ):
        answer["effective_route"] = "DIG"
    if (
        answer.get("resource_collection", {}).get("noul", 0) >= 0.8
        and answer.get("speech_act", {}).get("choice") == "COMMAND"
    ):
        answer["effective_route"] = "AGENT"
    if (
        answer.get("autoplay", {}).get("noul", 0) >= 0.8
        and answer["effective_route"]
        not in {"RECLAIM", "CANCEL", "STATUS", "ADVICE", "OTHER"}
        and (
            answer.get("speech_act", {}).get("choice") not in {"QUESTION", "CONDITION"}
        )
    ):
        answer["effective_route"] = "AGENT"
    if answer.get("project_reply", {}).get("noul", 0) >= 0.8 and answer[
        "effective_route"
    ] not in {"RECLAIM", "TRAVEL", "CANCEL"}:
        answer["effective_route"] = "BUILD"
    if (
        task
        and answer.get("continuation", {}).get("choice") == "SAME"
        and (answer["continuation"].get("confidence", 0) >= 0.75)
        and (task.get("status") not in {"NEEDS_INPUT", "COMPLETED", "CANCELLED"})
        and (answer.get("autoplay", {}).get("noul", 0) < 0.8)
        and (answer.get("speech_act", {}).get("choice", "RESUME") == "RESUME")
        and (answer["effective_route"] in {"RESUME", task.get("kind")})
    ):
        answer["effective_route"] = "RESUME"
    op = current_operation.get("operation", {})
    answer["operation_review"] = op
    mapping = {
        "NAVIGATE": "TRAVEL",
        "OPEN": "DIG",
        "REMOVE": "DIG",
        "GATHER": "AGENT",
        "INTERACT": "AGENT",
        "BUILD": "BUILD",
        "CRAFT": "CRAFT",
        "PLACE": "PLACE",
        "FIGHT": "COMBAT",
        "PLAY": "AGENT",
        "STATUS": "STATUS",
        "STOP": "RECLAIM",
        "CANCEL": "CANCEL",
    }
    probabilities = op.get("probabilities", {})
    action_mass = sum(
        (
            probabilities.get(k, 0)
            for k in (
                "NAVIGATE",
                "OPEN",
                "REMOVE",
                "GATHER",
                "BUILD",
                "CRAFT",
                "PLACE",
                "INTERACT",
                "FIGHT",
                "PLAY",
            )
        )
    )
    if (
        op.get("choice") in {"NAVIGATE", "PLAY"}
        and action_mass >= 0.9
        and (op.get("confidence", 0) < 0.7)
    ):
        op = {**op, "confidence": action_mass}
        answer["action_family_probability"] = action_mass
    if op.get("confidence", 0) >= 0.7 and op.get("choice") == "FACT":
        answer["effective_route"] = "OTHER"
        answer["authorize"] = {"noul": 0}
        answer["authorization_reviewed"] = False
        answer["autoplay"] = {"noul": 0}
    if op.get("confidence", 0) >= 0.7 and op.get("choice") in mapping:
        answer["effective_route"] = mapping[op["choice"]]
        if op["choice"] not in {"STATUS", "STOP", "CANCEL"}:
            answer["authorization_reviewed"] = True
            answer["authorize"] = {"noul": 1}
            answer["reclaim"] = {"noul": 0}
            answer["handoff"] = {"choice": "COMPANION", "confidence": op["confidence"]}
        else:
            answer["authorization_reviewed"] = False
            answer["authorize"] = {"noul": 0}
            answer["autoplay"] = {"noul": 0}
            answer["reclaim"] = {"noul": 0 if op["choice"] == "STATUS" else 1}
        if op["choice"] == "OPEN":
            answer["removal_outcome"] = "PASSAGE"
    if (
        question.get("choice") == "FACT"
        and question.get("confidence", 0) >= 0.9
        and (answer["effective_route"] == "STATUS")
    ):
        answer["effective_route"] = "OTHER"
        answer["authorize"] = {"noul": 0}
        answer["authorization_reviewed"] = False
    if (
        answer.get("outcome_required", {}).get("noul", 0) >= 0.75
        and authorized(answer)
        and (
            answer["effective_route"]
            not in {"RECLAIM", "CANCEL", "STATUS", "ADVICE", "BUILD", "CRAFT"}
        )
    ):
        answer["effective_route"] = "AGENT"
    if (
        answer.get("question_target", {}).get("choice") == "STATUS"
        and answer["question_target"].get("confidence", 0) >= 0.65
        and (answer.get("speech_act", {}).get("choice") == "QUESTION")
        and (answer["speech_act"].get("confidence", 0) >= 0.6)
    ):
        answer["effective_route"] = "STATUS"
        answer["authorize"] = {"noul": 0}
        answer["authorization_reviewed"] = False
        answer["reclaim"] = {"noul": 0}
    if (
        answer["effective_route"] == "RECLAIM"
        and answer["route"]["choice"] == "TRAVEL"
        and (answer["route"].get("confidence", 0) >= 0.65)
        and (answer.get("reclaim", {}).get("noul", 0) < 0.6)
    ):
        motion = await controller.judge_typed(
            "voxel_escape_or_handoff",
            {"current_request": text},
            {
                "meaning": {
                    "type": "choice",
                    "instructions": "Is the user asking the companion to MOVE the player away from danger, or to stop controlling and hand back to the human? “有人打我，先退远点/危险先躲开” requests companion escape. “停手我自己来/让我自己躲” is a human takeover. Interpret the actor and physical intent.",
                    "criteria": {
                        "ESCAPE": "Companion should move/evade now",
                        "HAND_BACK": "Human takes over now",
                        "ADVICE": "Only asks for information",
                    },
                }
            },
            deadline_ms=1800,
        )
        if (
            motion["meaning"]["choice"] == "ESCAPE"
            and motion["meaning"]["confidence"] >= 0.8
        ):
            answer["effective_route"] = "TRAVEL"
            answer["authorization_reviewed"] = True
            answer["reclaim"] = {"noul": 0}
            answer["handoff"] = {"choice": "COMPANION", "confidence": 1}
    if (
        answer["effective_route"] == "RECLAIM"
        and answer.get("cancel_target", {}).get("choice") in {"ALL", "PROJECT", "TASK"}
        and (
            answer.get("speech_act", {}).get("probabilities", {}).get("CANCEL", 0)
            >= 0.2
            or answer.get("operation_review", {})
            .get("probabilities", {})
            .get("CANCEL", 0)
            >= 0.2
        )
    ):
        ending = await controller.judge_typed(
            "voxel_pause_or_cancel",
            {"current_request": text, "active_task": brief(task)},
            {
                "ending": {
                    "type": "choice",
                    "instructions": "Does the human retire/clear an existing goal or just temporarily reclaim controls? 全部清空/清掉所有计划/不要这个任务 means CANCEL. 我自己来/停手 alone means PAUSE. Only requested task records are retired; never delete world blocks or general memory.",
                    "criteria": {
                        "CANCEL": "Retire requested task/plan",
                        "PAUSE": "Pause automation but keep the task",
                    },
                }
            },
            deadline_ms=1800,
        )
        if (
            ending["ending"]["choice"] == "CANCEL"
            and ending["ending"]["confidence"] >= 0.75
        ):
            answer["effective_route"] = "CANCEL"
    return answer


def effective_route(a):
    route = a["route"]["choice"]
    if route == "CANCEL":
        return route
    if (
        a.get("speech_act", {}).get("choice") == "RECLAIM"
        and a["speech_act"].get("confidence", 0) >= 0.6
    ):
        return "RECLAIM"
    if (
        route == "RECLAIM"
        and a["reclaim"]["noul"] < 0.65
        and (
            not (
                a.get("handoff", {}).get("choice") == "HUMAN"
                and a["handoff"].get("confidence", 0) >= 0.6
            )
        )
    ):
        probabilities = a["route"].get("probabilities", {})
        options = {
            k: v
            for k, v in probabilities.items()
            if k
            in {"OTHER", "ADVICE", "STATUS", "BUILD", "DIG", "CRAFT", "TRAVEL", "AGENT"}
        }
        route = max(options, key=options.get) if options else "OTHER"
    handoff = a.get("handoff", {}).get("choice")
    if (
        handoff == "HUMAN"
        and a["handoff"]["confidence"] >= 0.6
        and (route == "RECLAIM" or a["reclaim"]["noul"] >= 0.7)
    ):
        return "RECLAIM"
    if handoff == "PREVIEW" and a["authorize"]["noul"] < 0.6:
        return route if route in {"ADVICE", "RECLAIM"} else "STATUS"
    if (
        handoff == "CONDITION"
        and (a["change"]["noul"] >= 0.8 or a["route"]["confidence"] < 0.8)
        and (a["handoff"]["confidence"] >= 0.7)
    ):
        return "REVISE"
    if handoff == "COMPANION" and route in {
        "DIG",
        "PLACE",
        "CRAFT",
        "COMBAT",
        "RESUME",
        "REVISE",
        "TRAVEL",
        "AGENT",
    }:
        return route
    if a["reclaim"]["noul"] >= 0.8 and a["change"]["noul"] < 0.8:
        return "RECLAIM"
    if (
        route in {"DIG", "PLACE", "CRAFT", "COMBAT", "TRAVEL", "AGENT"}
        and a["authorize"]["noul"] < 0.4
        and (a["change"]["noul"] < 0.7)
        and (not a.get("authorization_reviewed"))
    ):
        return "ADVICE"
    return route


def authorized(a):
    if (
        a.get("handoff", {}).get("choice") == "HUMAN"
        and a["handoff"]["confidence"] >= 0.65
        and (a["reclaim"]["noul"] >= 0.8)
    ):
        return False
    if (
        a.get("handoff", {}).get("choice") in {"PREVIEW", "CONDITION"}
        and a["authorize"]["noul"] < 0.85
        and (not a.get("authorization_reviewed"))
    ):
        return False
    return (
        a.get("authorization_reviewed") is True or a["authorize"]["noul"] >= 0.85
    ) and (
        a["reclaim"]["noul"] < 0.8 or a.get("handoff", {}).get("choice") == "COMPANION"
    )


async def dig_scope(controller, text, snapshot, previous=None, reasoning=None):
    game = context(snapshot)
    lengths = {}
    for m in re.finditer(
        "([零〇一二两三四五六七八九十百\\d]+)\\s*(?:格|米|blocks?|met(?:er|re)s?)",
        text,
        re.I,
    ):
        count = number(m[1])
        if count is not None:
            lengths[str(int(count))] = {"span": m[0], "blocks": int(count)}
    a = await controller.judge_typed(
        "voxel_dig_scope",
        {"request": text, "previous": brief(previous)},
        {
            "shape": {
                "type": "choice",
                "instructions": "What removal scope does the user mean? 挖通这条路/清开正前方通道 means a passage through successive obstructions, not just one currently visible block. 挖一下/这块 means the focused block. Material/structure groups select individual objects.",
                "criteria": {
                    "FOCUS": "One current focused block",
                    "GROUP": "Specified group of blocks/material or demolition structure",
                    "PASSAGE": "Clear a walkable passage in a direction, including newly exposed obstructions",
                    "UNCLEAR": "No resolvable target",
                },
            },
            "direction": {
                "type": "choice",
                "instructions": "If clearing a passage, which direction relative to current player facing? 这条路 without another direction means the way ahead. Do not infer from absolute coordinate signs.",
                "criteria": {
                    "FRONT": "Ahead/forward/current path",
                    "BACK": "Behind/backward",
                    "LEFT": "To the left",
                    "RIGHT": "To the right",
                },
            },
            "width": {
                "type": "choice",
                "instructions": "Explicit requested passage width in blocks; BODY if no width stated, meaning just enough space for this player. A distance/quantity is not width.",
                "criteria": {
                    "BODY": "Player clearance, no explicit width",
                    **{str(i): f"{i} blocks wide" for i in range(1, 5)},
                },
            },
            "height": {
                "type": "choice",
                "instructions": "Explicit requested passage height; BODY if no height stated. Do not confuse length with height.",
                "criteria": {
                    "BODY": "Two air blocks to walk through",
                    **{str(i): f"{i} blocks high" for i in range(2, 5)},
                },
            },
            "length": {
                "type": "choice",
                "instructions": "Explicit passage LENGTH in blocks only. OPEN means clear through the currently encountered obstruction until an observed open exit; it is not an invented length. Long/unknown routes stay bounded to the local visible work area.",
                "criteria": {
                    "OPEN": "No stated distance. Until an observed open exit, within local work area",
                    **lengths,
                },
            },
            "advance": {
                "type": "noul",
                "instructions": "Does clearing this path serve getting the player through/forward? A general 帮我打通前面这条路 implies walking through the cleared passage. Only explicit 只挖别走/不要移动 is NO.",
            },
        },
        deadline_ms=1800,
    )
    shape = a["shape"]["choice"]
    if a["shape"]["confidence"] < 0.7 or shape == "UNCLEAR":
        state = {"current_request": text, "focused_block": game["focus"]}
        meanings = {
            "PASSAGE": "Make a traversable opening THROUGH an obstacle, exposing successive blocks as needed",
            "FOCUS": "Break one specified block only",
            "GROUP": "Remove an entire specified object/group/structure",
            "UNCLEAR": "Target or purpose cannot be resolved",
        }
        r = await controller.judge_typed(
            "voxel_removal_outcome",
            state,
            {
                "outcome": {
                    "type": "choice",
                    "instructions": "Judge the desired RESULT, not whether the crosshair hits one block. 打通墙/打个能过去的洞/穿过前面岩壁 means PASSAGE: a walkable opening. 拆掉整面墙 is GROUP; 打掉这一块 is FOCUS. A wall can contain many blocks even when only one face is visible.",
                    "criteria": meanings,
                }
            },
            deadline_ms=1800,
        )
        shape = r["outcome"]["choice"]
        a["outcome"] = r
        if (r["outcome"]["confidence"] < 0.7 or shape == "UNCLEAR") and reasoning:
            from .judgment import choose

            reviewed = await choose(
                reasoning,
                "Interpret the requested removal outcome. An opening THROUGH a wall is a passage; removing a single block is not equivalent.",
                state,
                meanings,
            )
            shape = reviewed["choice"]
            a["outcome_review"] = reviewed
        if shape == "UNCLEAR":
            return {
                "question": "你想在障碍里开一个能走过去的口，还是拆掉指定的一整片？",
                "judgment": a,
            }
    if shape != "PASSAGE":
        return {"shape": shape, "judgment": a}
    if a["length"]["choice"] != "OPEN" and int(a["length"]["choice"]) > 8:
        return {
            "shape": shape,
            "question": "这段通道超过当前可观察范围。先打通眼前八格以内这一段，可以吗？",
            "judgment": a,
        }
    p = snapshot["view"]["self"]
    yaw = p.get("yaw", 0)
    dx, dz = (-math.sin(yaw), math.cos(yaw))
    if a["direction"]["choice"] == "BACK":
        dx, dz = (-dx, -dz)
    elif a["direction"]["choice"] == "LEFT":
        dx, dz = (-dz, dx)
    elif a["direction"]["choice"] == "RIGHT":
        dx, dz = (dz, -dx)
    dx, dz = (1 if dx > 0 else -1, 0) if abs(dx) > abs(dz) else (0, 1 if dz > 0 else -1)
    origin = {
        k: math.floor(p["position"][k] + (0.1 if k == "y" else 0.5))
        for k in ("x", "y", "z")
    }
    return {
        "shape": shape,
        "corridor": {
            "origin": origin,
            "dx": dx,
            "dz": dz,
            "width": int(a["width"]["choice"]) if a["width"]["choice"] != "BODY" else 1,
            "height": int(a["height"]["choice"])
            if a["height"]["choice"] != "BODY"
            else 2,
            "length": int(a["length"]["choice"])
            if a["length"]["choice"] != "OPEN"
            else 8,
            "until_open": a["length"]["choice"] == "OPEN",
            "advance": a["advance"]["noul"] >= 0.7,
        },
        "judgment": a,
    }


async def placement(controller, text, snapshot, previous=None):
    game = context(snapshot)
    materials = (snapshot["view"].get("construction") or {}).get("materials", [])
    options = {r["item_id"]: r.get("name", r["item_id"]) for r in materials}
    a = await controller.judge_typed(
        "voxel_place_arguments",
        {
            "request": text,
            "previous": brief(previous),
            "held": game["player"]["wielded"],
            "inventory": game["inventory"],
            "focus": game["focus"],
        },
        {
            "material": {
                "type": "choice",
                "instructions": "Which ordinary block does the user request? Use the exact named material even if not currently held: code can select it. 泥土=Dirt; plausible ASR 沙土/种沙土 can mean placing dirt when that is the actual material. HELD only when no material name was specified.",
                "criteria": {
                    "HELD": "Current held block, no material specified",
                    "UNCLEAR": "Unknown/unsupported/ambiguous material",
                    **options,
                },
            },
            "target": {
                "type": "choice",
                "instructions": "Does the requested placement location refer to ONE current focused block face (这里/放一块/准星处), or request multiple blocks/geometry needing a design? Two or more blocks cannot occupy the same cell: use DESIGN even when only a count is given. Infer deictic place only from current focus, never an invented coordinate.",
                "criteria": {
                    "FOCUS": "Adjacent air cell at the current aimed block face",
                    "DESIGN": "Multiple blocks arranged as a line, wall, house or other geometry",
                    "UNCLEAR": "Location is materially ambiguous",
                },
            },
        },
        deadline_ms=1800,
    )
    item = a["material"]["choice"]
    if item == "HELD":
        item = (game["player"]["wielded"] or {}).get("item_id")
    if item not in options:
        return {
            "question": "要放哪种方块？你可以直接说材料名，我会从快捷栏选择。",
            "judgment": a,
        }
    focus = game.get("focus") or {}
    if a["target"]["choice"] == "DESIGN":
        return {"design": True, "judgment": a}
    if a["target"]["choice"] != "FOCUS" or not focus.get("place_position"):
        return {
            "question": "请看向要放置位置旁的方块表面，再告诉我放在这里。",
            "judgment": a,
        }
    return {
        "scope": {
            "kind": "PLACE",
            "targets": [{"position": focus["place_position"], "item_id": item}],
        },
        "item_id": item,
        "quantity": 1,
        "judgment": a,
    }


async def select_single_target(controller, text, snapshot, task=None, extra_nodes=()):
    from .judgment import bounded_choice

    game = context(snapshot)
    p = game["player"]
    yaw = p.get("yaw", 0)
    options = {}
    for i, r in enumerate([*game["nodes"], *extra_nodes]):
        at = r["position"]
        dx = at["x"] - p["position"]["x"]
        dz = at["z"] - p["position"]["z"]
        options["N" + str(i)] = {
            **r,
            "height_above_feet": round(at["y"] - p["position"]["y"], 2),
            "forward": round(-dx * math.sin(yaw) + dz * math.cos(yaw), 2),
            "right": round(dx * math.cos(yaw) + dz * math.sin(yaw), 2),
        }
    if not options:
        return {"targets": [], "ambiguous": True, "hostiles": False}
    a = await bounded_choice(
        controller,
        "voxel_single_removal_target",
        {"current_request": text, "prior_request": (task or {}).get("request")},
        "Select the ONE object named by the CURRENT removal request. Match material, relative position, height, and exclusions together. A low wood plank is never the focused stone. focused=true resolves only a bare this-block request; explicit material/position wins. right/forward are relative to the current view; positive right is right. NONE if the described target is not present or is ambiguous, never substitute another material.",
        {"NONE": "No unambiguous matching object", **options},
        deadline_ms=2000,
    )
    key = a["action"]["choice"]
    row = options.get(key)
    return {
        "targets": [{"key": key, **row}] if row else [],
        "ambiguous": not row or a["action"].get("confidence", 0) < 0.7,
        "hostiles": False,
        "judgments": {"target": a["action"]},
    }


async def select_targets(controller, text, snapshot, task=None, extra_nodes=()):
    game = context(snapshot)
    raw = (snapshot or {}).get("view", {}).get("survival", {})
    nodes = list(raw.get("nodes") or []) + list(extra_nodes)
    seen = set()
    nodes = [
        r
        for r in nodes
        if not (str(r.get("position")) in seen or seen.add(str(r.get("position"))))
    ]
    entities = list(raw.get("entities") or [])
    options = {
        f"N{i}": {
            k: r[k]
            for k in (
                "target_id",
                "item_id",
                "name",
                "position",
                "focused",
                "historical",
            )
            if k in r
        }
        for i, r in enumerate(nodes[:128])
    }
    options.update({f"E{i}": r for i, r in enumerate(entities[:12])})
    if not options:
        return {"targets": [], "ambiguous": True, "hostiles": False, "quantity": None}
    answers = {}
    keys = list(options)
    for start in range(0, len(keys), 6):
        part = {k: options[k] for k in keys[start : start + 6]}
        state = {
            "scene_objects": part,
            "relevant_prior_task": brief(task),
            "player_position": game["player"]["position"],
        }
        qs = {
            key: {
                "type": "choice",
                "instructions": f"用户说：“{text}”。只判断这个对象是否是用户要操作的目标："
                + json.dumps(row, ensure_ascii=False)
                + "。明确指定一组时应包含整组；不要把要保留的对象选中。focused=true代表准星目标；历史坐标只是目标记忆，不代表当前可见。",
                "criteria": {
                    "YES": "用户要操作此对象",
                    "NO": "不是要求操作的对象，或者要求保留",
                    "UNCLEAR": "指代有歧义无法确定",
                },
            }
            for key, row in part.items()
        }
        qs.update(
            {
                "ambiguous": {
                    "type": "noul",
                    "instructions": f"用户说“{text}”。目标指代是否存在无法从场景和前文解决的歧义？明确的一组多个对象不是歧义。",
                },
                "hostiles": {
                    "type": "noul",
                    "instructions": f"用户说“{text}”。是否授权持续防御附近陆续可见的敌对生物，而不只当前点名的一个？不包含未要求攻击的中立动物。",
                },
            }
        )
        a = await controller.judge_typed(
            "voxel_survival_targets", state, qs, deadline_ms=2000
        )
        answers.update(
            {k: v for k, v in a.items() if k not in {"ambiguous", "hostiles"}}
        )
        if start == 0:
            answers.update(ambiguous=a["ambiguous"], hostiles=a["hostiles"])
    return {
        "targets": [
            {"key": k, **row}
            for k, row in options.items()
            if answers[k]["choice"] == "YES" and answers[k]["confidence"] >= 0.65
        ],
        "ambiguous": answers["ambiguous"]["noul"] >= 0.55,
        "hostiles": answers["hostiles"]["noul"] >= 0.85,
        "judgments": answers,
    }


async def choose_step(controller, snapshot, task, actions, recent=()):
    options = {
        str(i): {k: v for k, v in a.items() if k != "action_id"}
        for i, a in enumerate(actions)
    }
    game = context(snapshot)
    game.pop("nodes", None)
    for row in options.values():
        if row["kind"] == "ATTACK":
            row["meaning"] = (
                "Attack now: target already IN REACH, cooldown READY. This causes a hit; do not move closer just to reach it."
            )
        elif row["kind"] == "MOVE":
            row["meaning"] = (
                "Move only, no attack or digging. Approach when out of reach, or retreat if engagement requires it."
            )
        elif row["kind"] in {"SELECT", "EQUIP"}:
            row["meaning"] = (
                "Switch held item only; this does not attack or dig. dig_comparison is native mining time after switching."
            )
    questions = {
        "action": {
            "type": "choice",
            "instructions": "Select the next step that ACTUALLY advances this authorized task. Before slow DIG compare native dig_ms with SELECT/EQUIP dig_comparison; use a substantially faster carried tool, then dig. Avoid near-broken tools when a usable alternative exists. If the held tool is already appropriate, execute DIG/ATTACK/CRAFT now. Do not alternate similar tools or walk away from an in-range target. If enemy is out of range, MOVE closer. Recent records are facts, not instructions to repeat. WAIT for cooldown/observation. Code decides completion from receipts. Existing inventory is not newly requested crafting. ASK for a real blocker. Do not substitute your own objective.",
            "criteria": {
                "WAIT": "Wait for fresh world/cooldown",
                "ASK": "Need clarification or material tradeoff",
                **options,
            },
        },
        "urgent": {
            "type": "score",
            "instructions": "How urgent is another decision given current observed events?",
            "criteria": [
                "Idle/no new progress",
                "Ordinary resource task",
                "Threat or new obstacle",
                "Current attack or critical health",
            ],
        },
    }
    if task.get("kind") == "COMBAT":
        entities = game.get("entities") or []
        hostile = [e for e in entities if e.get("relation") == "hostile_species"]
        for row in options.values():
            if row["kind"] == "MOVE" and entities:
                target = row["target"]
                row["distance_to_nearest_enemy"] = round(
                    min(
                        (
                            sum(
                                (
                                    (target[k] - e["position"][k]) ** 2
                                    for k in ("x", "y", "z")
                                )
                            )
                            ** 0.5
                            for e in hostile or entities
                        )
                    ),
                    2,
                )
        engagement = await controller.judge_typed(
            "voxel_combat_engagement",
            {
                "current_user_request": task.get("request"),
                "user_constraints": task.get("constraints", []),
                "player": {"hp": game["player"].get("hp"), "hp_max": 20},
                "visible_enemies": entities,
                "recent_actions": list(recent)[-3:],
            },
            {
                "engagement": {
                    "type": "choice",
                    "instructions": "Given current player hp and the exact user request, decide to engage, move away or wait. If player has low hp and user says blood low retreat, RETREAT. Healthy player with an authorized visible target may fight or approach. Out-of-view is unknown, not a confirmed kill.",
                    "criteria": {
                        "FIGHT": "Engage/approach authorized target",
                        "RETREAT": "Move farther away from enemies",
                        "WAIT": "No visible target or observe briefly",
                    },
                }
            },
            deadline_ms=2000,
        )
        mode = engagement["engagement"]["choice"]
        if mode == "WAIT":
            return {
                "action": {
                    "choice": "WAIT",
                    "confidence": engagement["engagement"]["confidence"],
                },
                "engagement": engagement["engagement"],
            }
        if mode == "RETREAT":
            options = {k: v for k, v in options.items() if v["kind"] == "MOVE"}
            questions["action"]["instructions"] = (
                "The separate combat judgment selected RETREAT. Choose a legal movement that increases distance from current enemies; do not choose closer movement. WAIT if no safe movement is available."
            )
            questions["action"]["criteria"] = {
                "WAIT": "No escape currently available",
                **options,
            }
        else:
            questions["action"]["instructions"] = (
                "Choose a useful action to defeat the authorized creature. An offered ATTACK is already legal and can hit now: moving closer is unnecessary to hit. Do not repeatedly switch weapons or approach while in range. Return WAIT only if no useful action."
            )
            if not any((v["kind"] == "ATTACK" for v in options.values())):
                questions["action"]["instructions"] = (
                    "The current enemy is OUT OF ATTACK RANGE or weapon cooldown is not ready. Choose a legal move bringing it into reach using enemy distance after moving, or WAIT only for cooldown when already near. Do not keep switching weapons or waiting while the enemy remains far away."
                )
        descriptions = {}
        for key, row in options.items():
            if row["kind"] == "ATTACK":
                value = f"ATTACK {row.get('entity_kind', 'authorized creature')} NOW with held weapon: in reach and cooldown ready; performs a hit."
            elif row["kind"] == "SELECT":
                value = f"SWITCH held weapon to {row['item_id']}; no attack. Damage statistic {row.get('damage')}"
            else:
                current_distance = min(
                    (e.get("distance", 1000) for e in entities), default=1000
                )
                distance = row.get("distance_to_nearest_enemy", 1000)
                relation = (
                    "closer to enemy"
                    if distance < current_distance
                    else "farther from enemy"
                )
                value = f"MOVE {relation}, enemy distance {current_distance:.1f} becomes {distance:.1f}, target {row.get('target')}"
            descriptions[key] = value
        questions["action"]["criteria"] = {"WAIT": "Wait", **descriptions}
        battle_state = {
            "user_goal": task.get("request"),
            "constraints": task.get("constraints", []),
            "hp": game["player"].get("hp"),
            "held": game["player"].get("wielded"),
            "enemies": entities,
            "engagement": mode,
            "recent": list(recent)[-3:],
        }
        a = await controller.judge_typed(
            "voxel_survival_step", battle_state, questions, deadline_ms=2000
        )
        a["engagement"] = engagement["engagement"]
        return a
    a = await controller.judge_typed(
        "voxel_survival_step",
        {
            "task": brief(task),
            "progress": {
                "newly_completed": task.get("completed", 0),
                "remaining_new_units": max(
                    0, task["quantity"] - task.get("completed", 0)
                )
                if task.get("quantity")
                else None,
                "existing_inventory_is_not_new_work": True,
            },
            "game": game,
            "recent": list(recent)[-8:],
        },
        questions,
        deadline_ms=2000,
    )
    if task.get("kind") == "COMBAT":
        a["engagement"] = engagement["engagement"]
    return a
