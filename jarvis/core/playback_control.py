import re
from jarvis.core.schemas import Decision


def literal_action(text):
    normalized = re.sub("[\\s，。！？,.!?]", "", text).lower()
    if normalized in {
        "停",
        "停下",
        "停下来",
        "停一下",
        "停止播报",
        "停止说话",
        "别说了",
        "先别说了",
        "不要再说了",
        "不要继续播报了",
        "你能不能停下来",
        "请停一下",
        "请停止播报",
        "stop",
        "stopplease",
        "pleasestop",
        "stopspeaking",
        "stoptalking",
        "canyoustop",
        "couldyoustopplease",
    }:
        return "STOP_SPEAKING"
    return None


def decision(
    transcript,
    epoch,
    action,
    playback=None,
    *,
    confidence=1,
    origin="local",
    reason=None,
):
    playback = playback or {}
    return Decision(
        utterance_id=transcript.utterance_id,
        revision=transcript.revision,
        decision_epoch=epoch,
        route="IGNORE" if action == "CONTINUE" else "CONTROL",
        control="NONE" if action == "CONTINUE" else action,
        confidence=confidence,
        decision_origin=origin,
        target_task_id=playback.get("task_id"),
        target_version=playback.get("version"),
        raw={
            "playback_control": True,
            "response_id": playback.get("response_id"),
            "reason": reason or action.lower(),
        },
    )


async def resolve(controller, transcript, epoch, context):
    active = context.get("active_playback", {})
    action = literal_action(transcript.text)
    if action:
        return decision(transcript, epoch, action, active)
    if not context.get("playback_active"):
        return None
    state = {"utterance": transcript.text, "paused_speech": active}
    questions = {
        "received": {
            "instructions": "Does utterance explicitly say the user already knows/heard this content or complain that the assistant is repeating it, with no additional request? Examples: 这个新闻我知道了; 你怎么又重复了; I already heard that. NO for bare okay/yes/mm, negation (I do not know it), requests to repeat/continue, or any separate substantive question. Evaluate the words in utterance; paused_speech is only the content it may refer to.",
            "criteria": {
                "YES": "Content already known/heard or repetition complaint; no additional request.",
                "NO": "Does not meet that condition.",
            },
        },
        "recipient": {
            "instructions": "Does utterance explicitly address a human bystander instead of the AI voice assistant? The assistant has no human name and cannot perform physical errands. Default ASSISTANT unless there is clear evidence of another addressee.",
            "criteria": {
                "BYSTANDER": "Clearly addresses a different human, e.g. a named coworker doing physical work.",
                "ASSISTANT": "Addresses the assistant, or no clear different addressee.",
            },
        },
        "turn": {
            "instructions": "What does utterance ask the assistant to do with its paused_speech? Judge the utterance, not the request stored in paused_speech. A substantive new question takes priority over a stop in the same utterance. The assistant is not a human coworker or bystander. A command explicitly addressed to another person is CONTINUE, even if it contains the word stop. Respect negation and distinguish quoted commands from commands to you.",
            "criteria": {
                "END": "User wants the speech to end: stop/be quiet; already knows this result; has heard it before; complains you are repeating. No separate question needing an answer.",
                "CONTINUE": "User says okay/yes/mm, continue, do not stop, or speaks to someone else. No new request and no complaint about this speech.",
                "NEW_TURN": "User asks a substantive question, requests a task, corrects facts, or asks for an explanation. Needs a new response.",
            },
        },
        "scope": {
            "instructions": "IF utterance asks to end the paused speech, is it a general stop or dismissal of this particular content? A general stop/be quiet command is ALL, even though only one speech is playing. Already knowing/hearing this result or complaining it is repeated is CURRENT. This conditional question does not decide whether the speech should end.",
            "criteria": {
                "ALL": "General stop/pause/be quiet, or stop all announcements.",
                "CURRENT": "Dismiss the current content because it is known/repeated.",
            },
        },
    }
    try:
        values, scores, origin = await controller.judge(
            "playback_control", state, questions, allow_fallback=False
        )
        if (
            values["recipient"] == "BYSTANDER"
            and scores["recipient"] >= controller.config["jev"]["control_accept"]
        ):
            return decision(
                transcript,
                epoch,
                "CONTINUE",
                active,
                confidence=scores["recipient"],
                origin=origin,
            )
        if (
            values["received"] == "YES"
            and scores["received"] >= controller.config["jev"]["control_accept"]
        ):
            action = (
                "STOP_SPEAKING"
                if values["scope"] == "ALL"
                and scores["scope"] >= controller.config["jev"]["control_accept"]
                else "DISMISS_SPEECH"
            )
            return decision(
                transcript,
                epoch,
                action,
                active,
                confidence=scores["received"],
                origin=origin,
            )
        action = values["turn"]
        if action == "NEW_TURN":
            return None
        if scores["turn"] >= controller.config["jev"]["control_accept"]:
            if action == "END":
                action = (
                    "STOP_SPEAKING"
                    if values["scope"] == "ALL"
                    and scores["scope"] >= controller.config["jev"]["control_accept"]
                    else "DISMISS_SPEECH"
                )
            return decision(
                transcript,
                epoch,
                action,
                active,
                confidence=scores["turn"],
                origin=origin,
            )
        return None
    except Exception as error:
        controller.emit("playback.control_unavailable", error=type(error).__name__)
        return decision(
            transcript,
            epoch,
            "HOLD_SPEECH",
            active,
            confidence=0,
            origin="terminal_fallback",
            reason="playback_control_unavailable",
        )


def compact_route_state(state, questions=None):
    state["tools"] = [
        t.get("name", t.get("id", "tool")) if isinstance(t, dict) else t
        for t in state["tools"]
    ]
    state["tasks"] = [
        {**t, "request": t.get("request", t.get("objective", ""))[:100]}
        for t in state["tasks"][:3]
    ]
    focus = state.get("conversation_focus", {})
    state["conversation_focus"] = {
        k: v[:160] if isinstance(v, str) else v for k, v in focus.items()
    }
    for kind in ("constraint", "recall_fact"):
        if questions and kind in questions:
            options = questions[kind]["criteria"]
            for i, fact in enumerate(state.get("user_profile", [])):
                if fact["id"] in options:
                    options[fact["id"]] = f"The saved fact at user_profile[{i}]."
