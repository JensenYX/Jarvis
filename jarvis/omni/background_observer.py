from collections import deque
from .capture import now_ms


class BackgroundObserver:
    def __init__(self, runtime):
        self.runtime = runtime
        self.settings = runtime.config["screen"].get("continuous_observation", {})
        self.pending = deque(maxlen=8)
        self.task = None
        self.last_started = float("-inf")

    def invalidate(self):
        self.pending.clear()
        if self.task is not None and (not self.task.done()):
            self.task.cancel()

    def start(self, frames):
        rt = self.runtime
        at = now_ms()
        if (
            not self.settings.get("enabled")
            or rt.capture.paused
            or rt.muted
            or (len(frames) < 2)
            or (self.task is not None and (not self.task.done()))
            or (at - self.last_started < self.settings.get("interval_ms", 1500))
        ):
            return
        self.last_started = at
        self.task = rt.spawn(self.observe(tuple(frames)))

    async def observe(self, frames):
        rt = self.runtime
        binding, epoch, control = (
            rt.binding(),
            rt.screen_session_epoch,
            rt.playback_control_epoch,
        )
        observed_at = frames[-1].observation.stamp.capture_ts_ms
        try:
            result = await rt.extractor.propose(
                frames,
                game=rt.game,
                user_request=None,
                language=rt.response_language,
                recent=rt.scheduler.delivered[-6:],
                preferences=list(rt.preferences),
                conversation=rt.recent_user_context(),
                timeout_s=self.settings.get("deadline_ms", 2500) / 1000,
            )
        except Exception as error:
            rt.emit("screen.observer.failed", error_type=type(error).__name__)
            return
        if (
            binding != rt.binding()
            or epoch != rt.screen_session_epoch
            or control != rt.playback_control_epoch
            or rt.capture.paused
        ):
            rt.emit("screen.observer.discarded", reason="stale_scene_or_control")
            return
        proposal, _, raw = result
        rt.emit(
            "screen.observer.observed",
            observed_at_ms=observed_at,
            proposal=proposal.model_dump(mode="json"),
            raw=raw,
            while_user_speaking=rt.user_speaking,
            while_assistant_speaking=bool(rt.response_id),
        )
        if not proposal.candidate_reply or not proposal.facts:
            rt.emit("screen.observer.discarded", reason="no_candidate")
            return
        self.enqueue(frames, result, binding=binding, epoch=epoch, control=control)

    def enqueue(self, frames, prepared, *, binding, epoch, control):
        if len(self.pending) == self.pending.maxlen:
            self.runtime.emit("screen.observer.discarded", reason="bounded_event_queue")
        self.pending.append(
            dict(
                frames=frames,
                prepared=prepared,
                binding=binding,
                epoch=epoch,
                control=control,
                observed_at_ms=frames[-1].observation.stamp.capture_ts_ms,
            )
        )

    def take(self):
        rt = self.runtime
        while self.pending:
            item = self.pending.popleft()
            proposal = item["prepared"][0]
            historical = proposal.temporal_mode == "historical" and all(
                (f.kind == "past_event" for f in proposal.facts)
            )
            limit = (
                self.settings.get("max_historical_pending_age_ms", 3500)
                if historical
                else self.settings.get("max_pending_age_ms", 3500)
            )
            if (
                item["binding"] != rt.binding()
                or item["epoch"] != rt.screen_session_epoch
                or item["control"] != rt.playback_control_epoch
            ):
                rt.emit("screen.observer.discarded", reason="stale_scene_or_control")
            elif not 0 <= now_ms() - item["observed_at_ms"] <= limit:
                rt.emit("screen.observer.discarded", reason="expired_event")
            else:
                rt.emit(
                    "screen.observer.candidate_ready",
                    observed_at_ms=item["observed_at_ms"],
                )
                return item
        return None
