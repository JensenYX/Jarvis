import asyncio
from pathlib import Path
from jarvis.core.schemas import Decision


async def resolve(controller, transcript, epoch, context):
    values, scores, _ = await controller.judge(
        "history_operation",
        {"input": transcript.text, "previous_turns": context.get("history", [])[-3:]},
        {
            "scope": {
                "instructions": "Use the latest explicit instruction. Chinese 你能不能把现在所有的历史记录都删掉 and English delete every history unambiguously mean HISTORY, not ALL: all modifies history, not personal facts. A question phrased as can you delete my history is a deletion request. Negations, hypothetical examples, asking what deletion means, and deleting game files are not consent. Generic history means conversation/task history, retaining separately saved personal facts. ALL requires explicitly including saved personal facts/preferences or everything you know about me.",
                "criteria": {
                    "HISTORY": "Delete/clear all conversation or task history.",
                    "ALL": "Delete history AND all saved personal facts/preferences.",
                    "UNSUPPORTED": "Selective deletion of one record/fact, external browser history or files; scope needs clarification.",
                    "NONE": "Not an actual deletion request.",
                },
            }
        },
        allow_fallback=False,
    )
    scope = values["scope"]
    score = scores["scope"]
    control = (
        "CLEAR_HISTORY"
        if score >= controller.config["jev"]["control_accept"]
        and scope in {"HISTORY", "ALL"}
        else "HISTORY_INFO"
        if scope == "NONE"
        else "HISTORY_CLARIFY"
    )
    return Decision(
        utterance_id=transcript.utterance_id,
        revision=transcript.revision,
        decision_epoch=epoch,
        route="CONTROL",
        control=control,
        confidence=score,
        raw={"scope": scope, "include_profile": scope == "ALL"},
    )


async def clear(runtime, include_profile=False):
    runtime.clear_playback("history_deleted")
    jobs = list(runtime.background | runtime.memory_jobs)
    for job in jobs:
        if job is not asyncio.current_task():
            job.cancel()
    await asyncio.gather(
        *(j for j in jobs if j is not asyncio.current_task()), return_exceptions=True
    )
    counts = await runtime.memory.clear_history(runtime.user_id, include_profile)
    runtime.history_audio_floor = max(
        [runtime.history_audio_floor]
        + [t.audio_end_sample for t in runtime.transcripts.values()]
    )
    from jarvis.core.speech_observations import clear_observations

    clear_observations(runtime)
    runtime.tasks.clear()
    runtime.results.clear()
    runtime.task_catalog.clear()
    runtime.task_focus_id = None
    runtime.matched_task_ids = []
    runtime.dialogue_focus = {}
    runtime.last_spoken_purpose = None
    runtime.history.clear()
    runtime.played.clear()
    runtime.transcripts.clear()
    runtime.decision_epochs.clear()
    runtime.logical_requests.clear()
    runtime.submitted.clear()
    runtime.committed.clear()
    runtime.jobs.clear()
    runtime.delivery_inflight.clear()
    runtime.delivery_attempts.clear()
    runtime.delivery_retry_scheduled.clear()
    runtime.requested_results.clear()
    runtime.result_speech_attempts.clear()
    runtime.memory_prefetch.clear()
    runtime.processed_batches.clear()
    runtime.interrupted_deliveries.clear()
    runtime.final_routes.clear()
    runtime.applied_final_decisions.clear()
    runtime.suppressed_announcement_sources.clear()
    runtime.playback_control_epoch += 1
    runtime.presentations.clear()
    runtime.chunks.clear()
    runtime.delivery_timer = None
    runtime.prefix_job = runtime.prefix_timer = runtime.prefix_pending = None
    runtime.current_utterance = None
    runtime.route_pending = False
    runtime.user_speaking = False
    runtime.profile = await runtime.memory.profile(runtime.user_id)
    hook = getattr(runtime, "erase_recordings", None)
    if hook:
        counts["recordings"] = await hook()
    prompt = (Path(runtime.config["assets"]) / "prompts/venus_system.md").read_text()
    prompt += "\n" + runtime.memory.profile_prompt(runtime.profile)
    language = runtime.config.get("speech_language", "auto")
    if language in {"en", "zh"}:
        prompt += "\nReply in " + ("English." if language == "en" else "Chinese.")
    en = runtime.response_language == "en"
    runtime.history_reset_pending = (
        (
            "History and saved personal information have been cleared."
            if en
            else "历史记录和保存的个人信息已清空。"
        )
        if include_profile
        else "History cleared. Personal information kept."
        if en
        else "历史已清空，个人信息保留。"
    )
    runtime.send_venus(
        {
            "type": "control",
            "generation_epoch": runtime.generation_epoch,
            "hold": True,
            "reset_context": True,
            "system_prompt": prompt,
        }
    )
    runtime.emit("history.cleared", **counts)
