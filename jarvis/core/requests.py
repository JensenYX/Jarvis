import asyncio
import re

MARKER = re.compile(
    "(?:第[一二三四五六七八九十\\d]+(?:个|项)?任务(?:是)?\\s*[:：，,]?|\\b(?:first|second|third|fourth|fifth)\\s+task(?:\\s+is)?\\s*[:：,]?|\\btask\\s+[1-5]\\s*[:：])",
    re.I,
)


def enumerated_requests(text):
    markers = list(MARKER.finditer(text))
    if not 2 <= len(markers) <= 5:
        return []
    parts = [
        text[
            m.end() : markers[i + 1].start() if i + 1 < len(markers) else len(text)
        ].strip(" ，,；;。.")
        for i, m in enumerate(markers)
    ]
    return parts if all(parts) else []


async def route_batch(controller, transcript, epoch, context, parts):
    from jarvis.core.schemas import Decision
    from jarvis.core.interactive_route import ROUTES

    state = {
        "requests": parts,
        "user_profile": [
            {"id": m["id"], "text": m["text"]} for m in context.get("user_profile", [])
        ],
        "tasks": context.get("tasks", []),
        "history": context.get("history", [])[-2:],
        "tools": context.get("tools", []),
    }
    questions = {}
    for i in range(len(parts)):
        questions[f"route_{i}"] = {
            "instructions": f"Select the route for requests[{i}] only. Each request is independent and all must be handled. Saved constraints apply only to that request. Edits to saved files/tasks are CONTROL; current news/weather and creating files are WORKER. Ordinary dinner advice is FAST.",
            "criteria": ROUTES,
        }
        if state["user_profile"]:
            questions[f"constraint_{i}"] = {
                "instructions": f"Does requests[{i}] conflict with a saved personal restriction? Select that fact, or NONE. Asking to eat an allergen does not revoke an allergy. Unrelated requests have NONE.",
                "criteria": {
                    "NONE": "No conflict",
                    **{m["id"]: m["text"] for m in state["user_profile"]},
                },
            }
    try:
        for attempt in range(2):
            try:
                values, confidence, _ = await controller.judge(
                    "route_batch", state, questions, allow_fallback=False
                )
                break
            except Exception as error:
                if attempt:
                    raise
                controller.emit(
                    "batch.route_retry", error=type(error).__name__, attempt=1
                )
                await asyncio.sleep(0.1)
        items = [
            {
                "text": text,
                "route": values[f"route_{i}"],
                "confidence": confidence[f"route_{i}"],
                "constraint": values.get(f"constraint_{i}", "NONE"),
            }
            for i, text in enumerate(parts)
        ]
        if controller.config["runtime"].get("conversation_v3"):
            for i, item in enumerate(items):
                item["constraint_confidence"] = confidence.get(f"constraint_{i}", 0)
        return Decision(
            utterance_id=transcript.utterance_id,
            revision=transcript.revision,
            decision_epoch=epoch,
            route="BATCH",
            confidence=min((item["confidence"] for item in items)),
            raw={"items": items},
        )
    except Exception as error:
        controller.emit("batch.route_failed", error=type(error).__name__)
        return Decision(
            utterance_id=transcript.utterance_id,
            revision=transcript.revision,
            decision_epoch=epoch,
            route="BATCH",
            confidence=0,
            decision_origin="terminal_fallback",
            raw={
                "items": [
                    {
                        "text": part,
                        "route": "WORKER",
                        "confidence": 0,
                        "constraint": "NONE",
                    }
                    for part in parts
                ]
            },
        )
