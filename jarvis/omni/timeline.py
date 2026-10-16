from __future__ import annotations
import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
from jarvis.omni.protocol import Observation


@dataclass(frozen=True)
class ClockEstimate:
    source_clock: str
    offset_ms: float
    error_ms: float
    measured_at_ms: float | None = None

    @classmethod
    def from_ping(
        cls,
        source_clock,
        client_send_ms,
        server_receive_ms,
        server_send_ms,
        client_receive_ms,
    ):
        if client_receive_ms < client_send_ms or server_send_ms < server_receive_ms:
            raise ValueError("Non-monotonic clock handshake")
        uncertainty = (
            client_receive_ms - client_send_ms - (server_send_ms - server_receive_ms)
        ) / 2
        if uncertainty < 0:
            raise ValueError("Inconsistent clock handshake")
        return cls(
            source_clock,
            (server_receive_ms - client_send_ms + (server_send_ms - client_receive_ms))
            / 2,
            uncertainty,
            server_send_ms,
        )

    def server_time(self, source_capture_ts_ms):
        return source_capture_ts_ms + self.offset_ms

    def error_at(self, server_ms):
        return self.error_ms + (
            max(0, server_ms - self.measured_at_ms) * 0.0002
            if self.measured_at_ms is not None
            else 0
        )


@dataclass(frozen=True)
class Media:
    observation: Observation
    payload: bytes


class CausalTimeline:
    def __init__(self, *, max_bytes=64000000, window_ms=60000, track="screen_only"):
        if (
            max_bytes <= 0
            or window_ms <= 0
            or track not in {"screen_only", "public_telemetry"}
        ):
            raise ValueError("Invalid timeline capacity/track")
        self.max_bytes, self.window_ms, self.track = (max_bytes, window_ms, track)
        self.items = deque()
        self.by_id = {}
        self.sequences = {}
        self.byte_length = 0
        self.binding = None
        self.latest_frame = None
        self.frame_ready = asyncio.Event()
        self.dropped_frames = 0

    def bind(self, game_session_id, scene_epoch):
        if self.binding != (game_session_id, scene_epoch):
            self.binding = (game_session_id, scene_epoch)
            self.items.clear()
            self.by_id.clear()
            self.sequences.clear()
            self.byte_length = 0
            self.latest_frame = None
            self.frame_ready.clear()

    def add(self, observation: Observation, payload: bytes):
        stamp = observation.stamp
        if observation.source == "event_memory":
            raise ValueError(
                "Retained memory must come from the scoped server store, not captured media"
            )
        if self.binding != (stamp.game_session_id, stamp.scene_epoch):
            raise ValueError("Stale or unbound game scene")
        if self.track == "screen_only" and observation.source == "public_telemetry":
            raise ValueError("Telemetry is excluded from screen-only input")
        if observation.observation_id in self.by_id:
            raise ValueError("Duplicate observation ID")
        key = (observation.source, stamp.source_clock)
        if stamp.sequence_id <= self.sequences.get(key, -1):
            raise ValueError("Duplicate or reordered source sequence")
        if len(payload) != observation.byte_length or len(payload) > self.max_bytes:
            raise ValueError("Invalid media byte length")
        if hashlib.sha256(payload).hexdigest() != observation.content_sha256:
            raise ValueError("Media hash mismatch")
        item = Media(observation, payload)
        self.sequences[key] = stamp.sequence_id
        self.items.append(item)
        self.by_id[observation.observation_id] = item
        self.byte_length += len(payload)
        self._evict(observation.available_at_ms)
        if observation.source == "screen" and observation.observation_id in self.by_id:
            if self.latest_frame is not None:
                self.dropped_frames += 1
            self.latest_frame = item
            self.frame_ready.set()

    def _evict(self, now_ms):
        while self.items and (
            self.byte_length > self.max_bytes
            or self.items[0].observation.stamp.capture_ts_ms < now_ms - self.window_ms
        ):
            old = self.items.popleft()
            self.by_id.pop(old.observation.observation_id, None)
            self.byte_length -= len(old.payload)
            if old is self.latest_frame:
                self.latest_frame = None
                self.frame_ready.clear()

    def window(self, *, decision_at_ms, duration_ms=None, sources=None):
        self._evict(decision_at_ms)
        lower = decision_at_ms - (
            self.window_ms if duration_ms is None else duration_ms
        )
        return tuple(
            (
                item
                for item in self.items
                if lower <= item.observation.stamp.capture_ts_ms <= decision_at_ms
                and item.observation.available_at_ms <= decision_at_ms
                and (sources is None or item.observation.source in sources)
            )
        )

    async def next_frame(self):
        await self.frame_ready.wait()
        frame, self.latest_frame = (self.latest_frame, None)
        self.frame_ready.clear()
        return frame
