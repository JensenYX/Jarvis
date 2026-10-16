from dataclasses import dataclass
import math
import time
import httpx


class ReviewUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Attempt:
    epoch: int
    probe: bool


class ReviewAvailability:
    def __init__(self, settings=None, *, clock=time.monotonic):
        settings = settings or {}
        self.enabled = settings.get("enabled", False)
        self.failures_to_open = settings.get("consecutive_failures", 3)
        self.cooldown_s = settings.get("cooldown_ms", 30000) / 1000
        if (
            type(self.enabled) is not bool
            or type(self.failures_to_open) is not int
            or self.failures_to_open < 1
            or (not math.isfinite(self.cooldown_s))
            or (self.cooldown_s <= 0)
        ):
            raise ValueError("Invalid review availability settings")
        self.clock = clock
        self.failures = 0
        self.open_until = None
        self.probing = False
        self.epoch = 0

    def begin(self):
        if not self.enabled:
            return None
        probe = self.open_until is not None
        if probe and (self.clock() < self.open_until or self.probing):
            raise ReviewUnavailable(
                "Remote review temporarily unavailable; no semantic verdict"
            )
        if probe:
            self.probing = True
        return Attempt(self.epoch, probe)

    def finish(self, attempt, *, available):
        if attempt is None or attempt.epoch != self.epoch:
            return
        if attempt.probe:
            self.probing = False
        if available is None:
            return
        if available:
            self.failures = 0
            self.open_until = None
        else:
            self.failures += 1
            if attempt.probe or self.failures >= self.failures_to_open:
                self.open_until = self.clock() + self.cooldown_s
                self.epoch += 1

    def snapshot(self):
        return {
            "enabled": self.enabled,
            "consecutive_failures": self.failures,
            "retry_after_ms": max(0, (self.open_until - self.clock()) * 1000)
            if self.open_until is not None
            else 0,
            "probe_inflight": self.probing,
            "epoch": self.epoch,
        }


def availability_outcome(error, http_status):
    if isinstance(error, (TimeoutError, httpx.TransportError)):
        return False
    if http_status is not None:
        return http_status != 429 and http_status < 500
    return None
