from __future__ import annotations
import hashlib
import time
from .capture import now_ms
from .decision.base import DecisionInput, Option, Question
from .protocol import Observation, MediaStamp
from .memory import Scope

STYLE_TEXT = {
    "quiet": "这局减少主动发言，只在我提问时回答。",
    "brief": "这局回答简短，优先一句话说明。",
    "balanced": "这局按事件价值适量交流，避免重复和不必要插话。",
    "active": "这局可以多互动，但我说话或操作繁忙时等合适时机。",
}


async def interpret(runtime, update, kind):
    scope = runtime.memory_scope()
    rows = (
        await runtime.screen_memory.candidates(scope, before_ms=now_ms(), limit=16)
        if kind == "CORRECTION"
        else []
    )
    if kind == "CORRECTION":
        rows += await runtime.screen_memory.prior_events(
            scope, available_before=time.time(), limit=8
        )
    if kind == "CORRECTION" and (not rows):
        return {
            "scope": scope,
            "rows": [],
            "choice": "NONE",
            "accepted": False,
            "reason": "no_in_scope_candidates",
        }
    state = {
        "actual_user_microphone": update.text,
        "game": runtime.game,
        "current_game_session": scope.game_session_id,
        "events": [
            {
                k: row[k]
                for k in (
                    "id",
                    "text",
                    "fact_status",
                    "source",
                    "event_kind",
                    "game_session_id",
                )
            }
            for row in rows
        ],
        "local_preferences": runtime.local_preferences,
    }
    direct = Question(
        id="direct",
        primitive="noul",
        instructions="Is the user actually giving an instruction or correction to the assistant? An instruction may apply conditionally or to an upcoming situation; this still expresses direct intent. Discussing what a possible instruction would mean, merely quoting another speaker, a question without an instruction, or sarcasm does not authorize changing records. Judge the speech act, not whether its requested condition has happened yet.",
    )
    if kind == "PREFERENCE":
        questions = (
            direct,
            Question(
                id="scope",
                primitive="choice",
                instructions="What scope does the user explicitly intend? Judge the full request, including names and personal preferences. A preference that explicitly applies to future sessions is DURABLE even if its behavior has a condition. If the utterance combines any enduring personal fact/preference with a temporary instruction, choose DURABLE so the personal-memory model can separate them. Preserve genuinely temporary conditions such as only this round or until this fight ends. Do not turn a local instruction into a durable personality trait.",
                options=(
                    Option(
                        id="LOCAL",
                        text="Only a present game, round or explicitly bounded situation; no enduring personal fact or future-session preference.",
                    ),
                    Option(
                        id="DURABLE",
                        text="Includes an explicit enduring personal fact or preference across sessions, possibly conditional or mixed with a temporary instruction.",
                    ),
                    Option(
                        id="NONE", text="No clear commentary preference was stated."
                    ),
                ),
            ),
        )
        scope_guidance = (getattr(runtime, "native_policy", None) or {}).get(
            "preference_scope"
        )
        if scope_guidance:
            questions = (
                direct,
                questions[1].model_copy(update={"instructions": scope_guidance}),
            )
    else:
        questions = (
            direct,
            Question(
                id="target",
                primitive="choice",
                instructions="Which one stored event is the user explicitly correcting? Select NONE if no event matches unambiguously. The correction is the user report, not new visual proof. Never choose an unrelated event just because it is available.",
                options=tuple(
                    [Option(id="NONE", text="No unambiguous matching event")]
                    + [Option(id=row["id"], text=row["text"]) for row in rows]
                ),
            ),
        )
    decision = await runtime.policy.backend.decide(
        DecisionInput(state=state, questions=questions), timeout_s=4
    )
    answers = {answer.question_id: answer for answer in decision.answers}
    choice = answers["scope" if kind == "PREFERENCE" else "target"]
    probability = choice.raw_probs[choice.option_ids.index(choice.choice)]
    accepted = (
        answers["direct"].noul >= runtime.policy.threshold
        and probability >= runtime.policy.threshold
    )
    return {
        "scope": scope,
        "rows": rows,
        "choice": choice.choice,
        "accepted": accepted,
        "decision": decision,
    }


