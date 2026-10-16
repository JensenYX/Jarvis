import base64
from .capture import now_ms
from .controls import STYLE_TEXT
from .decision.base import DecisionInput, Question
from .perception import ProposedFact, VisualProposal


class LifeEvents:
    def __init__(self, runtime):
        self.runtime = runtime
        self.invalidate()

    def invalidate(self):
        self.stable = None
        self.pending = None
        self.binding = None

    def observe(self, frame, reading):
        rt = self.runtime
        settings = rt.config["screen"].get("jev_venus", {})
        if not settings.get("life_transition_events") or rt.game != "lol":
            return
        if (
            not rt.preferences
            or rt.preferences[-1].get("source") != "user_interface"
            or rt.preferences[-1].get("source_id") != "style_selector"
            or (rt.preferences[-1].get("text") != STYLE_TEXT["active"])
        ):
            self.invalidate()
            return
        binding = (rt.binding(), rt.screen_session_epoch, rt.playback_control_epoch)
        if binding != self.binding:
            self.invalidate()
            self.binding = binding
        observation = frame.observation
        if (
            not reading
            or not reading.get("admitted")
            or reading["state"] not in {"low", "alive", "dead"}
            or (not 0 <= now_ms() - observation.stamp.capture_ts_ms <= 2500)
        ):
            self.pending = None
            return
        current = (frame, reading)
        category = lambda item: "dead" if item[1]["state"] == "dead" else "living"
        if self.stable is None:
            self.stable = current
            return
        age = (
            observation.stamp.capture_ts_ms
            - self.stable[0].observation.stamp.capture_ts_ms
        )
        if not 0 < age <= 10000:
            self.stable = current
            self.pending = None
            return
        if category(self.stable) == category(current):
            self.stable = current
            self.pending = None
            return
        if self.pending is None or category(self.pending) != category(current):
            self.pending = current
            return
        gap = (
            observation.stamp.capture_ts_ms
            - self.pending[0].observation.stamp.capture_ts_ms
        )
        if not 500 <= gap <= 2500:
            self.pending = current
            return
        before = self.stable
        frames = (before[0], self.pending[0], frame)
        self.stable = current
        self.pending = None
        records = [f.observation for f in frames]
        refs = tuple((o.observation_id for o in records))
        text = "刚才倒下了。" if reading["state"] == "dead" else "刚才复活了。"
        fact = ProposedFact(
            text=text,
            status="observed",
            subject="user",
            kind="past_event",
            evidence_refs=refs,
            valid_for_ms=30000,
        )
        proposal = VisualProposal(
            facts=(fact,),
            unknowns=(),
            candidate_reply=text,
            intent="REVIEW",
            event_description="Jev_life_transition:" + refs[-1],
            temporal_mode="historical",
            max_spoken_duration_ms=6000,
        )
        at = now_ms()
        state = {
            "game": "lol",
            "user_request": None,
            "decision_at_ms": at,
            "player_identity_binding": {
                "source": "explicit_replay_role",
                "scope": "displayed_HUD_controlled_avatar",
            },
            "observations": [
                {
                    "id": o.observation_id,
                    "source": o.source,
                    "visibility": o.visibility,
                    "age_ms": at - o.stamp.capture_ts_ms,
                    "content_sha256": o.content_sha256,
                    "scene_epoch": o.stamp.scene_epoch,
                }
                for o in records
            ],
            "pixel_life_states": [
                {**r, "capture_ts_ms": f.observation.stamp.capture_ts_ms}
                for f, r in (before, current)
            ],
            "interaction_capabilities": {
                "visual_conversation": True,
                "proactive_commentary": True,
                "instant_tactical_alerts": False,
            },
            "evidence_representation": "state transition of trained Jev classifications; not an authored assistant answer",
        }
        request = DecisionInput(
            state=state,
            observation_ids=refs,
            images=tuple(
                (
                    "data:"
                    + f.observation.mime_type
                    + ";base64,"
                    + base64.b64encode(f.payload).decode()
                    for f in frames
                )
            ),
            questions=(
                Question(
                    id="event",
                    primitive="noul",
                    instructions="Is this observed life-state transition supported?",
                ),
            ),
        )
        raw = {
            "model": "Jev-Omni/unified",
            "source": "Jev_typed_life_transition",
            "observation_records": [o.model_dump(mode="json") for o in records],
            "pixel_life_states": state["pixel_life_states"],
            "confirming_reading": reading,
            "confirmation_gap_ms": gap,
            "assistant_wording_supplied": False,
        }
        rt.emit(
            "screen.life_event.observed",
            proposal=proposal.model_dump(mode="json"),
            raw=raw,
        )
        rt.background_observer.enqueue(
            frames,
            (proposal, request, raw),
            binding=rt.binding(),
            epoch=rt.screen_session_epoch,
            control=rt.playback_control_epoch,
        )
