from __future__ import annotations
import asyncio
import base64
from collections import deque
import hashlib
from pathlib import Path
import time
from uuid import uuid4
import numpy as np
from jarvis.omni.protocol import MediaStamp, Observation
from jarvis.omni.timeline import CausalTimeline, ClockEstimate


def now_ms():
    return time.monotonic_ns() / 1000000


class ScreenCapture:
    def __init__(
        self, directory: Path, emit, send, *, max_bytes=64000000, ring_ms=60000
    ):
        self.directory, self.emit, self.send = (directory, emit, send)
        self.timeline = CausalTimeline(max_bytes=max_bytes, window_ms=ring_ms)
        self.clock = None
        self.pending_sync = {}
        self.game_session_id = None
        self.scene_epoch = 0
        self.source_clock = None
        self.game_audio = deque()
        self.mic_buffer = np.zeros(0, dtype=np.float32)
        self.mic_start_ms = None
        self.mic_expected_ms = None
        self.model_sequence = 0
        self.frames = 0
        self.game_samples = 0
        self.paused = False
        self.recording_metadata = []
        self.has_microphone = False
        self.silence_sequence = -1
        self.source_states = {}
        self.lock = asyncio.Lock()
        self.frame_files = deque()
        self.frame_bytes = 0
        self.retained_files = {}
        self.retained_bytes = 0
        self.erasure_epoch = 0
        self.scene_floors = {}
        self.directory.mkdir(parents=True, exist_ok=True)

    async def message(self, kind, data):
        async with self.lock:
            return await self._message(kind, data)

    async def _message(self, kind, data):
        if kind == "screen.clock.ping":
            nonce = uuid4().hex
            stamp = {
                "nonce": nonce,
                "client_send_ms": float(data["client_send_ms"]),
                "server_receive_ms": now_ms(),
                "source_clock": str(data["source_clock"]),
            }
            stamp["server_send_ms"] = now_ms()
            self.pending_sync[nonce] = stamp
            while len(self.pending_sync) > 8:
                self.pending_sync.pop(next(iter(self.pending_sync)))
            self.send({"type": "screen.clock.pong", "payload": stamp})
            return None
        if kind == "screen.clock.reply":
            stamp = self.pending_sync.pop(data["nonce"])
            estimate = ClockEstimate.from_ping(
                stamp["source_clock"],
                stamp["client_send_ms"],
                stamp["server_receive_ms"],
                stamp["server_send_ms"],
                float(data["client_receive_ms"]),
            )
            previous = self.clock
            if (
                previous is None
                or previous.source_clock != estimate.source_clock
                or estimate.error_ms <= previous.error_at(estimate.measured_at_ms)
            ):
                self.clock = estimate
            else:
                self.emit(
                    "screen.clock.sample_rejected",
                    reason="larger_uncertainty",
                    sample_offset_ms=estimate.offset_ms,
                    sample_error_ms=estimate.error_ms,
                    retained_error_ms=previous.error_at(estimate.measured_at_ms),
                )
            self.emit(
                "screen.clock.estimated",
                source_clock=self.clock.source_clock,
                offset_ms=self.clock.offset_ms,
                error_ms=self.clock.error_at(now_ms()),
                measured_at_ms=self.clock.measured_at_ms,
            )
            return None
        if kind == "screen.begin":
            if self.clock is None or self.clock.source_clock != data["source_clock"]:
                raise ValueError("Capture requires a measured source clock")
            name = str(data["game_session_id"])
            if len(name) != 32 or any((c not in "0123456789abcdef" for c in name)):
                raise ValueError("Invalid game session identifier")
            if int(data["scene_epoch"]) < self.scene_floors.get(name, 0):
                self.emit("screen.media.dropped", reason="retired_scene_begin")
                return None
            self.game_session_id = name
            self.source_clock = str(data["source_clock"])
            self.scene_epoch = int(data["scene_epoch"])
            self.timeline.bind(self.game_session_id, self.scene_epoch)
            self.game_audio.clear()
            self.mic_buffer = np.zeros(0, dtype=np.float32)
            self.mic_start_ms = self.mic_expected_ms = None
            self.has_microphone = bool(data.get("tracks", {}).get("microphone"))
            self.silence_sequence = -1
            self.source_states = {
                "screen": "live",
                "game_audio": "live"
                if data.get("tracks", {}).get("game_audio")
                else "missing",
            }
            self.paused = False
            self.emit(
                "screen.capture.started",
                game_session_id=self.game_session_id,
                scene_epoch=self.scene_epoch,
                tracks=data.get("tracks", {}),
                game=data.get("game", "unknown"),
                observation_track="screen_only",
            )
            return None
        if kind in {"screen.ended", "screen.pause"}:
            if (
                data.get("game_session_id") != self.game_session_id
                or data.get("scene_epoch") != self.scene_epoch
            ):
                return None
            self.paused = True
            self.scene_epoch += 1
            self.scene_floors[self.game_session_id] = self.scene_epoch
            while len(self.scene_floors) > 16:
                self.scene_floors.pop(next(iter(self.scene_floors)))
            self.timeline.bind(self.game_session_id, self.scene_epoch)
            self.game_audio.clear()
            self.mic_buffer = np.zeros(0, dtype=np.float32)
            self.mic_start_ms = self.mic_expected_ms = None
            self.emit(
                "screen.capture.stopped",
                reason=kind,
                game_session_id=self.game_session_id,
                scene_epoch=self.scene_epoch,
            )
            return None
        if kind == "screen.track_state":
            if (
                data.get("game_session_id") != self.game_session_id
                or data.get("scene_epoch") != self.scene_epoch
            ):
                return None
            self.source_states[str(data["source"])] = str(data["state"])
            self.emit("screen.track.state", **data)
            return None
        if kind not in {
            "screen.frame",
            "screen.game_audio",
            "screen.user_mic",
            "screen.clock_silence",
        }:
            raise ValueError("Unknown capture message")
        if self.clock is None:
            raise ValueError("Capture requires a measured source clock")
        if self.game_session_id is None or self.paused:
            self.emit(
                "screen.media.dropped",
                reason="inactive_capture",
                sequence_id=data.get("sequence_id"),
            )
            return None
        if (
            data["game_session_id"] != self.game_session_id
            or data["scene_epoch"] != self.scene_epoch
        ):
            self.emit(
                "screen.media.dropped",
                reason="stale_scene",
                sequence_id=data.get("sequence_id"),
            )
            return None
        if data["source_clock"] != self.source_clock:
            raise ValueError("Source clock changed without a new handshake")
        received = now_ms()
        clock_error = self.clock.error_at(received)
        payload = base64.b64decode(data["data"], validate=True)
        if len(payload) > (4000000 if kind == "screen.frame" else 128000):
            raise ValueError("Media packet exceeds limit")
        source = {
            "screen.frame": "screen",
            "screen.game_audio": "game_audio",
            "screen.user_mic": "user_mic",
            "screen.clock_silence": "clock_silence",
        }[kind]
        captured = self.clock.server_time(float(data["capture_ts_ms"]))
        sequence = int(data["sequence_id"])
        pcm = None
        duration = 0
        if source != "screen":
            pcm = np.frombuffer(payload, dtype="<f4").copy()
            if not 0 < len(pcm) <= 16000 or not np.isfinite(pcm).all():
                raise ValueError("Invalid 16kHz mono analysis audio")
            duration = len(pcm) / 16
        if source == "clock_silence":
            if self.has_microphone or np.any(pcm) or sequence <= self.silence_sequence:
                raise ValueError(
                    "Video-only clock must be ordered silent PCM without a microphone"
                )
            if captured - clock_error > received:
                raise ValueError("Video clock cannot advance into the future")
            self.silence_sequence = sequence
            return {"model_packets": self._pack_units(pcm, captured, received, source)}
        if source == "user_mic" and (not self.has_microphone):
            raise ValueError("User microphone was not declared for this capture")
        observation = Observation(
            observation_id=f"{self.game_session_id}:{self.scene_epoch}:{source}:{sequence}",
            stamp=MediaStamp(
                game_session_id=self.game_session_id,
                scene_epoch=self.scene_epoch,
                sequence_id=sequence,
                source_clock=self.source_clock,
                source_capture_ts_ms=float(data["capture_ts_ms"]),
                capture_ts_ms=captured,
                receive_ts_ms=received,
                clock_error_ms=clock_error,
            ),
            source=source,
            evidence_refs=(f"{source}:{sequence}",),
            visibility="player_visible" if source == "screen" else "player_audible",
            available_at_ms=received,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            mime_type=data.get("mime_type", "audio/pcm-f32le"),
            byte_length=len(payload),
            duration_ms=duration,
            sample_rate=None if source == "screen" else 16000,
        )
        self.timeline.add(observation, payload)
        self.emit("screen.observation", **observation.model_dump(mode="json"))
        if source == "screen":
            self.frames += 1
            path = (
                self.directory
                / f"{self.game_session_id}-{self.scene_epoch}-{sequence:08d}.jpg"
            )
            await asyncio.to_thread(path.write_bytes, payload)
            self.frame_files.append((path, len(payload), captured))
            self.frame_bytes += len(payload)
            while self.frame_files and (
                self.frame_bytes > self.timeline.max_bytes
                or self.frame_files[0][2] < received - self.timeline.window_ms
            ):
                old, size, _ = self.frame_files.popleft()
                self.frame_bytes -= size
                await asyncio.to_thread(old.unlink, missing_ok=True)
            return {"observation": observation, "frame": payload}
        if source == "game_audio":
            self.game_audio.append((captured, pcm))
            self.game_samples += len(pcm)
            while self.game_audio and self.game_audio[0][0] < received - 5000:
                self.game_audio.popleft()
            return {"observation": observation, "game_pcm": pcm}
        return {
            "observation": observation,
            "user_pcm": pcm,
            "model_packets": self._pack_units(pcm, captured, received, source),
        }

    async def retain_evidence(self, frames, *, erasure_epoch):
        async with self.lock:
            if erasure_epoch != self.erasure_epoch:
                return {}
            directory = self.directory.parent / "evidence"
            directory.mkdir(parents=True, exist_ok=True)
            paths = {}
            for frame in frames:
                key = frame.observation.content_sha256
                path = directory / (key + ".jpg")
                if key not in self.retained_files:
                    await asyncio.to_thread(path.write_bytes, frame.payload)
                    self.retained_files[key] = (path, len(frame.payload))
                    self.retained_bytes += len(frame.payload)
                paths[frame.observation.observation_id] = str(path)
            while (
                len(self.retained_files) > 512
                or self.retained_bytes > self.timeline.max_bytes
            ):
                key = next(iter(self.retained_files))
                path, size = self.retained_files.pop(key)
                self.retained_bytes -= size
                await asyncio.to_thread(path.unlink, missing_ok=True)
            return {key: path for key, path in paths.items() if Path(path).exists()}

    def erase(self):
        self.erasure_epoch += 1
        if self.game_session_id:
            self.scene_floors[self.game_session_id] = self.scene_epoch + 1
        self.timeline.bind(None, -1)
        self.game_session_id = None
        self.paused = True
        self.frame_files.clear()
        self.retained_files.clear()
        self.frame_bytes = self.retained_bytes = 0
        self.game_audio.clear()
        self.mic_buffer = np.zeros(0, dtype=np.float32)
        self.mic_start_ms = self.mic_expected_ms = None

    def _pack_units(self, pcm, captured, received, source):
        duration = len(pcm) / 16
        if self.mic_expected_ms is not None and abs(
            captured - self.mic_expected_ms
        ) > 50 + self.clock.error_at(received):
            self.emit(
                "screen.audio.discontinuity", gap_ms=captured - self.mic_expected_ms
            )
            self.mic_buffer = np.zeros(0, dtype=np.float32)
            self.mic_start_ms = captured
        if self.mic_start_ms is None:
            self.mic_start_ms = captured
        self.mic_expected_ms = captured + duration
        self.mic_buffer = np.concatenate([self.mic_buffer, pcm])
        packets = []
        while len(self.mic_buffer) >= 16000:
            begin, end = (self.mic_start_ms, self.mic_start_ms + 1000)
            user = self.mic_buffer[:16000]
            game = np.zeros(16000, dtype=np.float32)
            present = np.zeros(16000, dtype=bool)
            for game_start, block in self.game_audio:
                offset = round((game_start - begin) * 16)
                lower, upper = (max(0, offset), min(16000, offset + len(block)))
                if lower < upper:
                    game[lower:upper] = block[lower - offset : upper - offset]
                    present[lower:upper] = True
            mixed = np.clip(user + game, -1, 1).astype("<f4")
            candidates = (
                tuple(
                    (
                        frame
                        for frame in self.timeline.window(
                            decision_at_ms=received,
                            duration_ms=2000,
                            sources={"screen"},
                        )
                        if frame.observation.stamp.capture_ts_ms <= end
                    )
                )
                if self.source_states.get("screen") in {"live", "unmute"}
                else ()
            )
            frame = candidates[-1] if candidates else None
            metadata = {
                "sequence_id": self.model_sequence,
                "audio_start_ts_ms": begin,
                "audio_end_ts_ms": end,
                "game_session_id": self.game_session_id,
                "scene_epoch": self.scene_epoch,
                "clock_error_ms": self.clock.error_at(received),
                "available_at_ms": received,
                "sources": (["user_mic"] if source == "user_mic" else [])
                + (["game_audio"] if present.any() else []),
                "clock_audio_kind": "microphone"
                if source == "user_mic"
                else "synthetic_silence",
                "game_audio_samples_present": int(present.sum()),
            }
            if frame:
                metadata.update(
                    frame_sha256=frame.observation.content_sha256,
                    frame_capture_ts_ms=frame.observation.stamp.capture_ts_ms,
                    frame_observation_id=frame.observation.observation_id,
                )
            packets.append(
                {
                    "type": "media.step",
                    "metadata": metadata,
                    "pcm": base64.b64encode(mixed.tobytes()).decode(),
                    "frame": base64.b64encode(frame.payload).decode()
                    if frame
                    else None,
                }
            )
            if getattr(self, "native_routing", False) or getattr(
                self, "separate_microphone", False
            ):
                packets[-1]["microphone_pcm"] = base64.b64encode(
                    user.astype("<f4").tobytes()
                ).decode()
            self.model_sequence += 1
            self.mic_buffer = self.mic_buffer[16000:]
            self.mic_start_ms = end
        return packets