def reported_observation(runtime, update, at_ms):
    text = update.text.encode()
    return Observation(
        observation_id="asr:" + update.utterance_id + ":" + str(update.revision),
        source="user_mic",
        stamp=MediaStamp(
            game_session_id=runtime.capture.game_session_id,
            scene_epoch=runtime.capture.scene_epoch,
            sequence_id=update.revision,
            source_clock="server_asr_final_availability",
            source_capture_ts_ms=at_ms,
            capture_ts_ms=at_ms,
            receive_ts_ms=at_ms,
            clock_error_ms=0,
        ),
        evidence_refs=("asr_final:" + update.utterance_id,),
        visibility="player_audible",
        available_at_ms=at_ms,
        content_sha256=hashlib.sha256(text).hexdigest(),
        byte_length=len(text),
        mime_type="text/plain; charset=utf-8",
    )


async def apply(runtime, item):
    update, kind = (item["update"], item["kind"])
    current = lambda: (
        item["binding"] == runtime.binding()
        and item["epoch"] == runtime.screen_session_epoch
        and (item["playback_epoch"] == runtime.playback_control_epoch)
        and (update.utterance_id == runtime.current_utterance)
    )
    if not current():
        runtime.pending_personal_memory.pop(update.utterance_id, None)
        runtime.emit("screen.user_control.discarded", reason="newer_request_or_scene")
        return
    runtime.route_pending = False
    result = item.get("result")
    if not result or not result["accepted"] or result["choice"] == "NONE":
        runtime.pending_personal_memory.pop(update.utterance_id, None)
        runtime.clarify_screen(
            "我还没确定你的意思，可以再说明一下吗？",
            "I am not sure which preference or event you mean. Could you clarify?",
            update.utterance_id,
        )
        return
    runtime.emit(
        "screen.user_control.judged",
        kind=kind,
        decision=result["decision"].model_dump(mode="json"),
    )
    if kind == "PREFERENCE" and result["choice"] == "DURABLE":
        from jarvis.core.runtime import SessionRuntime

        runtime.release_personal_memory(update)
        runtime.final_routes.discard((update.utterance_id, update.revision))
        SessionRuntime.start_route(runtime, update)
        return
    runtime.pending_personal_memory.pop(update.utterance_id, None)
    observed = reported_observation(runtime, update, item["observed_at_ms"])
    target = (
        next((row for row in result["rows"] if row["id"] == result["choice"]), None)
        if kind == "CORRECTION"
        else None
    )
    if kind == "CORRECTION" and target is None:
        raise ValueError("Correction chose an unavailable event")
    scope = result["scope"]
    if target:
        if target["user_id"] != scope.user_id or target["game"] != scope.game:
            raise ValueError("Correction target escaped its user/game scope")
        scope = Scope(scope.user_id, scope.game, target["game_session_id"])
    identifier = await runtime.screen_memory.write(
        scope,
        text=update.text,
        fact_status="user_reported",
        event_kind="correction" if target else "local_preference",
        source="actual_user_microphone",
        scene_epoch=item["binding"][1],
        observed_at_ms=item["observed_at_ms"],
        initiated_at=item["initiated_at"],
        supersedes_id=target["id"] if target else None,
        evidence=[
            {
                "observation": observed.model_dump(mode="json"),
                "transcript": update.text,
                "game_event_time": "not inferred from ASR time",
                "superseded_text": target["text"] if target else None,
            }
        ],
    )
    if not identifier or not current():
        return
    if target:
        for key, (_, memory_id) in runtime.claim_memories.items():
            if memory_id == target["id"] and key in runtime.belief.claims:
                runtime.belief.invalidated[key] = "correction"
                runtime.belief.revision += 1
        runtime.emit(
            "screen.memory.corrected",
            memory_id=identifier,
            supersedes_id=target["id"],
            fact_status="user_reported",
            scope=scope.__dict__,
        )
        if runtime.native_policy:
            runtime.respond_to_screen_outcome(
                {
                    "status": "event_record_corrected",
                    "operation_completed": True,
                    "record_id": identifier,
                    "source": "actual_user_microphone",
                    "visual_confirmation": False,
                },
                update.utterance_id,
            )
        else:
            runtime.clarify_screen(
                "已按你的说明更正这条记录。",
                "I have corrected that record using your account.",
                update.utterance_id,
            )
    else:
        runtime.set_local_preference(
            update.text, source="actual_user_microphone", source_id=update.utterance_id
        )
        if runtime.native_policy:
            runtime.respond_to_screen_outcome(
                {
                    "status": "temporary_commentary_preference_applied",
                    "operation_completed": True,
                    "preference": update.text,
                    "scope": "current_game_with_original_user_conditions",
                },
                update.utterance_id,
            )
        else:
            runtime.clarify_screen(
                "好，按你说的来。",
                "Okay, I will follow that preference.",
                update.utterance_id,
            )
