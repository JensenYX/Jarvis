from __future__ import annotations
import asyncio
import base64
from collections import deque
import wave
import numpy as np
from jarvis.omni.capture import now_ms


class OwnerQueue:
    def __init__(self, emit, max_media=2):
        self.controls, self.media = (deque(), deque())
        self.ready = asyncio.Event()
        self.emit, self.max_media = (emit, max_media)
        self.sequence = 0

    def put(self, message):
        if message.get("type") == "media.step":
            message["metadata"]["sequence_id"] = self.sequence
            self.sequence += 1
            if len(self.media) >= self.max_media:
                dropped = self.media.popleft()
                self.emit(
                    "screen.model_input.dropped",
                    reason="owner_backpressure",
                    sequence_id=dropped["metadata"]["sequence_id"],
                )
            self.media.append(message)
        else:
            if len(self.controls) >= 128:
                raise RuntimeError("Voice control queue exceeded its bound")
            if message.get("reset_context"):
                self.media.clear()
            self.controls.append(message)
        self.ready.set()

    async def get(self):
        await self.ready.wait()
        message = (self.controls if self.controls else self.media).popleft()
        if not self.controls and (not self.media):
            self.ready.clear()
        return message


class VoiceClock:
    def __init__(self, capture, microphone):
        self.capture, self.microphone = (capture, microphone)
        self.buffer = np.zeros(0, dtype="<f4")
        self.start = self.expected = None
        self.sequence = -1

    def reset(self):
        self.buffer = np.zeros(0, dtype="<f4")
        self.start = self.expected = None

    def feed(self, data):
        clock = self.capture.clock
        if clock is None or data["source_clock"] != clock.source_clock:
            raise ValueError("Voice input requires clock synchronization")
        sequence = int(data["sequence_id"])
        if sequence <= self.sequence:
            raise ValueError("Voice input sequence is not increasing")
        self.sequence = sequence
        pcm = np.frombuffer(
            base64.b64decode(data["data"], validate=True), dtype="<f4"
        ).copy()
        if not 0 < len(pcm) <= 16000 or not np.isfinite(pcm).all():
            raise ValueError("Invalid microphone PCM")
        if not self.microphone and np.any(pcm):
            raise ValueError("A microphone-free clock must contain silence")
        captured, received = (clock.server_time(float(data["capture_ts_ms"])), now_ms())
        if captured - clock.error_at(received) > received:
            raise ValueError("Future voice input")
        if self.expected is not None and abs(
            captured - self.expected
        ) > 50 + clock.error_at(received):
            self.reset()
        if self.start is None:
            self.start = captured
        self.expected = captured + len(pcm) / 16
        self.buffer = np.concatenate((self.buffer, pcm))
        packets = []
        while len(self.buffer) >= 16000:
            packets.append(
                {
                    "type": "media.step",
                    "frame": None,
                    "pcm": base64.b64encode(self.buffer[:16000].tobytes()).decode(),
                    "metadata": {
                        "sequence_id": 0,
                        "audio_start_ts_ms": self.start,
                        "audio_end_ts_ms": self.start + 1000,
                        "available_at_ms": received,
                        "clock_error_ms": clock.error_at(received),
                        "sources": ["user_mic"] if self.microphone else [],
                        "clock_audio_kind": "microphone"
                        if self.microphone
                        else "synthetic_silence",
                    },
                }
            )
            self.buffer = self.buffer[16000:]
            self.start += 1000
        return (pcm, packets, captured)


class WaveTrack:
    def __init__(self, path, rate, origin_ms, *, clock_slack_ms=0):
        self.rate, self.origin_ms, self.samples = (rate, origin_ms, 0)
        self.clock_slack_samples = round(clock_slack_ms * rate / 1000)
        self.output = wave.open(str(path), "wb")
        self.output.setnchannels(1)
        self.output.setsampwidth(2)
        self.output.setframerate(rate)

    def write(self, pcm, at_ms=None):
        offset = (
            self.samples
            if at_ms is None
            else max(0, round((at_ms - self.origin_ms) * self.rate / 1000))
        )
        if self.samples and abs(offset - self.samples) <= self.clock_slack_samples:
            offset = self.samples
        gap = offset - self.samples
        while gap > 0:
            size = min(gap, self.rate)
            self.output.writeframesraw(bytes(size * 2))
            self.samples += size
            gap -= size
        begin = max(0, self.samples - offset)
        values = np.clip(pcm[begin:], -1, 1)
        self.output.writeframesraw((values * 32767).astype("<i2").tobytes())
        self.samples += len(values)

    def close(self):
        self.output.close()
