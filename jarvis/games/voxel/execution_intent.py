async def check(controller, text, record=None):
    try:
        for attempt in range(2):
            try:
                answer = await controller.judge_typed(
                    "voxel_explicit_execution",
                    {
                        "current_user_words": text,
                        "referenced_offer": {
                            k: record[k]
                            for k in ("request", "presented_text", "owner")
                            if k in (record or {})
                        },
                    },
                    {
                        "mode": {
                            "type": "choice",
                            "instructions": "Classify ONLY what the current user asks the companion to do now. Asking to check inventory/materials, whether something is possible, enough, useful, safe, or how to do it requests INFORMATION, even if it names a craftable item and a quantity. Inspect/check/look does not authorize the discussed action. A polite request such as can you make me a sword, help me craft, do that now, or an explicit accepted offer asks EXECUTION. Do not infer execution from a previous command. A request to revise an active action is EXECUTION only when it actually asks to continue performing it.",
                            "criteria": {
                                "INFORMATION": "Only inspect, assess feasibility, explain, propose, or give advice; no world modification",
                                "EXECUTION": "Explicitly requests the companion perform the game action now",
                                "UNCLEAR": "Neither meaning is sufficiently clear",
                            },
                        }
                    },
                    deadline_ms=3000,
                )
                break
            except TimeoutError:
                if attempt:
                    raise
        selected = answer["mode"]
        confidence = float(selected["confidence"])
        if (
            selected["choice"] not in {"INFORMATION", "EXECUTION", "UNCLEAR"}
            or not 0 <= confidence <= 1
        ):
            raise ValueError("Invalid execution judgment")
        selected = {**selected, "confidence": confidence}
    except (TimeoutError, ValueError, KeyError, TypeError) as exc:
        selected = {
            "choice": "UNCLEAR",
            "confidence": 0.0,
            "reason": type(exc).__name__,
        }
    authorized = selected["choice"] == "EXECUTION" and selected["confidence"] >= 0.8
    return {"authorized": authorized, "judgment": selected}
