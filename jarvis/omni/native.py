from __future__ import annotations
import json
from pathlib import Path
from jarvis.core.schemas import Decision
from .decision.base import DecisionInput, Option, Question


def load_policy(path):
    path = Path(path).expanduser().resolve()
    policy = json.loads(path.read_text())
    route = Question.model_validate(policy["route"])
    if set(route.option_ids()) != {
        "FAST",
        "SCREEN",
        "COACH",
        "GENERAL",
        "PREFERENCE",
        "CORRECTION",
    }:
        raise ValueError("Native route policy changed the supported operation set")
    for key in [
        "constraint",
        "recall_fact",
        "delivery",
        "fast_eligible",
        "operation_recheck",
    ]:
        if not isinstance(policy[key], str) or not policy[key].strip():
            raise ValueError("Missing native interaction instruction")
    if "preference_scope" in policy and (
        not isinstance(policy["preference_scope"], str)
        or not policy["preference_scope"].strip()
    ):
        raise ValueError("Invalid native preference scope instruction")
    if any(
        (
            not isinstance(policy.get("uncertain_clarification", {}).get(lang), str)
            or not policy["uncertain_clarification"][lang].strip()
            for lang in ["zh", "en"]
        )
    ):
        raise ValueError("Missing neutral clarification text")
    return policy


def route_request(runtime, update):
    context = runtime.context()
    profile = [{"id": m["id"], "text": m["text"]} for m in runtime.profile]
    state = {
        "actual_user_microphone": update.text,
        "game": runtime.game,
        "screen_shared": True,
        "conversation_focus": runtime.dialogue_focus,
        "recent_exchange": context.get("history", [])[-6:],
        "user_profile": profile,
        "explicit_commentary_preferences": runtime.preferences,
        "tasks": context.get("tasks", []),
        "playback_active": context.get("playback_active", False),
        "paused_speech": bool(runtime.paused_response),
        "language": runtime.response_language,
    }
    questions = [
        Question.model_validate(runtime.native_policy["route"]),
        Question(
            id="fast_eligible",
            primitive="noul",
            instructions=runtime.native_policy["fast_eligible"],
        ),
    ]
    if profile:
        options = (
            Option(id="NONE", text="No supplied fact applies."),
            *(Option(id=m["id"], text=m["text"]) for m in profile),
        )
        questions.extend(
            (
                Question(
                    id=key,
                    primitive="choice",
                    instructions=runtime.native_policy[key],
                    options=options,
                )
                for key in ["constraint", "recall_fact"]
            )
        )
    return DecisionInput(state=state, questions=tuple(questions))


def operation_recheck(request, policy):
    route = request.questions[0]
    followup = route.model_copy(
        update={
            "options": tuple((o for o in route.options if o.id != "FAST")),
            "instructions": route.instructions + " " + policy["operation_recheck"],
        }
    )
    return request.model_copy(update={"questions": (followup,)})


def fast_decision(runtime, update, epoch, result, playback_epoch):
    answers = {a.question_id: a for a in result.answers if a.primitive == "choice"}
    choices = {key: a.choice for key, a in answers.items()}
    confidences = {
        key: a.raw_probs[a.option_ids.index(a.choice)] for key, a in answers.items()
    }
    if (
        choices.get("constraint", "NONE") != "NONE"
        and confidences["constraint"] < runtime.config["jev"]["control_accept"]
    ):
        return None
    return Decision(
        utterance_id=update.utterance_id,
        revision=update.revision,
        decision_epoch=epoch,
        route="FAST",
        confidence=confidences["route"],
        raw={
            "choices": choices,
            "confidences": confidences,
            "playback_control_epoch": playback_epoch,
            "screen_native_route": True,
            "decision_input_hash": result.input_hash,
        },
    )


def delivery_context(runtime):
    preferences = [
        row for row in runtime.preferences if row.get("source") != "user_interface"
    ]
    state = {
        "game": runtime.game,
        "game_session_id": runtime.capture.game_session_id,
        "explicit_commentary_preferences": preferences,
    }
    return (
        runtime.native_policy["delivery"] + "\n" + json.dumps(state, ensure_ascii=False)
    )
