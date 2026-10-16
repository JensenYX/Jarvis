from jarvis.core.schemas import Decision


async def route(controller, transcript, epoch, context, *, allow_batch=True):
    tasks = context.get("tasks", [])[-12:]
    criteria = {
        "ANSWER": "Respond to the latest request using conversation, pending questions, policy and tools. Includes corrections, names, spelled identifiers, yes/no answers, and continuing a domain transaction.",
        "MEMORY": "Retrieve a personal fact from an earlier session that is absent from the supplied history and profile.",
    }
    actions = {
        "CANCEL": "Cancel this background task without replacement.",
        "REFINE": "Modify the requirements of this background task or its saved artifact.",
        "STATUS": "Report progress or read the saved result of this background task.",
        "RESUME": "Repeat the interrupted result of this background task.",
    }
    criteria["SAVE_MEMORY"] = (
        "Explicitly save a durable personal fact or preference; no independent task or lookup."
    )
    if allow_batch:
        criteria["BATCH"] = (
            "Two or more independent operations in this utterance. Keep all constraints about a single task together."
        )
    for task in tasks:
        key = f"{task['task_id']}:{task['version']}"
        for action, description in actions.items():
            criteria[f"{action}:{key}"] = (
                f"{description} Task: {task['objective'][:160]} ({task['status']})."
            )
    if context.get("playback_active"):
        criteria["STOP_SPEAKING"] = (
            "Stop the current voice playback only; preserve background work."
        )
    state = {
        "input": transcript.text,
        "history": context.get("history", [])[-12:],
        "pending_questions": context.get("pending_questions", []),
        "user_profile": context.get("user_profile", []),
        "tools": context.get("tools", []),
    }
    question = {
        "operation": {
            "instructions": "This user turn has already been admitted. Select the next harness operation. ANSWER lets the worker answer, use available tools, or ask the specific missing detail; it does not require a standalone new goal. A reply to an assistant question is ANSWER, even if only a name, number, yes, no or correction. Cancelling a booking/order with domain tools is ANSWER. Task controls only manage the assistant background jobs listed in the choices. An amendment to an existing generated file is REFINE. Do not cancel/refine a background job merely to supply a requested detail. Use MEMORY only for a fact actually absent from current history and profile.",
            "criteria": criteria,
        }
    }
    base = dict(
        utterance_id=transcript.utterance_id,
        revision=transcript.revision,
        decision_epoch=epoch,
    )
    try:
        values, probabilities, origin = await controller.judge(
            "delegated_operation",
            state,
            question,
            allow_fallback=False,
            deadline_ms=controller.config["jev"].get("delegated_deadline_ms", 3500),
        )
        operation = values["operation"]
        confidence = probabilities["operation"]
    except Exception as error:
        controller.emit(
            "native.harness.operation_unavailable",
            error=type(error).__name__,
            resolution="contextual_worker",
            tasks_preserved=True,
        )
        return Decision(
            **base,
            route="WORKER",
            confidence=0,
            decision_origin="terminal_fallback",
            raw={"admitted_fallback": True},
        )
    raw = {"choices": values, "confidences": probabilities, "native_admitted": True}
    if operation == "ANSWER":
        return Decision(
            **base,
            route="WORKER",
            confidence=confidence,
            decision_origin=origin,
            raw=raw,
        )
    if confidence < controller.config["jev"]["control_accept"]:
        return Decision(
            **base,
            route="WORKER",
            confidence=confidence,
            decision_origin=origin,
            raw={**raw, "uncertain_operation": operation},
        )
    if operation == "BATCH":
        return Decision(
            **base,
            route="BATCH",
            confidence=confidence,
            decision_origin=origin,
            raw={**raw, "typed_batch": True},
        )
    if operation == "SAVE_MEMORY":
        return Decision(
            **base,
            route="CONTROL",
            control="SAVE_MEMORY",
            confidence=confidence,
            decision_origin=origin,
            raw=raw,
        )
    if operation == "MEMORY":
        return Decision(
            **base,
            route="MEMORY",
            confidence=confidence,
            decision_origin=origin,
            raw=raw,
        )
    parts = operation.split(":")
    return Decision(
        **base,
        route="CONTROL",
        control=parts[0],
        target_task_id=parts[1] if len(parts) == 3 else None,
        target_version=int(parts[2]) if len(parts) == 3 else None,
        confidence=confidence,
        decision_origin=origin,
        raw=raw,
    )
