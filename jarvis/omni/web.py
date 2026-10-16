from __future__ import annotations
import asyncio
import base64
import contextlib
import json
from pathlib import Path
import shutil
import time
from urllib.parse import urlencode
import numpy as np
import websockets
from fastapi import WebSocketDisconnect
from jarvis.config import session_config
from .runtime import ScreenRuntime
from jarvis.core.schemas import new_id
from jarvis.core.trace import Trace
from .capture import ScreenCapture, now_ms
from .transport import OwnerQueue, VoiceClock, WaveTrack


class Recordings:
    def __init__(self):
        self.entries = {}

    def register(self, directory):
        token = new_id()
        self.entries[token] = {
            "directory": directory,
            "active": set(),
            "created": time.monotonic(),
        }
        while len(self.entries) > 8:
            self.entries.pop(next(iter(self.entries)))
        return token

    def revoke(self, token):
        self.entries.pop(token, None)


async def session(
    ws, config, memory, busy, recordings, *, runtime_factory=ScreenRuntime
):
    await ws.accept()
    if busy.locked():
        await ws.close(code=1013, reason="A session is already active")
        return
    async with busy:
        identifier = new_id()
        directory = (
            Path(config["root"])
            / "runs/screen/live"
            / (time.strftime("%Y%m%d_%H%M%S") + "_" + identifier[:8])
        )
        trace = Trace(
            directory, identifier, pending_capacity=4096, pending_bytes=16000000
        )
        jobs, tracks, runtime = ([], {}, None)
        outgoing = asyncio.Queue(maxsize=512)
        asr_out = asyncio.Queue(maxsize=200)
        owner = OwnerQueue(trace.emit)
        capture = ScreenCapture(
            directory / "screen/frames", trace.emit, outgoing.put_nowait
        )
        token = recordings.register(directory)
        origin = now_ms()
        playback_origin = origin
        offsets = {}
        asr_samples = 0
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 20)
            if hello.get("type") != "start":
                raise ValueError("Expected session start")
            cfg = session_config(config, config["runtime"].get("assets_version"))
            native = cfg["screen"].get("native_routing", {}).get("enabled", False)
            capture.native_routing = native
            capture.separate_microphone = (
                cfg["screen"].get("speech_audio_source") == "microphone"
            )
            if native:
                from .native_observer import NativeScreenRuntime

                runtime_factory = NativeScreenRuntime
                cfg["native_routing"] = cfg["screen"]["native_routing"]
            language = hello.get("language", "auto")
            if language not in {"auto", "zh", "en"}:
                raise ValueError("Unsupported language")
            cfg["speech_language"] = language
            user_id = str(hello.get("user_id", "screen_user"))[:128]
            microphone = bool(hello.get("microphone", True))
            clock = VoiceClock(capture, microphone)
            tracks = {
                name: WaveTrack(
                    directory / filename,
                    rate,
                    origin,
                    clock_slack_ms=50 if name in {"mic", "game"} else 0,
                )
                for name, filename, rate in (
                    ("mic", "user.wav", 16000),
                    ("game", "game_analysis_mono.wav", 16000),
                    ("assistant", "assistant_played.wav", 24000),
                )
            }
            trace.manifest(
                session_id=identifier,
                user_id=user_id,
                mode="screen_integration",
                policy_version=cfg["policy_version"],
                models=cfg["models"],
                observation_track="screen_only",
                model_weights_frozen=True,
                microphone=microphone,
                record_origin_monotonic_ms=origin,
            )
            asr_url = cfg["endpoints"]["asr"]
            if language != "auto":
                asr_url += "?" + urlencode({"language": language})
            async with contextlib.AsyncExitStack() as connections:
                venus = await connections.enter_async_context(
                    websockets.connect(cfg["endpoints"]["venus"], max_size=8000000)
                )
                asr = (
                    await connections.enter_async_context(
                        websockets.connect(asr_url, max_size=2000000)
                    )
                    if microphone
                    else None
                )
                if asr:
                    ready = json.loads(await asyncio.wait_for(asr.recv(), 20))
                    if ready.get("type") != "ready":
                        raise RuntimeError("ASR unavailable")
                from .prompt import screen_system_prompt

                profile = (
                    await memory.profile(user_id)
                    if cfg["memory"].get("profile_context")
                    else []
                )
                prompt = screen_system_prompt(cfg, memory, profile)
                await venus.send(
                    json.dumps(
                        {
                            "type": "start",
                            "hold": True,
                            "system_prompt": prompt,
                            **(
                                {
                                    "owner_protocol": "buffered_native_v1",
                                    "session_id": identifier,
                                    **(
                                        {
                                            "prefill_mode": cfg["screen"][
                                                "buffered_native"
                                            ]["prefill_mode"]
                                        }
                                        if "prefill_mode"
                                        in cfg["screen"]["buffered_native"]
                                        else {}
                                    ),
                                }
                                if cfg["screen"]
                                .get("buffered_native", {})
                                .get("enabled")
                                else {}
                            ),
                            **(
                                {
                                    "owner_protocol": "omni_native_route_v1",
                                    "native_routing": cfg["native_routing"],
                                }
                                if native
                                else {}
                            ),
                            "incremental_tts": True,
                            "tts_drain_units": True,
                            "speech_audio_source": cfg["screen"].get(
                                "speech_audio_source", "mixed"
                            ),
                            "speech_visual_source": cfg["screen"].get(
                                "speech_visual_source", "screen"
                            ),
                            "speech_text_end": cfg["screen"].get(
                                "speech_text_end", False
                            ),
                            "speech_batch_approved": cfg["screen"].get(
                                "speech_batch_approved", False
                            ),
                            "speech_immediate_approved": cfg["screen"].get(
                                "speech_immediate_approved", False
                            ),
                            "speech_immediate_native": cfg["screen"].get(
                                "speech_immediate_native", False
                            ),
                            "speech_first_chunk_tokens": cfg["screen"].get(
                                "speech_first_chunk_tokens", 25
                            ),
                            "native_speech_tokens": cfg["screen"].get(
                                "native_speech_tokens", 20
                            ),
                            "speech_text_decode_mode": cfg["screen"].get(
                                "speech_text_decode_mode", "sampling"
                            ),
                            "speech_chunk_tokens": cfg["screen"].get(
                                "speech_chunk_tokens", 25
                            ),
                            "speech_trim_zero_prefix_ms": cfg["screen"].get(
                                "speech_trim_zero_prefix_ms", 0
                            ),
                            "speech_decision_reservation_ms": cfg["screen"].get(
                                "speech_decision_reservation_ms", 0
                            ),
                            "emit_text_candidates": cfg["screen"].get(
                                "emit_text_candidates", False
                            ),
                            "batch_system_prefill": cfg["venus"].get(
                                "batch_system_prefill", False
                            ),
                        }
                    )
                )
                ready = json.loads(await asyncio.wait_for(venus.recv(), 90))
                if ready.get("type") != "ready":
                    raise RuntimeError(
                        "Omni unavailable: "
                        + str(ready.get("error", "prepare failed"))[:200]
                    )
                if cfg["screen"].get("speech_immediate_approved") and (
                    not ready.get("patches", {}).get("speech_immediate_approved")
                ):
                    raise RuntimeError(
                        "Omni did not enable the requested immediate approved speech"
                    )
                if (
                    cfg["screen"].get("speech_first_chunk_tokens", 25) != 25
                    and ready.get("patches", {}).get("speech_first_chunk_tokens")
                    != cfg["screen"]["speech_first_chunk_tokens"]
                ):
                    raise RuntimeError(
                        "Omni did not enable the requested first speech chunk size"
                    )
                for option, default in [
                    ("speech_chunk_tokens", 25),
                    ("speech_trim_zero_prefix_ms", 0),
                    ("speech_visual_source", "screen"),
                    ("speech_immediate_native", False),
                    ("native_speech_tokens", 20),
                    ("speech_text_decode_mode", "sampling"),
                    ("speech_decision_reservation_ms", 0),
                ]:
                    if (
                        cfg["screen"].get(option, default) != default
                        and ready.get("patches", {}).get(option)
                        != cfg["screen"][option]
                    ):
                        raise RuntimeError(
                            f"Omni did not enable the requested {option}"
                        )
                if (
                    cfg["screen"].get("buffered_native", {}).get("enabled")
                    and ready.get("patches", {}).get("owner_protocol")
                    != "buffered_native_v1"
                ):
                    raise RuntimeError(
                        "Omni did not declare buffered playback transactions"
                    )
                if (
                    cfg["screen"].get("emit_text_candidates")
                    and ready.get("patches", {}).get("text_candidate_protocol")
                    != "native_text_before_tts_v1"
                ):
                    raise RuntimeError(
                        "Omni did not declare the requested text candidate protocol"
                    )
                if (
                    native
                    and ready.get("patches", {}).get("owner_protocol")
                    != "omni_native_route_v1"
                ):
                    raise RuntimeError(
                        "Omni native decision protocol was not negotiated"
                    )
                runtime = runtime_factory(
                    cfg,
                    identifier,
                    user_id,
                    trace,
                    outgoing.put_nowait,
                    owner.put,
                    memory,
                    capture=capture,
                )
                runtime.profile = profile
                runtime.preferences = [
                    row for row in profile if row["kind"] == "preference"
                ][-8:]
                runtime.emit("venus.prepared", patches=ready.get("patches", {}))

                async def erase_recordings_locked():
                    nonlocal token, tracks
                    recordings.revoke(token)
                    for track in tracks.values():
                        track.close()
                    tracks = {}
                    count = 0
                    for folder in directory.parent.iterdir():
                        if not folder.is_dir() or folder.is_symlink():
                            continue
                        try:
                            manifest = json.loads(
                                (folder / "manifest.json").read_text()
                            )
                        except (OSError, ValueError):
                            continue
                        if manifest.get("user_id") != user_id:
                            continue
                        for old_token, entry in tuple(recordings.entries.items()):
                            if entry["directory"] == folder:
                                recordings.revoke(old_token)
                        shutil.rmtree(folder / "screen", ignore_errors=True)
                        for name in (
                            "user.wav",
                            "assistant_played.wav",
                            "game_analysis_mono.wav",
                        ):
                            (folder / name).unlink(missing_ok=True)
                        if folder != directory:
                            (folder / "events.jsonl").unlink(missing_ok=True)
                        count += 1
                    capture.directory.mkdir(parents=True, exist_ok=True)
                    capture.erase()
                    clock.reset()
                    trace.erase()
                    offsets.clear()
                    token = recordings.register(directory)
                    outgoing.put_nowait(
                        {
                            "type": "screen.history_erased",
                            "payload": {"recording_token": token},
                        }
                    )
                    for name, filename, rate in (
                        ("mic", "user.wav", 16000),
                        ("game", "game_analysis_mono.wav", 16000),
                        ("assistant", "assistant_played.wav", 24000),
                    ):
                        tracks[name] = WaveTrack(
                            directory / filename,
                            rate,
                            origin,
                            clock_slack_ms=50 if name in {"mic", "game"} else 0,
                        )
                    return count

                async def erase_recordings():
                    await runtime.screen_memory.clear(user_id)
                    async with capture.lock:
                        return await erase_recordings_locked()

                runtime.erase_recordings = erase_recordings

                async def client_sender():
                    while True:
                        await ws.send_json(await outgoing.get())

                async def venus_sender():
                    while True:
                        item = await owner.get()
                        if item["type"] == "media.step":
                            trace.emit("screen.model_input.sent", **item["metadata"])
                        await venus.send(json.dumps(item, ensure_ascii=False))

                async def asr_sender():
                    if asr is None:
                        await asyncio.Future()
                    while True:
                        await asr.send(await asr_out.get())

                async def venus_receiver():
                    async for raw in venus:
                        msg = json.loads(raw)
                        kind = msg.pop("type")
                        if kind == "step":
                            runtime.post("venus.step", msg)
                        elif kind.startswith("draft.") and cfg["screen"].get(
                            "buffered_native", {}
                        ).get("enabled"):
                            runtime.post("venus." + kind, msg)
                        elif kind == "native.route" and native:
                            runtime.post("venus.native.route", msg)
                        elif kind == "text.candidate":
                            trace.emit("venus.text.candidate", **msg)
                            runtime.post("venus.text.candidate", msg)
                        elif kind == "context.reset":
                            runtime.post("history.reset.ready", msg)
                        elif kind == "error":
                            raise RuntimeError("Omni: " + str(msg.get("error"))[:200])
                        else:
                            trace.emit("venus." + kind, **msg)
                    raise ConnectionError(
                        "Omni connection ended before the user session"
                    )

                async def asr_receiver():
                    if asr is None:
                        await asyncio.Future()
                    async for raw in asr:
                        msg = json.loads(raw)
                        if msg.get("owner_profile"):
                            trace.emit(
                                "asr.owner_profile",
                                event_type=msg["type"],
                                utterance_id=msg.get("payload", {}).get("utterance_id"),
                                profile=msg["owner_profile"],
                            )
                        runtime.post(msg["type"], msg.get("payload", {}))
                    raise ConnectionError(
                        "ASR connection ended before the user session"
                    )

                async def receive():
                    nonlocal playback_origin, asr_samples
                    while True:
                        message = await ws.receive_json()
                        kind, data = (message.get("type"), message.get("payload", {}))
                        if kind == "end":
                            return
                        if kind == "screen.playback_clock":
                            if (
                                capture.clock is None
                                or capture.clock.source_clock != data["source_clock"]
                            ):
                                raise ValueError("Playback clock is not synchronized")
                            playback_origin = capture.clock.server_time(
                                float(data["start_ts_ms"])
                            )
                            if (
                                not origin
                                <= playback_origin
                                <= now_ms() + capture.clock.error_ms
                            ):
                                raise ValueError("Invalid playback clock origin")
                            trace.emit(
                                "screen.playback_clock",
                                origin_ms=playback_origin,
                                error_ms=capture.clock.error_ms,
                            )
                        elif kind == "screen.voice":
                            if capture.game_session_id and (not capture.paused):
                                raise ValueError(
                                    "Voice-only input during active screen capture"
                                )
                            pcm, packets, captured = clock.feed(data)
                            if microphone:
                                asr_samples += len(pcm)
                                asr_out.put_nowait(pcm.tobytes())
                                tracks["mic"].write(pcm, captured)
                            for index, packet in enumerate(packets):
                                if native or capture.separate_microphone:
                                    packet["microphone_pcm"] = packet["pcm"]
                                    packet["metadata"]["microphone_end_sample"] = (
                                        asr_samples
                                        - len(clock.buffer)
                                        - (len(packets) - 1 - index) * 16000
                                    )
                                owner.put(packet)
                        elif kind in {
                            "screen.clock.ping",
                            "screen.clock.reply",
                            "screen.begin",
                            "screen.ended",
                            "screen.pause",
                            "screen.track_state",
                            "screen.frame",
                            "screen.game_audio",
                            "screen.user_mic",
                            "screen.clock_silence",
                        }:
                            if (
                                kind == "screen.begin"
                                and bool(data.get("tracks", {}).get("microphone"))
                                != microphone
                            ):
                                raise ValueError("Microphone declaration changed")
                            previous = (
                                capture.game_session_id,
                                capture.scene_epoch,
                                capture.paused,
                            )
                            result = await capture.message(kind, data)
                            changed = previous != (
                                capture.game_session_id,
                                capture.scene_epoch,
                                capture.paused,
                            )
                            if changed:
                                clock.reset()
                                owner.media.clear()
                                runtime.capture_changed(
                                    data.get("game") if kind == "screen.begin" else None
                                )
                                owner.put(
                                    {
                                        "type": "control",
                                        "generation_epoch": runtime.generation_epoch,
                                        "hold": True,
                                        "reset_context": True,
                                        "system_prompt": runtime.reset_system_prompt(),
                                        "game": data.get("game", "unknown"),
                                    }
                                )
                            if (
                                kind == "screen.track_state"
                                and data.get("source") == "screen"
                                and (data.get("state") in {"mute", "ended"})
                                and (
                                    (
                                        data.get("game_session_id"),
                                        data.get("scene_epoch"),
                                    )
                                    == runtime.binding()
                                )
                            ):
                                runtime.belief.invalidate("source_lost")
                                if runtime.screen_response:
                                    runtime.clear_playback("screen_source_lost")
                            if result:
                                if "user_pcm" in result:
                                    asr_samples += len(result["user_pcm"])
                                    asr_out.put_nowait(result["user_pcm"].tobytes())
                                    tracks["mic"].write(
                                        result["user_pcm"],
                                        result["observation"].stamp.capture_ts_ms,
                                    )
                                if "game_pcm" in result:
                                    tracks["game"].write(
                                        result["game_pcm"],
                                        result["observation"].stamp.capture_ts_ms,
                                    )
                                packets = result.get("model_packets", [])
                                for index, packet in enumerate(packets):
                                    if native:
                                        packet["metadata"]["microphone_end_sample"] = (
                                            asr_samples
                                            - len(capture.mic_buffer)
                                            - (len(packets) - 1 - index) * 16000
                                        )
                                    owner.put(packet)
                        elif kind == "playback.ack":
                            chunk = runtime.chunks.get(data.get("chunk_id"))
                            if chunk and data.get("generation_epoch") == chunk["epoch"]:
                                old, end = (
                                    offsets.get(data["chunk_id"], 0),
                                    int(data["played_samples"]),
                                )
                                if old < end <= chunk["samples"] and chunk.get("pcm"):
                                    pcm = np.frombuffer(
                                        base64.b64decode(chunk["pcm"]), dtype="<f4"
                                    )
                                    output_end = int(data.get("playback_end_sample", 0))
                                    if data.get("playback_end_client_ms") is not None:
                                        output_end_ms = capture.clock.server_time(
                                            float(data["playback_end_client_ms"])
                                        )
                                        if not np.isfinite(output_end_ms):
                                            raise ValueError(
                                                "Invalid playback timestamp"
                                            )
                                        data["playback_end_server_ms"] = output_end_ms
                                    else:
                                        output_end_ms = (
                                            playback_origin + output_end / 24
                                        )
                                        data["playback_clock_method"] = (
                                            "legacy_reset_sample_counter"
                                        )
                                    tracks["assistant"].write(
                                        pcm[old:end], output_end_ms - (end - old) / 24
                                    )
                                    offsets[data["chunk_id"]] = end
                            runtime.post(kind, data)
                        elif kind in {
                            "stop_speaking",
                            "feedback",
                            "present_result",
                            "playback.event",
                        }:
                            runtime.post(kind, data)
                        elif kind == "screen.style":
                            from .controls import STYLE_TEXT

                            if (
                                data.get("style") not in STYLE_TEXT
                                or not capture.game_session_id
                                or capture.paused
                            ):
                                raise ValueError(
                                    "Style selection requires an active shared game and a supported option"
                                )
                            runtime.post(
                                "screen.style.selected", {"style": data["style"]}
                            )
                        else:
                            trace.emit(
                                "client.message_rejected", message_type=str(kind)
                            )

                async def monitor():
                    while True:
                        for key in tuple(offsets):
                            if key not in runtime.chunks:
                                offsets.pop(key, None)
                        trace.emit(
                            "screen.runtime.metrics",
                            queues={
                                "client": outgoing.qsize(),
                                "asr": asr_out.qsize(),
                                "runtime": runtime.queue.qsize(),
                                "model_media": len(owner.media),
                                "model_controls": len(owner.controls),
                                "trace": trace.pending.qsize(),
                            },
                            media_bytes=capture.timeline.byte_length,
                            media_items=len(capture.timeline.items),
                            raw_frame_bytes=capture.frame_bytes,
                            raw_frame_files=len(capture.frame_files),
                            retained_evidence_bytes=capture.retained_bytes,
                            claims=len(runtime.belief.claims),
                            claim_observations=len(runtime.belief.observations),
                            playback_chunks=len(runtime.chunks),
                            playback_responses=len(runtime.presentations),
                            playback_pcm_characters=sum(
                                (len(c.get("pcm", "")) for c in runtime.chunks.values())
                            ),
                            pending_trace_bytes=getattr(
                                trace.pending, "resident_bytes", None
                            ),
                            clock_error_ms=capture.clock.error_at(now_ms())
                            if capture.clock
                            else None,
                        )
                        await asyncio.sleep(30)

                async def capacity_watch():
                    await runtime.capacity_exceeded.wait()
                    raise BufferError(
                        "Screen session exceeded its bounded backlog or playback budget"
                    )

                jobs = [
                    asyncio.create_task(fn())
                    for fn in (
                        client_sender,
                        venus_sender,
                        asr_sender,
                        venus_receiver,
                        asr_receiver,
                        runtime.run,
                        receive,
                        monitor,
                        capacity_watch,
                    )
                ]
                outgoing.put_nowait(
                    {
                        "type": "session",
                        "session_id": identifier,
                        "generation_epoch": 0,
                        "input_sample_rate": 16000,
                        "output_sample_rate": 24000,
                        "microphone": microphone,
                        "recording_token": token,
                        "frontend": "venus-omni",
                        "observation_track": "screen_only",
                    }
                )
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    job.result()
        except WebSocketDisconnect:
            pass
        except Exception as error:
            with contextlib.suppress(Exception):
                trace.emit(
                    "session.failed",
                    error=type(error).__name__,
                    message=str(error)[:300],
                )
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "message": str(error)[:250]})
                await ws.close(
                    code=1013, reason="Session ended after a connection failure"
                )
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            if runtime:
                await runtime.close()
            for track in tracks.values():
                track.close()
            with contextlib.suppress(Exception):
                trace.emit(
                    "session.closed",
                    recording="separate timestamped tracks",
                    future_frames_accepted=0,
                )
            await asyncio.to_thread(trace.close)
