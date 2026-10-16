import asyncio
import time
import json
from jarvis.audio.native_routing import NativeRoutingMixin
from jarvis.core.runtime import SessionRuntime
from jarvis.core.schemas import TranscriptUpdate


class ObserverRoutingMixin(NativeRoutingMixin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observed_turns = {}
        self.observed_decisions = []
        self.observed_wires = set()
        self.observed_direct_wires = set()
        self.observed_consumed = set()
        self.observed_deleted = set()
        self.observed_memory = set()
        self.observed_history = set()
        self.native_source_observations = {}
        self.observer_current = None
        self.memory_observer_pending = set()
        self.memory_observer_done = set()
        self.memory_observer_results = {}
        if self.config["native_routing"].get("delegated_continuity"):
            from jarvis.core.worker_dialogue import snapshot

            self.worker.dialogue_snapshot = lambda task: snapshot(self, task)

    def speak(
        self,
        backend=None,
        purpose="fast",
        task=None,
        source_utterance=None,
        user_text=None,
    ):
        if (
            self.config["native_routing"].get("delegated_continuity")
            and purpose == "ack"
            and (backend in {"Okay.", "好。", "我来处理一下。"})
        ):
            self.emit(
                "native.harness.ack_suppressed", reason="await_substantive_response"
            )
            return None
        return super().speak(
            backend,
            purpose,
            task=task,
            source_utterance=source_utterance,
            user_text=user_text,
        )

    def joined_observation(self, uid):
        turn = self.observed_turns[uid]
        update = turn["final"]
        identifiers = [uid]
        if not self.config["native_routing"].get("delegated_continuity"):
            return (update, identifiers)
        start = turn["start"]
        parts = [update.text]
        preceding = sorted(
            (
                (key, row)
                for key, row in self.observed_turns.items()
                if row["start"] < start and row.get("final")
            ),
            key=lambda item: item[1]["start"],
            reverse=True,
        )
        for key, row in preceding:
            final = row["final"]
            if (
                key in self.observed_consumed
                or key in self.observed_history
                or row["start"] <= self.history_audio_floor
                or (start - final.audio_end_sample > 3 * 16000)
                or (sum(map(len, parts)) + len(final.text) > 1900)
            ):
                break
            parts.insert(0, final.text)
            identifiers.insert(0, key)
            start = row["start"]
        return (update.model_copy(update={"text": " ".join(parts)}), identifiers)

    def update_pending_context(self):
        if not self.config["native_routing"].get("pending_context"):
            return
        rows = []
        for task in self.tasks.values():
            if str(task.status) in {"CANCELLED", "SUPERSEDED", "FAILED"}:
                continue
            result = self.results.get((task.task_id, task.version))
            description = (
                "Pending question: " + result.spoken_answer
                if str(task.status) == "NEEDS_INPUT" and result
                else "Status: "
                + str(task.status)
                + ". "
                + (result.spoken_answer[:90] if result else "Work is ongoing.")
            )
            rows.append(
                {
                    "task_id": task.task_id,
                    "version": task.version,
                    "objective": task.objective[:140],
                    "question": description,
                }
            )
        if getattr(self, "workspace_attachment", None):
            rows.append(
                {
                    "task_id": "attachment",
                    "version": 1,
                    "objective": "User uploaded code; questions about code, lists, copies and bugs require harness analysis of this attachment.",
                    "question": "Use harness for code questions; no guessed answer from audio alone.",
                }
            )
        voxel = getattr(self, "voxel", None)
        operations = getattr(voxel, "operations", None)
        bridge = getattr(voxel, "bridge", None)
        snapshot = getattr(bridge, "snapshot", None)
        game_task = getattr(operations, "task", None)
        if (
            getattr(self, "mode", None) == "voxel"
            and snapshot
            and game_task
            and (operations.world == snapshot.get("world_id"))
            and (game_task.get("status") not in {"CANCELLED", "FAILED"})
        ):
            rows.append(
                {
                    "task_id": "voxel:" + game_task["id"],
                    "version": operations.generation,
                    "objective": "VoxeLibre game task: " + game_task["request"][:140],
                    "question": "Status: "
                    + game_task["status"]
                    + ". "
                    + (
                        "Execution is stopped. "
                        if game_task["status"] == "PAUSED"
                        else ""
                    )
                    + "Use the harness to resume, change, cancel or inspect this specific task. A spoken answer cannot execute it. This task does not change the routing of unrelated conversation or general preferences.",
                }
            )
        if self.config["native_routing"].get("public_tool_context") or self.config[
            "native_routing"
        ].get("public_conversation_context"):
            from jarvis.core.public_service_focus import current_rows

            rows = current_rows(self)
        routing_context = rows[-6:]
        if self.config["native_routing"].get("delegated_continuity") and self.history:
            recent = self.history[-4:]
            assistant = next(
                (r["text"] for r in reversed(recent) if r["role"] == "assistant"), None
            )
            user = next(
                (r["text"] for r in reversed(recent) if r["role"] == "user"), None
            )
            if assistant and user:
                routing_context = [
                    *routing_context[-4:],
                    {
                        "objective": "Conversation: " + user[:180],
                        "question": "Assistant said: " + assistant[:240],
                    },
                    {
                        "objective": "Follow-up replies continue the conversation, including short details and corrections.",
                        "question": "Use the harness to resolve these against the previous request and answer.",
                    },
                ]
        capabilities = self.config["native_routing"].get("public_tool_context")
        if capabilities:
            routing_context = [
                {
                    "objective": "Public tools: " + capabilities,
                    "question": "DELEGATE service requests and replies to service questions to the harness. This includes diagnostic readouts, setting changes, test outcomes, identifiers and short corrections. The worker owns policy, verification and tools. DIRECT is for unrelated social speech. Ignore speech addressed elsewhere.",
                },
                *routing_context[-5:],
            ]
        elif self.config["native_routing"].get("conversation_reasoning_context"):
            routing_context = [
                {
                    "objective": "Conversation capability: the harness can reason over requests and dialogue.",
                    "question": "Use the harness for complete requests, precise details, comparisons, recommendations, calculations, choices, corrections and follow-up answers. Ordinary greetings and unrelated social conversation stay direct. Ignore speech addressed elsewhere.",
                },
                *routing_context[-5:],
            ]
        self.send_venus(
            {
                "type": "control",
                "router_context": json.dumps(routing_context, ensure_ascii=False),
            }
        )
        self.emit(
            "native.pending_context", pending=rows, routing_context=routing_context
        )

    def finish_presentation(self, response_id):
        presentation = self.presentations.get(response_id, {})
        recorded = presentation.get("history_recorded")
        super().finish_presentation(response_id)
        if (
            self.config["native_routing"].get("delegated_continuity")
            and (not recorded)
            and presentation.get("history_recorded")
        ):
            self.update_pending_context()

    async def accept_result(self, result):
        await super().accept_result(result)
        self.update_pending_context()

    def start_worker(self, *a, **kw):
        task = super().start_worker(*a, **kw)
        if task:
            task.memory = list(self.profile)
            if self.config["native_routing"].get("delegated_continuity"):
                limit = self.config["native_routing"].get(
                    "conversation_history_turns", 32
                )
                task.history = list(self.history[-limit:])
                if getattr(self, "workspace_attachment", None):
                    task.history.append(
                        {
                            "role": "user",
                            "text": "User-provided workspace attachment: "
                            + self.workspace_attachment["text"],
                            "source": "user_pasted",
                        }
                    )
                from jarvis.core.worker_dialogue import prior_questions

                task.observations = list(task.observations) + [
                    {
                        "prior_question_results": prior_questions(self, task),
                        "prior_question_results_note": "Historical worker proposals, oldest to newest. They may already have been answered, changed or superseded in the conversation. Use later user replies and actual tool observations as current evidence. A proposed question may not have been delivered, and a delivered instruction does not prove the user performed the action.",
                    }
                ]
                if not task.provisional:
                    from jarvis.core.speech_observations import prior_observations

                    update = a[0] if a else kw["update"]
                    speech = prior_observations(self, update)
                    task.observations = [
                        row
                        for row in task.observations
                        if "unconfirmed_speech_observations" not in row
                    ]
                    if speech:
                        task.observations = list(task.observations) + [
                            {"unconfirmed_speech_observations": speech}
                        ]
                        self.emit(
                            "worker.speech_context",
                            task_id=task.task_id,
                            utterance_ids=[row["utterance_id"] for row in speech],
                            admitted_audio_end_sample=update.audio_end_sample,
                            source="observed_asr_after_native_decision",
                        )
        self.update_pending_context()
        return task

    def invalidate(self, *a, **kw):
        result = super().invalidate(*a, **kw)
        self.update_pending_context()
        return result

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
        tolerance = 6
        if (
            self.config["native_routing"].get("delegated_continuity")
            and uid not in self.observed_consumed
            and (uid not in self.observed_history)
        ):
            tolerance = self.config["native_routing"].get("unhandled_binding_age_s", 20)
        if final and sample > final.audio_end_sample + tolerance * 16000:
            return None
        return uid

    async def handle(self, kind, payload):
        if (
            kind
            in {"asr.partial", "asr.final", "user.speech.started", "user.speech.ended"}
            and payload["utterance_id"] in self.observed_deleted
        ):
            return
        if kind == "memory.confirm_ready":
            self.speak(
                payload["text"], "confirmation", source_utterance=payload["source"]
            )
            return
        if kind in {"user.speech.started", "user.speech.ended"}:
            uid = payload["utterance_id"]
            if kind == "user.speech.started":
                self.user_speaking = True
                if self.response_id:
                    self.clear_playback("user_voice_interrupt")
                if getattr(self, "mode", None) == "voxel":
                    self.update_pending_context()
                self.observed_turns.setdefault(uid, {"start": payload["audio_sample"]})
                self.observer_current = uid
                if self.config["native_routing"].get("delegated_continuity"):
                    self.send_venus(
                        {
                            "type": "control",
                            "router_speech_start_sample": payload["audio_sample"],
                        }
                    )
            elif uid in self.observed_turns:
                self.user_speaking = False
                self.quiet_since = time.monotonic()
                self.observed_turns[uid]["speech_end"] = payload["audio_sample"]
                if self.config["native_routing"].get("delegated_continuity"):
                    self.send_venus(
                        {
                            "type": "control",
                            "router_speech_end_sample": payload["audio_sample"],
                            "router_endpoint_observed_sample": payload[
                                "endpoint_observed_sample"
                            ],
                        }
                    )
                if self.observer_current == uid:
                    self.observer_current = None
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
                turn["final_at"] = time.monotonic()
                if (
                    self.config["native_routing"].get("independent_memory")
                    and update.audio_end_sample > self.history_audio_floor
                ):
                    self.observed_memory.add(update.utterance_id)
                    from jarvis.core.observer_memory import observe

                    self.spawn(observe(self, update, event.event_id), memory=True)
            await self.flush_observations()
            return
        if kind == "native.observer.expire":
            for decision in self.observed_decisions:
                if decision["wire"] == payload["wire"] and (not decision["done"]):
                    decision["done"] = True
                    self.send_venus(
                        {
                            "type": "control",
                            "router_consumed_sample": decision["sample"],
                        }
                    )
                    self.emit(
                        "native.delegate.unbound",
                        response_id=decision["wire"],
                        reason="no_aligned_final_asr",
                        audio_end_sample=decision["sample"],
                    )
            return
        if kind == "native.observer.flush":
            await self.flush_observations()
            return
        if kind == "native.observer.fragment_flush":
            for decision in self.observed_decisions:
                if decision["wire"] == payload["wire"]:
                    decision["fragment_wakeup_pending"] = False
            await self.flush_observations()
            return
        await super().handle(kind, payload)
        if kind in {"decision.ready", "remote.route.ready", "native.idle"}:
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

        async def expire():
            await asyncio.sleep(
                self.config["native_routing"].get("binding_timeout_s", 20)
            )
            self.post("native.observer.expire", {"wire": wire})

        self.spawn(expire())
        await self.flush_observations()

    def venus_step(self, message):
        if (
            message.get("native_foreground")
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
            self.post("native.observer.flush")
        super().venus_step(message)

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
                import re

                explicit_edit = bool(
                    self.tasks
                    and re.search(
                        "update (?:this|the) game|(?:use|switch to) (?:a )?(?:light|dark) theme|游戏.*(?:改成|加一个|改为)|打开看看|let.s try the game|what is ready|复盘",
                        update.text,
                        re.I,
                    )
                )
                continue
            if self.route_pending:
                continue
            if (
                self.config["native_routing"].get("delegated_continuity")
                and uid not in self.observed_consumed
            ):
                from jarvis.core.identifier_fragments import fragment_binding

                uid, delay = fragment_binding(
                    self.observed_turns,
                    decision,
                    uid,
                    self.observed_consumed
                    | self.observed_history
                    | self.observed_deleted,
                    time.monotonic(),
                    self.config["native_routing"].get("binding_timeout_s", 20),
                )
                decision["uid"] = uid
                turn = self.observed_turns[uid]
                update = turn["final"]
                if delay:
                    if not decision.get("fragment_wakeup_pending"):
                        decision["fragment_wakeup_pending"] = True

                        async def wake(wire=decision["wire"], wait=delay):
                            await asyncio.sleep(wait)
                            self.post("native.observer.fragment_flush", {"wire": wire})

                        self.spawn(wake())
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
            update, source_ids = self.joined_observation(uid)
            self.observed_consumed.update(source_ids)
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
            self.emit(
                "native.delegate.bound",
                response_id=decision["wire"],
                utterance_id=uid,
                proposal=decision["proposal"],
                request=update.text,
                source_utterance_ids=source_ids,
                decision_audio_sample=decision["sample"],
                asr_audio_sample=update.audio_end_sample,
                binding_wait_ms=(time.monotonic() - decision["at"]) * 1000,
                identifier_fragment_grace=bool(decision.get("identifier_grace")),
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
                        "_bound": True,
                        "_observation_id": uid,
                    }
                )
            finally:
                self._binding_observation_id = None

    def record_observed_history(self, uid, turn):
        if uid in self.observed_history:
            return
        update, source_ids = self.joined_observation(uid)
        if not update.text.strip():
            return
        self.observed_history.update(source_ids)
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
            self.spawn(
                self.memory.extract(
                    self.user_id, update.text, turn["event_id"], self.worker, self.emit
                ),
                memory=True,
            )

    def start_route(self, update, **kwargs):
        if update.utterance_id not in self.native_sources or getattr(
            self, "mode", None
        ) in {"pvz_web", "voxel"}:
            return super().start_route(update, **kwargs)
        epoch = self.decision_epochs.get(update.utterance_id, 0) + 1
        self.decision_epochs[update.utterance_id] = epoch
        context = self.context()
        context["history"] = list(self.history[-12:])
        context["pending_questions"] = [
            {
                "objective": task.objective,
                "question": self.results[task.task_id, task.version].spoken_answer,
            }
            for task in self.tasks.values()
            if str(task.status) == "NEEDS_INPUT"
            and (task.task_id, task.version) in self.results
        ][-4:]
        playback_epoch = self.playback_control_epoch

        async def decide():
            from jarvis.core.delegated_route import route

            decision = await route(self.controller, update, epoch, context)
            decision.raw["playback_control_epoch"] = playback_epoch
            self.post("decision.ready", decision)

        return self.spawn(decide())

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


def runtime_factory(expansion):
    if expansion:
        from jarvis.games.runtime import ExpansionRuntime

        class ObserverExpansionRuntime(ObserverRoutingMixin, ExpansionRuntime):
            pass

        return ObserverExpansionRuntime

    class ObserverSessionRuntime(ObserverRoutingMixin, SessionRuntime):
        pass

    return ObserverSessionRuntime
