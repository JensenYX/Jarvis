def _bounds(runtime):
    cfg = getattr(runtime, "config", {}).get("native_routing", {})
    return (
        cfg.get("speech_observation_seconds", 180),
        cfg.get("speech_observation_limit", 24),
        cfg.get("speech_observation_characters", 4000),
    )


def prior_observations(runtime, update, *, seconds=None, limit=None, characters=None):
    configured_seconds, configured_limit, configured_characters = _bounds(runtime)
    seconds = configured_seconds if seconds is None else seconds
    limit = configured_limit if limit is None else limit
    characters = configured_characters if characters is None else characters
    return _observations(
        runtime,
        update.audio_end_sample,
        update.utterance_id,
        seconds=seconds,
        limit=limit,
        characters=characters,
    )


def current_observations(runtime):
    turns = getattr(runtime, "observed_turns", {})
    ends = [
        turn["final"].audio_end_sample for turn in turns.values() if turn.get("final")
    ]
    if not ends:
        return []
    seconds, limit, characters = _bounds(runtime)
    return _observations(
        runtime, max(ends), None, seconds=seconds, limit=limit, characters=characters
    )


def _observations(runtime, ceiling, excluded_uid, *, seconds, limit, characters):
    floor = max(runtime.history_audio_floor, ceiling - seconds * 16000)
    handled = (
        runtime.observed_consumed
        | runtime.observed_history
        | getattr(runtime, "observed_deleted", set())
    )
    rows = []
    for uid, turn in runtime.observed_turns.items():
        final = turn.get("final")
        if (
            uid == excluded_uid
            or uid in handled
            or final is None
            or (turn["start"] <= floor)
            or (final.audio_end_sample > ceiling)
            or (not final.text.strip())
        ):
            continue
        rows.append(
            {
                "utterance_id": uid,
                "audio_start_sample": turn["start"],
                "audio_end_sample": final.audio_end_sample,
                "text": final.text,
                "source": "observed_asr",
                "addressed_to_assistant": "unconfirmed",
            }
        )
    selected = []
    remaining = characters
    for row in sorted(rows, key=lambda item: item["audio_start_sample"], reverse=True):
        if len(row["text"]) > remaining:
            continue
        selected.append(row)
        remaining -= len(row["text"])
        if len(selected) == limit:
            break
    return list(reversed(selected))


def clear_observations(runtime):
    observed = getattr(runtime, "observed_turns", {})
    if hasattr(runtime, "observed_turns"):
        if not hasattr(runtime, "observed_deleted"):
            runtime.observed_deleted = set()
        runtime.observed_deleted.update(observed)
    ends = [runtime.history_audio_floor]
    for turn in observed.values():
        ends.append(turn["start"])
        for name in ("latest", "final"):
            if turn.get(name) is not None:
                ends.append(turn[name].audio_end_sample)
    runtime.history_audio_floor = max(ends)
    observed.clear()
    for name in (
        "observed_decisions",
        "observed_wires",
        "observed_direct_wires",
        "observed_consumed",
        "observed_memory",
        "observed_history",
        "native_source_observations",
        "memory_observer_results",
        "memory_observer_pending",
        "memory_observer_done",
    ):
        value = getattr(runtime, name, None)
        if value is not None:
            value.clear()
    if hasattr(runtime, "observer_current"):
        runtime.observer_current = None
