import asyncio
import time
from jarvis.omni.native_routing import NativeRoutingMixin
from jarvis.omni.runtime import ScreenRuntime
from jarvis.core.schemas import TranscriptUpdate
from jarvis.core.language import response_language
from jarvis.omni.native_scene import NativeSceneMixin


class ObserverRoutingMixin(NativeRoutingMixin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observed_turns = {}
        self.observed_decisions = []
        self.observed_wires = set()
        self.observed_direct_wires = set()
        self.native_handoff_pending = set()
        self.observed_consumed = set()
        self.observed_memory = set()
        self.observed_history = set()
        self.native_source_observations = {}
        self.observer_current = None

    def aligned_turn(self, sample):
        rows = [
            (uid, t)
            for uid, t in self.observed_turns.items()
            if t["start"] <= sample and t["start"] > self.history_audio_floor
        ]
        if not rows:
            return None
        uid, turn = max(rows, key=lambda x: x[1]["start"])
        final = turn.get("final")
        if final and sample > final.audio_end_sample + 6 * 16000:
            return None
        return uid

    async def handle(self, kind, payload):
        if kind == "venus.native.route":
            self.emit("native.route.marker", **payload)
            if payload.get("generation_epoch") != self.generation_epoch:
                return
            interrupted = payload.get("interrupted_backend_response_id")
            if interrupted and payload["action"] == 1:
                self.native_handoff_pending.add(payload["response_id"])
            if interrupted and interrupted == self.response_id:
                presentation = self.presentations[interrupted]
                presentation["interrupted"] = True
                presentation["interruption_reason"] = "native_route"
                self.response_id = None
                self.send_client(
                    {
                        "type": "playback.clear",
                        "generation_epoch": self.generation_epoch,
                        "reason": "native_route",
                    }
                )
                self.finish_presentation(interrupted)
                self.emit(
                    "native.backend.interrupted",
                    response_id=interrupted,
                    native_response_id=payload["response_id"],
                    action=payload["action"],
                )
            if (
                payload["action"] == 1
                and payload["response_id"] not in self.observed_direct_wires
            ):
                wire = payload["response_id"]
                self.observed_direct_wires.add(wire)
                self.observed_decisions.append(
                    {
                        "wire": wire,
                        "sample": payload["audio_end_sample"],
                        "kind": "direct",
                        "done": False,
                    }
                )
                self.schedule_binding_expiry(wire)
                await self.flush_observations()
            if payload["action"] == 2:
                await self.dispatch_native(
                    {
                        "text": "",
                        "response_id": payload["response_id"],
                        "audio_end_sample": payload["audio_end_sample"],
                    }
                )
            return
        if kind in {"user.speech.started", "user.speech.ended"}:
            uid = payload["utterance_id"]
            if kind == "user.speech.started":
                self.observed_turns.setdefault(uid, {"start": payload["audio_sample"]})
                self.observer_current = uid
                if self.native_stopped:
                    self.native_stopped = False
                    self.muted = False
                    self.send_venus(
                        {
                            "type": "control",
                            "router_consumed_sample": payload["audio_sample"],
                        }
                    )
                    self.send_venus(
                        {
                            "type": "control",
                            "native_resume": True,
                            "generation_epoch": self.generation_epoch,
                        }
                    )
                if self.response_id:
                    self.clear_playback("user_barge_in")
                self.user_speaking = True
            elif uid in self.observed_turns:
                self.observed_turns[uid]["speech_end"] = payload["audio_sample"]
                if self.observer_current == uid:
                    self.observer_current = None
                self.user_speaking = False
            self.emit("native.observer." + kind, **payload)
            await self.flush_observations()
            return
        if kind in {"asr.partial", "asr.final"}:
            update = TranscriptUpdate.model_validate(payload)
            turn = self.observed_turns.setdefault(
                update.utterance_id, {"start": max(0, update.audio_end_sample - 16000)}
            )
            previous = turn.get("latest")
            if previous and (previous.is_final or previous.revision >= update.revision):
                return
            turn["latest"] = update
            event = self.emit("native.observer." + kind, **update.model_dump())
            if update.is_final:
                turn["final"] = update
                turn["event_id"] = event.event_id
            await self.flush_observations()
            return
        if kind == "native.observer.expire":
            released = payload["wire"] in self.native_handoff_pending
            self.native_handoff_pending.discard(payload["wire"])
            for decision in self.observed_decisions:
                if decision["wire"] == payload["wire"] and (not decision["done"]):
                    released = True
                    decision["done"] = True
                    self.send_venus(
                        {
                            "type": "control",
                            "router_consumed_sample": decision["sample"],
                        }
                    )
                    self.emit(
                        "native." + decision["kind"] + ".unbound",
                        response_id=decision["wire"],
                        reason="no_aligned_final_asr",
                        audio_end_sample=decision["sample"],
                    )
            if released:
                self.post("native.idle")
            return
        if kind == "native.observer.flush":
            await self.flush_observations()
            return
        if kind == "native.idle" and (
            self.native_handoff_pending
            or any((not d["done"] for d in self.observed_decisions))
        ):
            return
        await super().handle(kind, payload)
        if kind in {
            "decision.ready",
            "remote.route.ready",
            "native.idle",
            "screen.route.ready",
            "screen.user_control.ready",
        }:
            await self.flush_observations()

    async def dispatch_native(self, payload):
        if payload.get("_bound"):
            before = set(self.native_sources)
            await super().dispatch_native(payload)
            for source in self.native_sources - before:
                self.native_source_observations[source] = payload["_observation_id"]
            return
        wire = payload["response_id"]
        if wire in self.observed_wires:
            self.emit("native.delegate_duplicate", response_id=wire)
            return
        self.observed_wires.add(wire)
        decision = {
            "wire": wire,
            "sample": payload["audio_end_sample"],
            "proposal": payload["text"],
            "at": time.monotonic(),
            "done": False,
            "kind": "delegate",
        }
        self.observed_decisions.append(decision)
        self.emit(
            "native.route.selected",
            route="DELEGATE",
            response_id=wire,
            audio_end_sample=decision["sample"],
            proposal=decision["proposal"],
            asr_required=False,
            jev_admission=False,
        )
        self.schedule_binding_expiry(wire)
        await self.flush_observations()

    def schedule_binding_expiry(self, wire):

        async def expire():
            await asyncio.sleep(
                self.config["native_routing"].get("binding_timeout_s", 20)
            )
            self.post("native.observer.expire", {"wire": wire})

        self.spawn(expire())

    def venus_step(self, message):
        if (
            message.get("native_foreground")
            and message.get("generation_epoch") == self.generation_epoch
            and (not message.get("dropped_stale_epoch"))
            and (
                message.get("text", "").strip()
                or message.get("pcm")
                or message.get("end_of_turn")
            )
        ):
            self.native_handoff_pending.discard(message["response_id"])
        if "native_audio_end_sample" in message:
            message = {
                **message,
                "audio_end_sample": message["native_audio_end_sample"],
            }
        if (
            not self.native_stopped
            and message.get("native_foreground")
            and (not message.get("is_listen"))
            and (message.get("text", "").strip() or message.get("pcm"))
            and (message.get("generation_epoch") == self.generation_epoch)
            and (message["response_id"] not in self.observed_direct_wires)
        ):
            wire = message["response_id"]
            self.observed_direct_wires.add(wire)
            self.observed_decisions.append(
                {
                    "wire": wire,
                    "sample": message.get("audio_end_sample", 0),
                    "kind": "direct",
                    "done": False,
                }
            )
            self.schedule_binding_expiry(wire)
            self.post("native.observer.flush")
        super().venus_step(message)

    def clear_playback(self, reason):
        self.native_handoff_pending.clear()
        super().clear_playback(reason)

    async def flush_observations(self):
        for decision in self.observed_decisions:
            if decision["done"]:
                continue
            uid = decision.get("uid") or self.aligned_turn(decision["sample"])
            if uid is None:
                continue
            decision["uid"] = uid
            turn = self.observed_turns[uid]
            update = turn.get("final")
            if update is None:
                continue
            if update.audio_end_sample <= self.history_audio_floor:
                decision["done"] = True
                self.send_venus(
                    {"type": "control", "router_consumed_sample": decision["sample"]}
                )
                continue
            if decision["kind"] == "direct":
                decision["done"] = True
                if uid in self.observed_consumed:
                    continue
                self.record_observed_history(uid, turn)
                self.emit(
                    "native.direct.bound",
                    wire_response_id=decision["wire"],
                    utterance_id=uid,
                    decision_audio_sample=decision["sample"],
                    asr_audio_sample=update.audio_end_sample,
                )
                continue
            if self.route_pending:
                continue
            decision["done"] = True
            if uid in self.observed_consumed:
                self.emit(
                    "native.delegate_duplicate",
                    response_id=decision["wire"],
                    utterance_id=uid,
                )
                self.send_venus(
                    {
                        "type": "control",
                        "router_consumed_sample": max(
                            update.audio_end_sample, decision["sample"]
                        ),
                    }
                )
                continue
            self.observed_consumed.add(uid)
            if not update.text.strip():
                self.emit(
                    "native.delegate.unbound",
                    response_id=decision["wire"],
                    reason="empty_final_asr",
                )
                self.send_venus(
                    {
                        "type": "control",
                        "router_consumed_sample": max(
                            update.audio_end_sample, decision["sample"]
                        ),
                    }
                )
                continue
            self.send_venus(
                {"type": "control", "router_consumed_sample": update.audio_end_sample}
            )
            self.response_language = response_language(
                update.text, update.language, self.config.get("speech_language", "auto")
            )
            self.emit(
                "native.delegate.bound",
                response_id=decision["wire"],
                utterance_id=uid,
                proposal=decision["proposal"],
                request=update.text,
                decision_audio_sample=decision["sample"],
                asr_audio_sample=update.audio_end_sample,
                binding_wait_ms=(time.monotonic() - decision["at"]) * 1000,
                source_kind="observed_asr_after_native_decision",
            )
            self.history[:] = [
                r for r in self.history if r.get("observer_utterance_id") != uid
            ]
            self._binding_observation_id = uid
            try:
                await self.dispatch_native(
                    {
                        "text": update.text,
                        "response_id": decision["wire"],
                        "audio_end_sample": update.audio_end_sample,
                        "language": update.language,
                        "_bound": True,
                        "_observation_id": uid,
                    }
                )
            finally:
                self._binding_observation_id = None
        active = {d.get("uid") for d in self.observed_decisions if not d["done"]}
        keep = set(list(self.observed_turns)[-256:]) | active | {self.observer_current}
        for uid in list(self.observed_turns):
            if uid not in keep:
                self.observed_turns.pop(uid)
        self.observed_consumed.intersection_update(keep)
        self.observed_memory.intersection_update(keep)
        self.observed_history.intersection_update(keep)
        done = [d for d in self.observed_decisions if d["done"]][-256:]
        self.observed_decisions = [
            d for d in self.observed_decisions if not d["done"]
        ] + done
        wires = {d["wire"] for d in self.observed_decisions}
        self.observed_wires.intersection_update(wires)
        self.observed_direct_wires.intersection_update(wires)

    def record_observed_history(self, uid, turn):
        if uid in self.observed_history:
            return
        update = turn["final"]
        if not update.text.strip():
            return
        self.observed_history.add(uid)
        self.observed_consumed.add(uid)
        self.transcripts[uid] = update
        self.response_language = response_language(
            update.text, update.language, self.config.get("speech_language", "auto")
        )
        self.current_utterance = uid
        self.focus_epoch += 1
        self.route_pending = False
        memory_done = uid in self.observed_memory
        self.observed_memory.add(uid)
        self.send_venus(
            {"type": "control", "router_consumed_sample": update.audio_end_sample}
        )
        self.history.append(
            {
                "role": "user",
                "text": update.text,
                "source_event_id": turn["event_id"],
                "observer_utterance_id": uid,
            }
        )
        self.emit(
            "native.observer.addressed",
            utterance_id=uid,
            source="native_foreground_engagement",
        )
        if self.config["memory"].get("enabled") and (not memory_done):
            self.schedule_personal_memory(update, turn["event_id"])
            self.release_personal_memory(update)
        if hasattr(self, "native_status_context"):
            self.native_status_context()

    async def apply_decision(self, decision):
        if (
            decision.utterance_id in self.native_sources
            and decision.route == "FAST"
            and self.config["native_routing"].get("resolve_delegated_fast", True)
        ):
            self.emit(
                "native.harness.fast_resolved",
                utterance_id=decision.utterance_id,
                original_route="FAST",
                resolution="worker_answer",
            )
            decision = decision.model_copy(update={"route": "WORKER"})
        await super().apply_decision(decision)


class NativeScreenRuntime(NativeSceneMixin, ObserverRoutingMixin, ScreenRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_context_state = {}

    def set_local_preference(self, text, *, source, source_id):
        super().set_local_preference(text, source=source, source_id=source_id)
        if self.config["native_routing"].get("proactive"):
            self.cancel_native_scene("preference_changed")
            self.native_status_context()

    def native_status_context(self, **extra):
        from .task_context import task_status_snapshot

        snapshot = task_status_snapshot(self, None, max_tasks=6)
        from .native_context import prior_observations

        self.native_context_state.update(extra)
        self.native_context_state["prior_observations"] = prior_observations(self)
        self.send_venus(
            {
                "type": "control",
                "generation_epoch": self.generation_epoch,
                "native_context": {
                    "task_observations": snapshot,
                    **self.native_context_state,
                },
            }
        )
        self.emit(
            "native.status_context",
            task_count=len(snapshot["tasks"]),
            context_kind=next(iter(extra), "task_snapshot"),
        )

    async def dispatch_native(self, payload):
        if payload.get("_bound"):
            self.native_status_context(
                handoff={
                    "request": payload["text"],
                    "status": "requested_not_completed",
                },
                operation_outcome=None,
            )
        return await super().dispatch_native(payload)

    async def handle(self, kind, payload):
        if kind == "history.reset.ready":
            self.native_context_state.clear()
            if self.history_reset_pending:
                samples = [
                    turn["latest"].audio_end_sample
                    for turn in self.observed_turns.values()
                    if turn.get("latest")
                ]
                self.history_audio_floor = max([self.history_audio_floor] + samples)
                self.observed_turns.clear()
                self.observed_decisions.clear()
                self.observed_wires.clear()
                self.observed_direct_wires.clear()
                self.native_handoff_pending.clear()
                self.observed_consumed.clear()
                self.observed_memory.clear()
                self.observed_history.clear()
                self.native_delegate_queue.clear()
                self.native_backend_queue.clear()
                self.native_wire_map.clear()
                self.native_context_state.clear()
                self.send_venus(
                    {
                        "type": "control",
                        "router_consumed_sample": self.history_audio_floor,
                    }
                )
        await super().handle(kind, payload)
        if kind in {
            "worker.started",
            "worker.completed",
            "decision.ready",
            "screen.route.ready",
            "screen.user_control.ready",
            "history.reset.ready",
        }:
            self.native_status_context()

    def respond_to_screen_outcome(
        self, outcome, utterance, *, native_only=False, capture_required=True
    ):
        self.native_status_context(operation_outcome=outcome)
        return super().respond_to_screen_outcome(
            outcome,
            utterance,
            native_only=native_only,
            capture_required=capture_required,
        )

    def finish_generation(self, presentation):
        was_done = presentation.get("generated_done")
        super().finish_generation(presentation)
        if (
            not was_done
            and presentation.get("generated_done")
            and (not presentation.get("history_recorded"))
            and self.config["native_routing"].get("pending_delivery_context")
        ):
            self.native_status_context()

    def finish_presentation(self, response_id):
        before = len(self.history)
        super().finish_presentation(response_id)
        if len(self.history) != before:
            self.native_status_context()
