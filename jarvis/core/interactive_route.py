from jarvis.core.schemas import Decision

ROUTES = {
    "FAST": "Ordinary conversation, common knowledge, capabilities, acknowledgement, or a natural follow-up question. No external work is needed yet.",
    "WORKER": "A concrete independent task needs tools/current external facts, exact calculation, code generation, or complex reasoning.",
    "MEMORY": "A personal fact from an earlier session is needed and absent from current history.",
    "CONTROL": "Manage an existing task or playback; hear its saved result or recall its request.",
}


async def route(controller, transcript, epoch, context):
    cfg = controller.config["jev"]
    if controller.config["runtime"].get("playback_control_v2"):
        from jarvis.core.playback_control import resolve

        playback = await resolve(controller, transcript, epoch, context)
        if playback:
            return playback
    if controller.config["runtime"].get("batch_requests"):
        from jarvis.core.requests import enumerated_requests, route_batch

        parts = enumerated_requests(transcript.text)
        if parts:
            return await route_batch(controller, transcript, epoch, context, parts)
    state = {
        "input": transcript.model_dump(),
        "history": context.get("history", [])[-6:],
        "tasks": context.get("tasks", []),
        "tools": context.get("tools", []),
        "playback_active": context.get("playback_active", False),
        "language": context.get("language", "auto"),
    }
    if controller.config["memory"].get("profile_context"):
        state["user_profile"] = context.get("user_profile", [])
    if controller.config["runtime"].get("task_orchestration"):
        state["input"] = {"text": transcript.text, "language": transcript.language}
        state["user_profile"] = [
            {"id": m["id"], "text": m["text"]} for m in state.get("user_profile", [])
        ]
    questions = {
        "route": {"instructions": controller.prompt("route"), "criteria": ROUTES}
    }
    if controller.config["runtime"].get("task_orchestration"):
        questions["route"]["criteria"] = {
            **ROUTES,
            "FAST": "Ordinary conversation or applying facts ALREADY present in user_profile. Answer a food/suitability question using the supplied restrictions directly; no new retrieval is needed.",
            "MEMORY": "A personal fact must be retrieved because it is absent from BOTH history AND user_profile. Never choose this just to apply a supplied personal fact. Saved task/game/file recall is CONTROL.",
            "CONTROL": "Refer to an existing task or saved game/file: recall/remember it, hear its result, check status, modify or cancel it. Asking whether you remember a previously generated game is CONTROL even without an edit. Only this path can read the task/file records.",
        }
    if controller.config["runtime"].get("task_dialogue"):
        questions["route"]["criteria"]["CONTROL"] = (
            "Refer to prior requests: what did I ask, what are you working on, progress, first/second/remaining task, previous game, resume/modify/cancel. These use saved task records, including unfinished multi-task items. No new goal is required."
        )
        state["tasks"] = [
            {
                "request": r["request"],
                "status": r["status"],
                "batch_item": r["batch_item"],
            }
            for r in context.get("task_dialogue", {}).get("state", [])[:5]
        ]
        if state["tasks"]:
            questions["reference"] = {
                "instructions": "Does this utterance refer back to an existing request rather than introduce an independent new goal? Include fragments identifying a previous task, progress, 'there is still one task left' after a batch, and numbered follow-ups. An unrelated new question is NEW.",
                "criteria": {
                    "EXISTING": "Refers to previously requested work.",
                    "NEW": "New goal, ordinary chat, or no identified task reference.",
                },
            }
    if controller.config["runtime"].get("task_interaction_v2"):
        working = [
            r
            for r in context.get("task_dialogue", {}).get("state", [])
            if r.get("current_session")
        ]
        if working:
            state["tasks"] = [
                {
                    "request": r["request"],
                    "status": r["status"],
                    "batch_item": r["batch_item"],
                }
                for r in working[:5]
            ]
        questions["route"]["criteria"]["HISTORY"] = (
            "Explicitly delete/clear saved conversation, task history, or personal memory. This is an operation, never a request to list current tasks."
        )
        questions["route"]["instructions"] += (
            "\nA similar topic in old tasks does NOT make a new question CONTROL. Answer what to eat tonight directly (FAST); only explicit progress/recall/change/result references are CONTROL. A request to clear history is HISTORY even if no tasks exist."
        )
        if "reference" in questions:
            questions["reference"]["instructions"] += (
                " Mere topic overlap, or repeating an unanswered everyday question, is NEW. Only an explicit reference to prior work is EXISTING. Deletion of all history is NEW here; the HISTORY route handles it."
            )
    if controller.config["runtime"].get("conversation_v3"):
        questions.pop("reference", None)
        state["conversation_focus"] = context.get("conversation_focus", {})
        questions["route"]["criteria"]["FAST"] = (
            "Conversation, emotional support, everyday advice, stable facts, simple arithmetic, clarifying or following up the spoken answer, or stating/applying/recalling personal facts already in user_profile. No new external work is needed."
        )
        questions["route"]["criteria"]["CONTROL"] = (
            "Explicit operation on a tracked task/file: status, list requests, read completed results, retry, modify, cancel, or stop speech ONLY. Conversational follow-ups and personal restrictions are FAST, even if they refer to a prior reply."
        )
        questions["route"]["instructions"] += (
            "\nUse conversation_focus for elliptical follow-ups. A correction of dinner advice (but I am allergic to garlic), yes after an acknowledgement, or asking what personal restrictions you mentioned are FAST/IGNORE, not task operations. Stop that topic followed by a substantive new question should answer the new question. Merely using a pronoun or mentioning a prior reply is not CONTROL."
        )
        if state.get("user_profile"):
            questions["recall_fact"] = {
                "instructions": "Is the user explicitly asking which saved personal fact/restriction was just mentioned? Select the fact matching the most recent conversation_focus answer and recent exchange. For 刚才的限制是什么 choose the restriction from that immediate exchange, not an older unrelated allergy. NONE for recommendations, why a recommendation is good, stating a fact, or no explicit personal-fact recall.",
                "criteria": {
                    "NONE": "Not asking to recall a specific provided personal fact.",
                    **{m["id"]: m["text"] for m in state["user_profile"]},
                },
            }
    if controller.config["runtime"].get("conversational_delivery"):
        questions["route"]["criteria"] = {
            **questions["route"]["criteria"],
            "IGNORE": "Only acknowledges an answer, says to continue existing background work unchanged, or is clearly background/other-directed speech or an unintelligible unrelated fragment. No new question, constraint, request for progress, or request to hear a result. No spoken reply is needed.",
        }
    if controller.config["runtime"].get("batch_requests"):
        questions["route"]["criteria"] = {
            **questions["route"]["criteria"],
            "BATCH": "Two or more independent requests in one utterance, each needs its own answer or task; do not select only the last request. Not merely several constraints of one task.",
        }
    if controller.config["runtime"].get("targeted_task_recall"):
        questions["route"]["criteria"]["CONTROL"] = (
            "Recall or identify earlier work/files by subject (which games did you make, which Python code, only the game not all tasks); check progress, read a result, modify, retry or cancel existing tasks; stop playback. Saved file identification is supported, including natural follow-ups about its name."
        )
        if state["tasks"]:
            questions["saved_work_reference"] = {
                "instructions": "Is input asking to identify/retrieve work or a file the assistant previously produced or was asked to produce? This includes what games did you design for me, which saved code, and the name of the just-mentioned saved project. NO for everyday conversation, explaining dinner advice, personal facts, or creating a new project.",
                "criteria": {
                    "YES": "Identify or retrieve existing work from task records.",
                    "NO": "No such saved-work lookup.",
                },
            }
    if (
        controller.config["runtime"].get("reversible_interruptions")
        and state["playback_active"]
        and (not controller.config["runtime"].get("playback_control_v2"))
    ):
        questions["turn"] = {
            "instructions": "The assistant has paused an unfinished spoken answer for this sound. Does the latest utterance clearly address the assistant with a request, correction, question or stop command? Short but meaningful follow-ups count. Unrelated fragments, background conversation and acknowledgements should let the unfinished answer continue. Uncertainty is not a reason to discard the answer and ask a new clarification question.",
            "criteria": {
                "INTERRUPT": "Clear assistant-directed request or stop; replace the unfinished answer.",
                "CONTINUE": "No clear new assistant-directed request; resume the unfinished answer.",
            },
        }
    if state.get("user_profile"):
        questions["constraint"] = {
            "instructions": "Does the latest request propose a choice/action incompatible with an explicit saved personal constraint? Compare across languages and implicit applications (food, preferences, accessibility, budget, etc.). Select the conflicting fact, not a general preference. Asking to eat something does not revoke an allergy. An explicit correction/revocation of a saved fact is not a request to violate it. NONE for no actual conflict, ordinary conversation, or simply stating a fact to remember. This question is independent of the route.",
            "criteria": {
                "NONE": "No explicit saved personal constraint rules out the requested choice.",
                **{m["id"]: m["text"] for m in state["user_profile"]},
            },
        }
    base = dict(
        utterance_id=transcript.utterance_id,
        revision=transcript.revision,
        decision_epoch=epoch,
    )
    if controller.config["runtime"].get("playback_control_v2"):
        from jarvis.core.playback_control import compact_route_state

        compact_route_state(state, questions)
    try:
        values, confidence, origin = await controller.judge(
            "route", state, questions, allow_fallback=False
        )
        if (
            values.get("saved_work_reference") == "YES"
            and confidence.get("saved_work_reference", 0) >= cfg["control_accept"]
        ):
            values["route"] = "CONTROL"
            confidence["route"] = confidence["saved_work_reference"]
        if (
            values.get("recall_fact", "NONE") != "NONE"
            and confidence.get("recall_fact", 0) >= cfg["control_accept"]
            and (values["route"] in {"FAST", "MEMORY"})
        ):
            return Decision(
                **base,
                route="FAST",
                confidence=confidence["recall_fact"],
                raw={
                    "choices": values,
                    "confidences": confidence,
                    "reason": "provided_personal_fact_recall",
                },
            )
        if (
            values["route"] != "HISTORY"
            and values.get("reference") == "EXISTING"
            and (confidence.get("reference", 0) >= cfg["control_accept"])
        ):
            values["route"] = "CONTROL"
            confidence["route"] = confidence["reference"]
        if "turn" in questions and (
            values["route"] == "IGNORE"
            or (
                confidence["route"] < cfg["route_accept"]
                and values["turn"] == "CONTINUE"
            )
        ):
            return Decision(
                **base,
                route="IGNORE",
                confidence=confidence["turn"],
                raw={
                    "choices": values,
                    "confidences": confidence,
                    "reason": "no_confirmed_interruption",
                },
            )
        if confidence["route"] < cfg["route_accept"]:
            check = {
                **questions,
                "route": {
                    "instructions": controller.prompt("route")
                    + "\nResolve the latest request itself. Current public facts require WORKER even if unrelated old tasks exist. A request to hear an existing result requires CONTROL. Do not treat a general capability question as a task.",
                    "criteria": questions["route"]["criteria"],
                },
            }
            if (
                controller.config["runtime"].get("task_dialogue")
                and values["route"] == "WORKER"
            ):
                check["standalone_work"] = {
                    "instructions": "Does input itself explicitly request external lookup, calculation, or creation of a usable artifact, with enough information to begin? A request to design a small game is sufficient: optional genre details need not be asked. Merely having older tasks about the same subject does not make this a reference. NO for a capability-only question, vague pronoun/ordinal referring to old work, or an explicit edit/cancel of an existing task.",
                    "criteria": {
                        "YES": "Explicit self-contained work; the bounded read-only/file worker can start.",
                        "NO": "No self-contained new work objective.",
                    },
                }
                if controller.config["runtime"].get("task_interaction_v2"):
                    check["standalone_work"]["instructions"] += (
                        " A request for another game (再做一个 / also make a game) with its own name is YES even if a saved old game has that name. Applying an existing preference to new work is also YES."
                    )
            if (
                controller.config["runtime"].get("task_orchestration")
                and values.get("constraint", "NONE") != "NONE"
                and (confidence.get("constraint", 0) >= cfg["control_accept"])
            ):
                check["constraint_reply"] = {
                    "instructions": "Would directly applying the selected saved restriction answer the user's immediate proposed choice, without dropping a separate task? Dinner advice and whether that proposed food is suitable form one request. A separate search, code task or other independent request requires NO.",
                    "criteria": {
                        "YES": "The saved restriction directly answers this request; no separate task would be dropped.",
                        "NO": "Other independent work remains, or the restriction does not resolve the request.",
                    },
                }
            values, confidence, origin = await controller.judge(
                "route_recheck", state, check, allow_fallback=False
            )
            if (
                values.get("saved_work_reference") == "YES"
                and confidence.get("saved_work_reference", 0) >= cfg["control_accept"]
            ):
                values["route"] = "CONTROL"
                confidence["route"] = confidence["saved_work_reference"]
            if (
                values["route"] != "HISTORY"
                and values.get("reference") == "EXISTING"
                and (confidence.get("reference", 0) >= cfg["control_accept"])
            ):
                values["route"] = "CONTROL"
                confidence["route"] = confidence["reference"]
            if (
                values["route"] == "WORKER"
                and values.get("standalone_work") == "YES"
                and (confidence.get("standalone_work", 0) >= cfg["control_accept"])
            ):
                return Decision(
                    **base,
                    route="WORKER",
                    confidence=confidence["standalone_work"],
                    raw={
                        "choices": values,
                        "confidences": confidence,
                        "reason": "explicit_self_contained_work",
                    },
                )
            if (
                values.get("constraint_reply") == "YES"
                and confidence.get("constraint_reply", 0) >= cfg["control_accept"]
                and (values.get("constraint", "NONE") != "NONE")
                and (confidence.get("constraint", 0) >= cfg["control_accept"])
                and (values["route"] in {"FAST", "MEMORY"})
            ):
                return Decision(
                    **base,
                    route="FAST",
                    confidence=min(
                        confidence["constraint_reply"], confidence["constraint"]
                    ),
                    raw={
                        "choices": values,
                        "confidences": confidence,
                        "reason": "explicit_constraint_answers_request",
                    },
                )
        if confidence["route"] < cfg["route_accept"] and (
            not (
                controller.config["runtime"].get("task_orchestration")
                and values["route"] == "CONTROL"
                and (
                    state["tasks"]
                    or controller.config["runtime"].get("task_interaction_v2")
                )
            )
        ):
            if (
                controller.config["runtime"].get("task_dialogue")
                and values.get("reference") == "EXISTING"
            ):
                from jarvis.core.task_dialogue import bound_reference, resolve

                if bound_reference(transcript.text, context["task_dialogue"]):
                    return await resolve(controller, transcript, epoch, context)
            return Decision(
                **base,
                route="CLARIFY",
                confidence=confidence["route"],
                missing_slot="GOAL",
                raw={"uncertain_route": values},
            )
        choice = values["route"]
        if (
            choice == "IGNORE"
            and controller.config["runtime"].get("playback_control_v2")
            and state["playback_active"]
        ):
            from jarvis.core.playback_control import decision

            return decision(
                transcript,
                epoch,
                "HOLD_SPEECH",
                context.get("active_playback"),
                confidence=0,
                origin="terminal_fallback",
                reason="unconfirmed_playback_continuation",
            )
        if choice == "HISTORY":
            from jarvis.core.history_control import resolve

            return await resolve(controller, transcript, epoch, context)
        raw = {
            "choices": values,
            "confidences": confidence,
            "interactive_routing": True,
        }
        if (
            values.get("constraint", "NONE") != "NONE"
            and confidence.get("constraint", 0) < cfg["control_accept"]
        ):
            return Decision(
                **base,
                route="CLARIFY",
                confidence=confidence.get("constraint", 0),
                missing_slot="PERSONAL_CONSTRAINT",
                raw=raw,
            )
        if choice != "CONTROL":
            return Decision(
                **base, route=choice, confidence=confidence["route"], raw=raw
            )
        if controller.config["runtime"].get("task_dialogue"):
            from jarvis.core.task_dialogue import resolve

            return await resolve(controller, transcript, epoch, context)
        if controller.config["runtime"].get("task_orchestration"):
            actions = {
                "NONE": "No identified action",
                "READ": "Hear the result or check progress",
                "RECALL": "Recall the original task/file",
                "REFINE": "Modify the existing task or file, including a completed game",
                "REPLACE": "Replace the goal",
                "CANCEL": "Cancel the task",
                "STOP_SPEAKING": "Stop voice playback only",
                "OVERVIEW": "List current background activity",
            }
            targets = {
                "NONE": "No specific task",
                **{
                    f"{t['task_id']}:{t['version']}": f"Saved task at tasks[{i}]; use its objective, status and files from state."
                    for i, t in enumerate(state["tasks"])
                },
            }
            selected, probability, _ = await controller.judge(
                "control",
                state,
                {
                    "action": {
                        "instructions": "Select the user's operation on an existing task. Changing game speed is REFINE; asking whether you remember the game is RECALL. READ covers results/progress. These operations may target completed tasks.",
                        "criteria": actions,
                    },
                    "target": {
                        "instructions": "Which saved task does the latest reference identify, across languages? A reference to the game/file you gave me identifies the task with the actual matching saved file, rather than a failed later attempt with no saved file. Match the subject first. If several saved files have the same subject and the user does not specify an older one, prefer the newest by created_at/version. NONE for no specific task.",
                        "criteria": targets,
                    },
                },
                allow_fallback=False,
            )
            action, target = (selected["action"], selected["target"])
            threshold = (
                cfg["control_accept"]
                if action in {"CANCEL", "REFINE", "REPLACE"}
                else cfg["route_accept"]
            )
            certain = probability["action"] >= threshold and (
                action in {"STOP_SPEAKING", "OVERVIEW"}
                or (target != "NONE" and probability["target"] >= threshold)
            )
            if not certain or action == "NONE":
                return Decision(
                    **base,
                    route="CLARIFY",
                    confidence=min(probability.values()),
                    missing_slot="TARGET",
                )
            parts = target.split(":")
            return Decision(
                **base,
                route="CONTROL",
                confidence=min(probability.values()),
                control="STATUS" if action == "READ" else action,
                target_task_id=parts[0] if target != "NONE" else None,
                target_version=int(parts[1]) if target != "NONE" else None,
                raw=raw,
            )
        options = {
            "NONE": "No existing task or playback command is identified.",
            "STOP_SPEAKING": "Stop speaking only, without cancelling background work.",
            "OVERVIEW": "Ask what background work is currently active in general, not the progress/result of one specific task.",
        }
        for task in state["tasks"]:
            key = f"{task['task_id']}:{task['version']}"
            for action, text in {
                "READ": "Ask progress OR hear this task's result; if ready, give the result",
                "RECALL": "Repeat what the user originally requested, not the answer",
                "REFINE": "Change constraints of this task",
                "REPLACE": "Replace this task's goal",
                "CANCEL": "Cancel this task without a replacement",
            }.items():
                options[f"{action}:{key}"] = (
                    f"{text}: {task['objective'][:180]} (status {task['status']})"
                )
        values, confidence, _ = await controller.judge(
            "control",
            state,
            {
                "control": {
                    "instructions": "Select the exact action and task from the latest utterance and conversation. READ covers both asking progress and asking to hear a result. RECALL is only asking what the original request was. Prefer the most recent matching substantive task; an earlier request merely asking for help is not the mathematical expression itself. OVERVIEW covers a general question about current activity. If no intended task can be identified, NONE. Never cancel a task merely because the user stops playback.",
                    "criteria": options,
                }
            },
            allow_fallback=False,
            threshold=cfg["control_accept"],
        )
        parts = values["control"].split(":")
        threshold = (
            cfg["control_accept"]
            if parts[0] in {"CANCEL", "REFINE", "REPLACE"}
            else cfg["route_accept"]
        )
        if confidence["control"] < threshold or parts[0] == "NONE":
            return Decision(
                **base,
                route="CLARIFY",
                confidence=confidence["control"],
                missing_slot="TARGET",
            )
        return Decision(
            **base,
            route="CONTROL",
            confidence=confidence["control"],
            control="STATUS" if parts[0] == "READ" else parts[0],
            target_task_id=parts[1] if len(parts) == 3 else None,
            target_version=int(parts[2]) if len(parts) == 3 else None,
            task_relation=parts[0] if parts[0] in {"REFINE", "REPLACE"} else "NONE",
            raw=raw,
        )
    except Exception as error:
        controller.emit("route.unavailable", error=type(error).__name__)
        if (
            controller.config["runtime"].get("playback_control_v2")
            and state["playback_active"]
        ):
            from jarvis.core.playback_control import decision

            return decision(
                transcript,
                epoch,
                "HOLD_SPEECH",
                context.get("active_playback"),
                confidence=0,
                origin="terminal_fallback",
                reason="routing_unavailable",
            )
        if "turn" in questions:
            return Decision(
                **base,
                route="IGNORE",
                confidence=0,
                decision_origin="terminal_fallback",
                raw={"reason": "routing_unavailable_preserve_unfinished_answer"},
            )
        return Decision(
            **base,
            route="CLARIFY",
            confidence=0,
            missing_slot="routing_unavailable",
            decision_origin="terminal_fallback",
        )
