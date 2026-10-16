import asyncio
import time
from collections import OrderedDict


class FrameWindow:
    def __init__(self, *, numbered=False, clock=time.monotonic):
        self.numbered = numbered
        self.clock = clock
        self.pending = OrderedDict()
        self.changed = asyncio.Event()
        self.sequence = 0
        self.quality = 80
        self.width = 1024
        self.ack_s = 0.12
        self.capture_ms = 0.0
        self.frames = 0
        self.acknowledged = 0
        self.bytes = 0
        self.timeouts = 0
        self.refreshes = 0
        self.last_ack = 0.0
        self.fast = 0
        self.last_refresh = 0.0

    async def ready(self):
        limit = 2 if self.numbered else 1
        while len(self.pending) >= limit:
            self.changed.clear()
            until = max(0.25, min(2.0, self.ack_s * 5))
            oldest = next(iter(self.pending.values()))["at"]
            try:
                await asyncio.wait_for(
                    self.changed.wait(), max(0.05, until - (self.clock() - oldest))
                )
            except TimeoutError:
                if self.pending:
                    self.pending.popitem(last=False)
                self.timeouts += 1
                self.ack_s = max(self.ack_s, 0.5)

    def sent(self, data, capture_ms):
        self.sequence += 1
        self.frames += 1
        self.bytes += len(data)
        self.capture_ms = capture_ms
        while len(self.pending) >= 2:
            self.pending.popitem(last=False)
        self.pending[self.sequence] = {"at": self.clock(), "bytes": len(data)}
        return (
            b"VXF1" + self.sequence.to_bytes(4, "big") + data if self.numbered else data
        )

    def ack(self, identifier=None):
        if not self.pending:
            return
        if identifier is None:
            if self.numbered:
                return
            identifier = next(iter(self.pending))
        if type(identifier) is not int or identifier not in self.pending:
            return
        now = self.clock()
        row = self.pending[identifier]
        elapsed = max(0, now - row["at"])
        for seq in list(self.pending):
            if seq <= identifier:
                self.pending.pop(seq)
        self.last_ack = now
        self.acknowledged += 1
        self.ack_s = 0.65 * self.ack_s + 0.35 * elapsed
        self.changed.set()

    def refresh(self):
        now = self.clock()
        if now - self.last_refresh < 1:
            return
        self.last_refresh = now
        self.refreshes += 1
        self.pending.clear()
        self.changed.set()

    @property
    def interval(self):
        return max(1 / 20, min(0.5, self.ack_s * 0.4))

    def stats(self):
        return {
            "frames": self.frames,
            "displayed": self.acknowledged,
            "bytes": self.bytes,
            "ack_ms": round(self.ack_s * 1000),
            "capture_ms": round(self.capture_ms, 1),
            "quality": self.quality,
            "width": self.width,
            "ack_timeouts": self.timeouts,
            "refreshes": self.refreshes,
        }
