from __future__ import annotations
from .protocol import InteractionPlan


class SpeechScheduler:
    def __init__(self, belief):
        self.belief = belief
        self.active: InteractionPlan | None = None
        self.started_at_ms = None
        self.delivered = []

    def reason(
        self,
        plan,
        *,
        now_ms,
        session_epoch,
        generation,
        game_session_id,
        scene_epoch,
        user_speaking=False,
    ):
        if plan.session_epoch != session_epoch or plan.speech_generation != generation:
            return "stale_speech_generation"
        if plan.game_session_id is not None and (
            plan.game_session_id,
            plan.scene_epoch,
        ) != (game_session_id, scene_epoch):
            return "stale_scene"
        if user_speaking:
            return "user_speaking"
        if self.started_at_ms is None and (
            not plan.earliest_start_ms <= now_ms <= plan.latest_useful_start_ms
        ):
            return "outside_start_window"
        if (
            self.started_at_ms is not None
            and now_ms - self.started_at_ms > plan.max_spoken_duration_ms
        ):
            return "speech_duration_exceeded"
        if any(
            (
                not self.belief.eligible(
                    key,
                    at_ms=now_ms,
                    game_session_id=plan.game_session_id,
                    scene_epoch=plan.scene_epoch,
                )
                for key in plan.claim_ids
            )
        ):
            return "claim_expired_or_invalidated"
        return None

    def queue(self, plan, **context):
        previous_start = self.started_at_ms
        self.started_at_ms = None
        reason = self.reason(plan, **context)
        if reason is None:
            self.active = plan.model_copy(update={"status": "queued"})
        else:
            self.started_at_ms = previous_start
        return reason

    def chunk(self, **context):
        if self.active is None:
            return "no_active_plan"
        reason = self.reason(self.active, **context)
        if reason is None and self.started_at_ms is None:
            self.started_at_ms = context["now_ms"]
            self.active = self.active.model_copy(update={"status": "started"})
        return reason

    def finish(self, *, actually_complete, now_ms):
        plan = self.active
        if plan is None:
            return None
        plan = plan.model_copy(
            update={"status": "completed" if actually_complete else "interrupted"}
        )
        if actually_complete:
            self.delivered.append(
                {
                    "event_id": plan.event_id,
                    "claim_ids": plan.claim_ids,
                    "utterance": plan.utterance_goal,
                    "completed_at_ms": now_ms,
                    "game_session_id": plan.game_session_id,
                    "scene_epoch": plan.scene_epoch,
                }
            )
            self.delivered = self.delivered[-32:]
        self.active, self.started_at_ms = (None, None)
        return plan
