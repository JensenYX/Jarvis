import asyncio
import copy
import json
import re
import time
from jarvis.core.runtime import SessionRuntime
from jarvis.core.schemas import Task, TaskStatus, TranscriptUpdate, new_id
from jarvis.games.worker import ExpansionWorker
from jarvis.games.remote import RemoteGameAdapter, BridgeError
from jarvis.games import coach
from jarvis.games.guidance import interface_help, failed_help
from jarvis.games.autoplay import PvZAutoplay


class ExpansionRuntime(PvZAutoplay, SessionRuntime):
    def __init__(self, config, *args, **kwargs):
        config = copy.deepcopy(config)
        config["runtime"]["max_active_workers"] = 1
        config["prefix"]["enabled"] = False
        super().__init__(config, *args, **kwargs)
        self.matched_task_ids = []
        conversation_fields = frozenset(self.__dict__) | {"erase_recordings"}
        options = config["game_expansion"]
        self.remote = RemoteGameAdapter(
            self.session_id,
            self.bridge_event,
            request_timeout=options.get("request_timeout_s", 3),
            heartbeat_timeout=options.get("heartbeat_timeout_s", 3),
            max_origin_age=options.get("max_origin_age_s", 3),
        )
        if kwargs.get("worker") is None:
            self.worker = ExpansionWorker(
                config, self.tools, self.emit, client=self.worker.client
            )
        self.registry = None
        self.route_origins = {}
        self.routed = set()
        self.advice_epoch = 0
        self.task_bindings = {}
        self.current_analysis = None
        self.preparing_binding = None
        self.analysis_epoch = 0
        self.pending_advice = None
        self.last_advice = None
        self.offered_advice = None
        self.last_proactive = 0.0
        self.proactive_enabled = True
        self.pending_help = None
        self.setup_announced = None
        self.previous_snapshot = None
        self.game_speech_epoch = 0
        self.seed_recommendation = None
        self.tactical_target = None
        self.last_game_request = None
        self.game_request_answered = set()
        self.route_lock = asyncio.Lock()
        self.game_route_job = None
        self.pending_advice_request = None
        self.choice_lock = asyncio.Lock()
        self.action_job = None
        self.choice_job = None
        self.choice_pending = None
        self.lease_job = None
        self.init_autoplay()
        self.story = None
        self.receipts = []
        self.text_mode = options.get("text_mode", False)
        self.mode = None
        self.voxel = None
        if config.get("voxel", {}).get("enabled"):
            from jarvis.games.voxel.runtime import VoxelMode
            from jarvis.games.voxel.mailbox import SnapshotQueue

            self.queue = SnapshotQueue(actor=True)
            self.voxel = VoxelMode(self)
        downstream = self.send_client
        from .voxel.browser import BrowserDelivery

        browser = BrowserDelivery()

        def send(message):
            if message.get("type") == "audio":
                binding = self.presentations.get(message["response_id"], {}).get(
                    "remote_binding"
                )
                if binding:
                    message = {
                        **message,
                        "remote_binding": {
                            **binding,
                            "remaining_ms": max(
                                0, int((binding["expires"] - time.monotonic()) * 1000)
                            ),
                        },
                    }
                game_binding = self.presentations.get(message["response_id"], {}).get(
                    "game_binding"
                )
                if game_binding:
                    message = {**message, "game_binding": game_binding}
            if self.mode == "voxel" and (
                not self.text_mode or self.config.get("voxel", {}).get("browser_view")
            ):
                message = browser.message(message)
            if message is not None:
                downstream(message)

        self.send_client = send

    def bridge_event(self, event_type, **payload):
        self.post("remote.event", {"type": event_type, "payload": payload})

    async def run(self):
        self.ensure_lease()
        await super().run()

    def ensure_lease(self):
        if self.lease_job is None or self.lease_job.done():
            self.lease_job = self.spawn(self.lease())

    async def lease(self):
        while not self.closed:
            await asyncio.sleep(0.5)
            self.remote.check_lease()
            self.post("remote.lease", {})

    async def select_game(self, game_id):
        if game_id not in {"pvz_web", "voxel"} or (
            game_id == "voxel" and (not self.voxel)
        ):
            raise ValueError("Unsupported game")
        if self.mode == "voxel":
            await self.voxel.deactivate()
        self.stop_autoplay("game_changed", revoke=False)
        self.seed_recommendation = None
        self.tactical_target = None
        self.last_game_request = None
        if self.choice_job:
            self.choice_job.cancel()
            self.choice_job = None
        self.choice_generation = getattr(self, "choice_generation", 0) + 1
        self.mode = game_id
        self.remote.transport_owner = None
        self.game_speech_epoch += 1
        self.pending_advice = None
        self.last_advice = None
        self.offered_advice = None
        self.pending_help = None
        self.setup_announced = None
        self.advice_epoch += 1
        self.receipts.clear()
        self.previous_snapshot = None
        self.choice_pending = None
        if self.response_id and self.presentations[self.response_id].get(
            "remote_binding"
        ):
            self.clear_playback("active_game_changed")
        for task_id in list(self.task_bindings):
            task = self.tasks.get(task_id)
            if task:
                self.invalidate(task, TaskStatus.SUPERSEDED)

        async def send(message):
            self.send_client({"type": "game.bridge.send", "message": message})

        await self.remote.activate(game_id, send)
        token = self.registry.issue(self.remote) if self.registry else None
        if game_id == "voxel":
            token = await self.voxel.activate()
        self.send_client(
            {
                "type": "game.selected",
                "game_id": game_id,
                "active_game_epoch": self.remote.active_game_epoch,
                "session_id": self.session_id,
                "pairing_token": token,
            }
        )

    def binding(self, scope="position", *, snapshot=None, action=None, field=None):
        b = self.remote.binding
        s = snapshot or self.remote.snapshot
        if not b or not s:
            return None
        result = {
            **b.__dict__,
            "scope": scope,
            "decision_id": s.decision_id,
            "expires": time.monotonic() + (6 if b.game_id == "pvz_web" else 15),
        }
        result["host_observed_at_ms"] = s.observed_at_host_mono_ms
        result["ttl_ms"] = 6000 if b.game_id == "pvz_web" else 15000
        if field:
            result["field"] = field
            result["value"] = getattr(s.view, field, None)
        if action:
            result["action"] = action.model_dump(mode="json")
            if s.view.game == "pvz_web" and action.facts.row:
                row = action.facts.row
                result["lane_zombies"] = [
                    [z.entity_id, z.zone, z.state]
                    for z in s.view.zombies
                    if z.row == row
                ]
                result["lane_plants"] = [
                    [p.entity_id, p.col, p.state] for p in s.view.plants if p.row == row
                ]
                result["cell_plants"] = [
                    p.entity_id
                    for p in s.view.plants
                    if p.row == row and p.col == action.facts.col
                ]
                if s.view.column_x:
                    x = s.view.column_x[action.facts.col - 1]
                    result["zombie_side"] = [
                        [z.entity_id, z.x >= x] for z in s.view.zombies if z.row == row
                    ]
        if scope == "interface":
            result["expires"] = time.monotonic() + 12
            result["ui_stage"] = getattr(s.view, "ui_stage", "unknown")
            result["phase"] = s.phase
        elif not field and (not action or action.kind == "WAIT"):
            result["position_key"] = self.position_key(s)
        if scope == "analysis":
            result["ttl_ms"] = (
                self.config["game_expansion"].get("worker_timeout_s", 20) * 1000
            )
            result["expires"] = time.monotonic() + result["ttl_ms"] / 1000
        return result

    @staticmethod
    def position_key(snapshot):
        v = snapshot.view
        if v.game != "pvz_web":
            return snapshot.decision_id
        return {
            "phase": snapshot.phase,
            "sun": v.sun,
            "seeds": [[c.seed_id, c.ready, c.cost] for c in v.seed_bank],
            "plants": [[p.entity_id, p.row, p.col, p.state] for p in v.plants],
            "zombies": [[z.entity_id, z.row, z.zone, z.state] for z in v.zombies],
        }

    def valid(self, binding, *, referenced_action=False):
        if not binding:
            return True
        if binding.get("voxel"):
            return bool(self.voxel and self.voxel.valid(binding))
        b = self.remote.binding
        if (
            not b
            or not self.remote.connected
            or any((binding[k] != v for k, v in b.__dict__.items()))
        ):
            return False
        if binding["scope"] in {"history", "receipt", "game"}:
            return True
        s = self.remote.snapshot
        if binding["scope"] == "interface":
            return bool(
                s
                and time.monotonic() < binding["expires"]
                and (s.phase == binding["phase"])
                and (getattr(s.view, "ui_stage", "unknown") == binding["ui_stage"])
            )
        if not s or not s.focused or s.paused:
            return False
        if not referenced_action:
            if time.monotonic() >= binding["expires"]:
                return False
            if s.observed_at_host_mono_ms - binding.get(
                "host_observed_at_ms", s.observed_at_host_mono_ms
            ) > binding.get("ttl_ms", 15000):
                return False
        if binding.get("field"):
            return getattr(s.view, binding["field"], None) == binding["value"]
        if "position_key" in binding:
            return self.position_key(s) == binding["position_key"]
        if s.view.game == "pvz_web" and binding.get("action"):
            action = binding["action"]
            facts = action["facts"]
            row = facts.get("row")
            if action["kind"] == "COLLECT_SUN":
                return s.phase == "playing" and any(
                    (sun.entity_id == facts["target_id"] for sun in s.view.suns)
                )
            if not row:
                return s.decision_id == binding["decision_id"]
            if (
                s.phase != "playing"
                or not s.view.normal_rules
                or s.view.auto_collect_sun
            ):
                return False
            if [
                [z.entity_id, z.zone, z.state] for z in s.view.zombies if z.row == row
            ] != binding.get("lane_zombies", []):
                return False
            if [
                [p.entity_id, p.col, p.state] for p in s.view.plants if p.row == row
            ] != binding.get("lane_plants", []):
                return False
            if [
                p.entity_id
                for p in s.view.plants
                if p.row == row and p.col == facts["col"]
            ] != binding.get("cell_plants", []):
                return False
            if "zombie_side" in binding:
                x = s.view.column_x[facts["col"] - 1]
                if [
                    [z.entity_id, z.x >= x] for z in s.view.zombies if z.row == row
                ] != binding["zombie_side"]:
                    return False
            return any(
                (
                    c.seed_id == facts["seed_id"]
                    and c.ready
                    and (s.view.sun is not None)
                    and (s.view.sun >= facts["cost"])
                    for c in s.view.seed_bank
                )
            )
        return s.decision_id == binding["decision_id"]

    def speak(
        self,
        backend=None,
        purpose="fast",
        task=None,
        source_utterance=None,
        user_text=None,
    ):
        binding = self.preparing_binding or (
            self.task_bindings.get(task[0]) if task else None
        )
        if not self.valid(binding):
            return None
        if self.text_mode:
            if self.user_speaking or self.muted:
                return None
            text = (
                backend
                or "文字调试模式：一般聊天请开启语音；游戏事实和后台结果会显示在这里。"
            )
            self.emit("assistant.text", text=text, purpose=purpose, binding=binding)
            self.history.append(
                {"role": "assistant", "text": text, "actually_played": False}
            )
            if task and purpose == "result":
                self.committed.add(task)
            return None
        rid = super().speak(backend, purpose, task, source_utterance, user_text)
        if rid:
            self.presentations[rid]["remote_binding"] = binding
            self.emit("remote.speech.bound", response_id=rid, binding=binding)
        return rid

    def speak_game(self, text, purpose, binding, **kwargs):
        if text and binding and binding.get("voxel"):
            from .voxel.narration import spoken_text

            text = spoken_text(text)
        self.preparing_binding = binding
        try:
            return self.speak(text, purpose, **kwargs)
        finally:
            self.preparing_binding = None

    def expire_game_speech(self, reason):
        rid = self.response_id
        if not rid:
            return
        presentation = self.presentations[rid]
        if self.valid(presentation.get("remote_binding")):
            return
        request = presentation.get("advice_request")
        played = sum(
            (
                self.chunks.get(k, {}).get("played", 0)
                for k in presentation.get("chunks", [])
            )
        )
        incomplete = played < sum(
            (
                self.chunks.get(k, {}).get("samples", 0)
                for k in presentation.get("chunks", [])
            )
        ) or not presentation.get("generated_done")
        self.clear_playback(reason)
        if (
            request
            and incomplete
            and (not self.user_speaking)
            and (not self.route_pending)
        ):
            self.refresh_explicit_advice(request)

    def persist_task(self, task):
        if task.skill not in {"remote_game_analysis", "game_analysis"}:
            super().persist_task(task)

    async def refresh_profile(self, query=None):
        if query and self.mode == "voxel":
            self.spawn(super().refresh_profile(query))
            return
        if (
            query
            and getattr(self, "remote", None)
            and (self.remote.game_id == "pvz_web")
        ):
            self.spawn(super().refresh_profile(query))
            return
        await super().refresh_profile(query)

    def start_worker(self, update, *args, replacement=None, **kwargs):
        if replacement and replacement.skill == "remote_game_analysis":
            previous = self.tasks.get(replacement.task_id)
            if not replacement.observations and previous:
                replacement.observations = copy.deepcopy(previous.observations)
            replacement.allowed_tool_scope = ["get_game_snapshot", "lookup_game_rule"]
            replacement.expected_output = "answer"
            task = super().start_worker(
                update, *args, replacement=replacement, **kwargs
            )
            if task and (
                not replacement.observations
                or task.task_id not in self.task_bindings
                or (not self.valid(self.task_bindings[task.task_id]))
            ):
                self.invalidate(task, TaskStatus.SUPERSEDED)
            return task
        return super().start_worker(update, *args, replacement=replacement, **kwargs)

    def result_eligible(self, key):
        return self.valid(self.task_bindings.get(key[0])) and super().result_eligible(
            key
        )

    def fence_playback(self, reason, *, all_announcements=False):
        self.game_speech_epoch += 1
        self.pending_advice = None
        self.pending_help = None
        self.receipts.clear()
        self.choice_pending = None
        return super().fence_playback(reason, all_announcements=all_announcements)

    async def control(self, decision, update):
        if decision.control == "CLEAR_HISTORY" and (
            not self.stale_playback_intent(decision, update)
        ):
            if self.voxel and self.mode == "voxel":
                if self.voxel.operations:
                    self.voxel.operations.stop("history_cleared")
                    self.voxel.operations.task = None
                    self.voxel.operations.world = None
                    self.voxel.operations.identity = None
                if self.voxel.building:
                    self.voxel.building.stop("history_cleared")
                    self.voxel.building.project = None
                    self.voxel.building.world = None
                    self.voxel.building.identity = None
                self.voxel.bridge.revoke("history_cleared")
                self.voxel.epoch += 1
                self.voxel.speech_epoch += 1
                self.voxel.pending = None
                self.voxel.confirmation = None
                self.voxel.last_advice = None
                self.voxel.advice_offered = None
                self.voxel.origins.clear()
                self.voxel.memory.goal = None
                self.voxel.memory.landmarks = []
                self.voxel.memory.ids = {}
            self.stop_autoplay("history_cleared", revoke=False)
            self.seed_recommendation = None
            self.tactical_target = None
            self.last_game_request = None
            self.game_request_answered.clear()
            self.advice_epoch += 1
            self.game_speech_epoch += 1
            self.pending_advice = None
            self.offered_advice = None
            self.last_advice = None
            self.choice_pending = None
            self.receipts.clear()
            self.route_origins.clear()
            self.task_bindings.clear()
            self.current_analysis = None
            try:
                await self.remote.revoke("history_cleared", wait=False)
            except BridgeError:
                pass
        if decision.control == "REPLACE":
            old = self.delivery_task((decision.target_task_id, decision.target_version))
            if old and old.skill == "remote_game_analysis":
                self.invalidate(old, TaskStatus.SUPERSEDED)
                replacement = old.model_copy(
                    deep=True,
                    update={
                        "version": old.version + 1,
                        "status": TaskStatus.QUEUED,
                        "objective": update.text,
                        "source_utterance_id": update.utterance_id,
                        "logical_request_id": update.utterance_id,
                        "provisional": False,
                        "skill": None,
                        "observations": [],
                        "expected_output": "auto",
                        "progress": {},
                        "allowed_tool_scope": ["read_only"]
                        + (
                            ["artifact_write"]
                            if self.config["tools"].get("artifacts_enabled")
                            else []
                        ),
                        "history": self.history[-6:],
                        "memory": list(self.profile),
                    },
                )
                self.task_bindings.pop(old.task_id, None)
                self.current_analysis = None
                self.start_worker(update, replacement=replacement)
                self.speak(
                    "好的，按最新要求处理。",
                    "ack",
                    source_utterance=update.utterance_id,
                )
                return
        result = await super().control(decision, update)
        if decision.control == "CLEAR_HISTORY" and (not self.tasks):
            self.choice_job = None
            self.action_job = None
            self.ensure_lease()
            if self.text_mode:
                self.post("history.reset.ready")
        return result

    async def handle(self, type, payload):
        if self.story and type.startswith("pvz.story."):
            if type == "pvz.story.turn":
                await self.story.apply(payload)
            elif (
                type == "pvz.story.answer"
                and payload["source"] == self.current_utterance
            ):
                self.story.say(payload["text"], payload["source"])
            elif (
                type == "pvz.story.notice" and payload["binding"] == self.remote.binding
            ):
                self.story.say(payload["text"], proactive=True)
            elif type == "pvz.story.failure":
                self.route_pending = False
                self.emit(type, **{k: v for k, v in payload.items() if k != "update"})
            return
        if (
            type == "asr.final"
            and self.mode != "voxel"
            and await self.suppress_context_echo(
                TranscriptUpdate.model_validate(payload)
            )
        ):
            return
        if type == "remote.lease":
            if self.mode == "voxel":
                await self.voxel.handle(type, payload)
                return
            self.expire_game_speech("remote_content_expired")
            self.autoplay_snapshot()
            return
        if type == "game.select":
            await self.select_game(payload["game_id"])
            return
        if self.mode == "voxel":
            if await self.voxel.handle(type, payload):
                return
            await SessionRuntime.handle(self, type, payload)
            if type in {
                "playback.ack",
                "speech.checked",
                "user.speech.ended",
                "delivery.wake",
            }:
                await self.voxel.flush()
                if self.voxel.building:
                    await self.voxel.building.flush()
            return
        if type.startswith("game.") and type not in {
            "game.command",
            "game.bridge.receive",
        }:
            return
        if type == "game.bridge.receive":
            try:
                await self.remote.receive(payload)
            except Exception as error:
                self.emit(
                    "bridge.message_rejected",
                    reason="invalid_or_stale",
                    error=type(error).__name__,
                    fields=[
                        {"path": list(e["loc"]), "kind": e["type"]}
                        for e in error.errors(include_input=False)
                    ][:4]
                    if hasattr(error, "errors")
                    else [],
                    detail=str(error)[:120] if isinstance(error, BridgeError) else "",
                )
            finally:
                self.send_client(
                    {
                        "type": "game.bridge.ack",
                        "message_id": payload.get("message_id"),
                        "active_game_epoch": payload.get("active_game_epoch"),
                    }
                )
            return
        if type == "user.speech.started":
            self.route_origins[payload["utterance_id"]] = self.remote.origin(
                payload["utterance_id"]
            )
        if type == "stop_speaking":
            self.game_speech_epoch += 1
            self.pending_advice = None
            self.pending_help = None
            self.receipts.clear()
            self.choice_pending = None
        if type == "remote.event":
            kind, p = (payload["type"], payload["payload"])
            self.emit(kind, **p)
            if kind in {
                "bridge.snapshot",
                "bridge.disconnected",
                "bridge.event",
                "bridge.revoked",
            }:
                self.autoplay_snapshot()
            if kind == "bridge.disconnected":
                self.seed_recommendation = None
                self.tactical_target = None
            if kind in {"bridge.snapshot", "bridge.disconnected"}:
                self.expire_game_speech("remote_context_changed")
                for task_id, binding in list(self.task_bindings.items()):
                    task = self.tasks.get(task_id)
                    if (
                        task
                        and (not self.valid(binding))
                        and (
                            task.status
                            in {
                                TaskStatus.QUEUED,
                                TaskStatus.RUNNING,
                                TaskStatus.COMPLETED,
                            }
                        )
                    ):
                        self.invalidate(task, TaskStatus.SUPERSEDED)
            if kind == "bridge.snapshot":
                s = self.remote.snapshot
                stage = getattr(s.view, "ui_stage", "unknown") if s else None
                if (
                    s
                    and (not self.story)
                    and self.proactive_enabled
                    and (stage in {"title", "menu", "select_cards"})
                    and (self.setup_announced != (self.remote.binding, stage))
                ):
                    self.setup_announced = (self.remote.binding, stage)
                    self.show_help(
                        interface_help(s), s, source="interface", proactive=True
                    )
                if (
                    s
                    and (not self.story)
                    and (not self.autoplay_run)
                    and self.proactive_enabled
                    and coach.meaningful(s, self.previous_snapshot)
                ):
                    self.schedule_advice(s, proactive=True)
                self.previous_snapshot = s
                if self.story:
                    self.story.observe()
            return
        if type == "game.command":
            command = payload.get("action")
            if command == "reclaim":
                self.advice_epoch += 1
                self.choice_pending = None
                self.stop_autoplay("user_reclaimed", revoke=False)
                self.remote.fence_control()
                self.spawn(self.reclaim())
                return
            if command == "proactive":
                self.proactive_enabled = bool(payload.get("enabled"))
                return
            if command == "cancel_analysis":
                self.cancel_analysis()
                return
            if command in {"ask_move", "play_once", "autoplay"}:
                event = self.emit("remote.user.command", action=command)
                self.muted = False
                self.pending_help = None
                self.advice_epoch += 1
                origin = self.remote.origin(event.event_id, client=payload)
                if origin is None:
                    self.emit("remote.action.failed", reason="stale_button_context")
                    self.show_help(
                        failed_help(self.remote.snapshot, "stale_button_context"),
                        self.remote.snapshot,
                    )
                    return
                if command == "autoplay":
                    if self.remote.game_id == "pvz_web":
                        self.control_generation = (
                            getattr(self, "control_generation", 0) + 1
                        )
                        self.autoplay_start_job = self.spawn(
                            self.begin_autoplay(
                                origin,
                                "continuous",
                                None,
                                "持续代打当前植物大战僵尸游戏",
                            )
                        )
                    return
                if command == "play_once" and self.autoplay_run:
                    self.emit("remote.action.failed", reason="autoplay_active")
                    return
                if (
                    command == "play_once"
                    and self.action_job
                    and (not self.action_job.done())
                ):
                    self.emit("remote.action.failed", reason="one_action_inflight")
                    self.show_help(
                        failed_help(self.remote.snapshot, "one_action_inflight"),
                        self.remote.snapshot,
                    )
                    return
                prior = None
                if payload.get("advice_id"):
                    if (
                        not self.last_advice
                        or self.last_advice["advice_id"] != payload["advice_id"]
                    ):
                        self.emit(
                            "remote.action.failed", reason="referenced_advice_expired"
                        )
                        self.show_help(
                            failed_help(
                                self.remote.snapshot, "referenced_advice_expired"
                            ),
                            self.remote.snapshot,
                        )
                        return
                    prior = copy.deepcopy(self.last_advice)
                job = self.spawn(
                    self.user_action(
                        command, origin, self.game_speech_epoch, prior_advice=prior
                    )
                )
                if command == "play_once":
                    self.action_job = job
                return
            if command == "text" and self.config["game_expansion"].get(
                "text_development"
            ):
                text = str(payload.get("text", "")).strip()[:1200]
                if not text:
                    return
                uid = new_id()
                self.muted = False
                await self.handle(
                    "user.speech.started", {"utterance_id": uid, "audio_sample": 0}
                )
                await self.transcript(
                    TranscriptUpdate(
                        utterance_id=uid,
                        revision=1,
                        text=text,
                        is_final=True,
                        audio_end_sample=1,
                    )
                )
                return
            return
        if type == "remote.route.ready":
            await self.apply_game_route(**payload)
            return
        if type == "remote.help.ready":
            if (
                payload["speech_epoch"] != self.game_speech_epoch
                or payload["advice_epoch"] != self.advice_epoch
                or payload["binding"] != self.remote.binding
            ):
                return
            if payload.get("roster"):
                roster = payload["roster"]
                previous = self.seed_recommendation
                plants = list(
                    dict.fromkeys(
                        (
                            previous.get("plants", [])
                            if previous and roster.get("mode") == "add"
                            else []
                        )
                        + roster["plants"]
                    )
                )
                self.seed_recommendation = {
                    "plants": plants,
                    "text": payload["text"],
                    "binding": self.remote.binding,
                }
            if self.last_game_request:
                self.game_request_answered.add(
                    self.last_game_request["update"].utterance_id
                )
            self.show_help(
                payload["text"],
                payload["snapshot"],
                source=payload.get("source", "interface"),
                spoken=payload.get("spoken"),
            )
            return
        if type == "remote.choice.finished":
            if payload.get("generation") != getattr(self, "choice_generation", 0):
                return
            self.choice_job = None
            pending = self.choice_pending
            self.choice_pending = None
            if (
                pending
                and pending[2] == self.advice_epoch
                and (pending[3] == self.remote.binding)
            ):
                if pending[1]:
                    delay = max(
                        0,
                        self.config["game_expansion"].get("proactive_cooldown_s", 15)
                        - (time.monotonic() - self.last_proactive),
                    )
                    if delay:
                        self.choice_pending = pending
                        generation = getattr(self, "choice_generation", 0)

                        async def later():
                            await asyncio.sleep(delay)
                            self.post(
                                "remote.proactive.wake", {"generation": generation}
                            )

                        self.spawn(later())
                    else:
                        self.schedule_advice(
                            *pending[:2],
                            request=pending[4],
                            attempt=pending[5] if len(pending) > 5 else 0,
                        )
                else:
                    self.schedule_advice(
                        *pending[:2],
                        request=pending[4],
                        attempt=pending[5] if len(pending) > 5 else 0,
                    )
            return
        if type == "remote.proactive.wake":
            if (
                payload["generation"] != getattr(self, "choice_generation", 0)
                or self.choice_job is not None
            ):
                return
            pending = self.choice_pending
            self.choice_pending = None
            if (
                pending
                and pending[2] == self.advice_epoch
                and (pending[3] == self.remote.binding)
                and self.proactive_enabled
            ):
                self.schedule_advice(self.remote.snapshot, True, pending[4])
            return
        if type == "remote.advice.seen":
            if (
                self.offered_advice
                and self.offered_advice["advice_id"] == payload.get("advice_id")
                and self.valid(self.offered_advice["binding"])
            ):
                self.last_advice = copy.deepcopy(self.offered_advice)
                self.last_advice["binding"]["scope"] = "history"
                self.emit("remote.advice.presented", advice_id=payload["advice_id"])
            return
        if type == "remote.advice.ready":
            action, snapshot, provider = (
                payload["action"],
                payload["snapshot"],
                payload["provider"],
            )
            binding = payload["binding"]
            if (
                self.valid(binding)
                and payload["speech_epoch"] == self.game_speech_epoch
                and (payload["advice_epoch"] == self.advice_epoch)
            ):
                if (
                    not payload.get("proactive")
                    and self.response_id
                    and self.presentations[self.response_id].get("remote_binding")
                ):
                    self.clear_playback("explicit_current_advice")
                if payload.get("target"):
                    self.tactical_target = payload["target"]
                text = coach.wording(action, snapshot)
                target = payload.get("target")
                if target and action.kind == "WAIT":
                    text = f"先留阳光，准备在第{target['row']}路种{target['name']}，需要{target['cost']}点。"
                if payload.get("attempt", 0):
                    text = "局面变了，" + text
                advice_id = new_id()
                self.pending_advice = (action, text, binding)
                self.pending_advice_request = (
                    None
                    if payload.get("proactive")
                    else {
                        "request": payload.get("request", ""),
                        "attempt": payload.get("attempt", 0),
                        "epoch": self.advice_epoch,
                        "binding": self.remote.binding,
                    }
                )
                self.offered_advice = {
                    "advice_id": advice_id,
                    "action": action.model_dump(),
                    "text": text,
                    "snapshot": coach.summary(snapshot),
                    "binding": binding,
                    "scope": "history",
                }
                self.emit(
                    "remote.advice",
                    advice_id=advice_id,
                    action=action.model_dump(),
                    text=text,
                    binding=binding,
                    provider=provider,
                )
                await self.flush_advice()
            elif (
                not payload.get("proactive")
                and payload["speech_epoch"] == self.game_speech_epoch
                and (payload["advice_epoch"] == self.advice_epoch)
            ):
                self.refresh_explicit_advice(
                    {
                        "request": payload.get("request", ""),
                        "attempt": payload.get("attempt", 0),
                        "epoch": self.advice_epoch,
                        "binding": self.remote.binding,
                    }
                )
            return
        await super().handle(type, payload)
        if type == "playback.ack" and payload.get("played_samples", 0) > 0:
            chunk = self.chunks.get(payload.get("chunk_id"), {})
            rid = chunk.get("response_id")
            if rid and self.presentations.get(rid, {}).get("question_id"):
                self.game_request_answered.add(self.presentations[rid]["question_id"])
        if type in {
            "playback.ack",
            "speech.checked",
            "user.speech.ended",
            "delivery.wake",
        }:
            await self.flush_advice()

    async def suppress_context_echo(self, update):
        from jarvis.games.asr_context import glossary_echo

        if not glossary_echo(
            update.text, self.config["game_expansion"].get("asr_context", "")
        ):
            return False
        previous = self.transcripts.get(update.utterance_id)
        if previous and (previous.is_final or previous.revision >= update.revision):
            return True
        if update.audio_end_sample <= self.history_audio_floor:
            return True
        self.transcripts[update.utterance_id] = update
        self.routed.add(update.utterance_id)
        self.route_origins.pop(update.utterance_id, None)
        self.emit("asr.final", **update.model_dump())
        self.emit(
            "asr.context_echo_suppressed",
            utterance_id=update.utterance_id,
            reason="long_verbatim_glossary_sequence",
        )
        if self.current_utterance == update.utterance_id:
            self.user_speaking = False
            self.route_pending = False
            self.resume_playback()
            pending = self.last_game_request
            if (
                True
                and pending
                and (pending["binding"] == self.remote.binding)
                and (time.monotonic() - pending["at"] < 18)
                and (pending["update"].utterance_id not in self.game_request_answered)
            ):
                old = pending["update"]
                self.current_utterance = old.utterance_id
                self.routed.discard(old.utterance_id)
                if self.game_route_job and (not self.game_route_job.done()):
                    self.game_route_job.cancel()
                pending["read_only"] = True
                self.route_pending = True
                self.start_route(old, recovered=True)
                self.emit("remote.question.restored", utterance_id=old.utterance_id)
            await self.try_deliver()
        return True

    def start_route(self, update, *, recovered=False):
        if (
            self.story
            and self.mode == "pvz_web"
            and update.is_final
            and self.remote.snapshot
            and (self.remote.snapshot.view.ui_stage in {"playing", "ended"})
        ):
            return self.story.start(update)
        if self.mode == "voxel":
            return self.voxel.start_route(update)
        if not update.is_final or not self.remote.game_id:
            return super().start_route(update)
        if update.utterance_id in self.routed:
            return None
        self.routed.add(update.utterance_id)
        if self.game_route_job and (not self.game_route_job.done()):
            self.game_route_job.cancel()
        if not recovered:
            self.last_game_request = {
                "update": update,
                "binding": self.remote.binding,
                "at": time.monotonic(),
                "read_only": False,
            }
        self.advice_epoch += 1
        self.pending_advice = None
        self.pending_help = None
        origin = self.route_origins.pop(update.utterance_id, None)
        speech_epoch = self.game_speech_epoch
        analysis_epoch = self.analysis_epoch
        source_binding = self.remote.binding
        last_advice = copy.deepcopy(self.last_advice)
        history = list(self.history)
        self.control_generation = getattr(self, "control_generation", 0) + 1
        words = re.sub("[\\s，。！？,.!?]", "", update.text).lower()
        local = (
            "RECLAIM"
            if self.remote.game_id != "pvz_web"
            and words
            in {
                "我来",
                "让我来",
                "这一手我来",
                "我来你先别操作",
                "我来你别操作",
                "先别操作",
                "收回控制",
                "停止代打",
                "letmeplay",
                "illtakeover",
                "stopplaying",
            }
            else "CANCEL_ANALYSIS"
            if words in {"取消分析", "取消这次分析", "cancelanalysis"}
            else None
        )
        if local:
            self.post(
                "remote.route.ready",
                {
                    "update": update,
                    "result": {
                        "route": local,
                        "fact": "NONE",
                        "confidence": 1,
                        "provider": "local_control",
                    },
                    "snapshot": self.remote.snapshot,
                    "origin": origin,
                    "speech_epoch": speech_epoch,
                    "source_binding": source_binding,
                    "last_advice": last_advice,
                },
            )
            return None

        async def resolve():
            try:
                snapshot = self.remote.snapshot
                if snapshot and (not self.remote.fresh()):
                    snapshot = await self.remote.observe()
                async with self.route_lock:
                    if update.utterance_id != self.current_utterance:
                        return
                    pending = self.control_confirmation
                    if pending and (
                        pending["binding"] != self.remote.binding
                        or time.monotonic() > pending["expires"]
                    ):
                        pending = None
                    result = await coach.route(
                        self.controller,
                        update.text,
                        snapshot,
                        history,
                        last_advice,
                        pending={
                            k: v
                            for k, v in pending.items()
                            if k not in {"binding", "expires"}
                        }
                        if pending
                        else None,
                        autoplay=self.autoplay_context(),
                        reviewer=self.worker,
                    )
                if recovered and result["route"] not in {
                    "ADVICE",
                    "FACT",
                    "GAME_CHAT",
                    "EXPLAIN",
                    "EXPLAIN_HISTORY",
                }:
                    result = {
                        "route": "IGNORE",
                        "fact": "NONE",
                        "confidence": 0,
                        "provider": "noise_recovery_read_only",
                    }
                if (
                    result["route"]
                    not in {"RECLAIM", "STOP", "CLARIFY_CONTROL", "ASSISTANT", "IGNORE"}
                    and source_binding == self.remote.binding
                    and (not self.remote.fresh())
                ):
                    snapshot = await self.remote.observe()
            except Exception as error:
                self.emit("remote.route.failed", error=type(error).__name__)
                result = {
                    "route": "UNAVAILABLE"
                    if self.remote.game_id == "pvz_web"
                    else "ASSISTANT",
                    "fact": "NONE",
                    "confidence": 0,
                }
                snapshot = self.remote.snapshot
            self.post(
                "remote.route.ready",
                {
                    "update": update,
                    "result": result,
                    "snapshot": snapshot,
                    "origin": origin,
                    "speech_epoch": speech_epoch,
                    "source_binding": source_binding,
                    "last_advice": last_advice,
                    "analysis_epoch": analysis_epoch,
                },
            )

        self.game_route_job = self.spawn(resolve())
        return self.game_route_job

    async def transcript(self, update):
        if self.mode == "voxel":
            if update.is_final:
                self.voxel.capture(update)
            await SessionRuntime.transcript(self, update)
            return
        if update.is_final and update.utterance_id not in self.routed:
            self.route_origins[update.utterance_id] = self.remote.origin(
                update.utterance_id
            )
        await super().transcript(update)

    async def apply_game_route(
        self,
        update,
        result,
        snapshot,
        origin,
        speech_epoch,
        source_binding=None,
        last_advice=None,
        analysis_epoch=None,
    ):
        if update.utterance_id != self.current_utterance:
            return
        self.route_pending = False
        route = result["route"]
        self.emit("remote.route.applied", utterance_id=update.utterance_id, **result)
        if route == "ASSISTANT":
            self.route_pending = True
            super().start_route(update)
            return
        if route == "IGNORE":
            self.resume_playback()
            return
        if self.paused_response:
            self.clear_playback("confirmed_user_request")
        if route == "STOP":
            await self.handle("stop_speaking", {})
            return
        if route == "RECLAIM":
            self.advice_epoch += 1
            self.choice_pending = None
            self.stop_autoplay("user_reclaimed", revoke=False)
            self.remote.fence_control()
            self.spawn(self.reclaim())
            if result.get("also_advice") and self.remote.snapshot:
                self.schedule_advice(self.remote.snapshot, request=update.text)
            return
        if route == "CANCEL_ANALYSIS":
            self.cancel_analysis()
            self.speak("已取消这次分析。", "confirmation")
            return
        if source_binding != self.remote.binding:
            self.emit("remote.route.discarded", reason="game_binding_changed")
            return
        if route == "UNAVAILABLE":
            self.show_help(
                "刚才没读到完整游戏状态，等连接恢复后再问我。"
                if not self.remote.connected
                else "这次没能及时判断你的问题，可以再问一次。",
                self.remote.snapshot,
                source="unavailable",
            )
            return
        if route == "CLARIFY_CONTROL":
            scope = result.get("scope", "NONE")
            duration = result.get("duration_ms")
            question = {
                "CONTINUOUS": "是要我持续代打，直到你说自己来吗？",
                "LEVEL": "是要我帮你打完当前这一关吗？",
                "ONCE": "是要我只帮你操作这一步吗？",
            }.get(scope)
            if scope == "TIMED" and duration:
                duration_text = (
                    f"{duration / 60000:g}分钟"
                    if duration % 60000 == 0
                    else f"{duration / 1000:g}秒"
                )
                question = f"是要我现在代打{duration_text}，到时交还给你吗？"
            if not question:
                scope = "NONE"
                question = (
                    "你希望我代打多久，还是只打完这一关？"
                    if result.get("scope") == "TIMED"
                    else "你是要我现在接手代打，还是只给建议？"
                )
            self.control_confirmation = {
                "request": update.text,
                "proposal": question,
                "scope": scope,
                "duration_ms": duration,
                "binding": self.remote.binding,
                "expires": time.monotonic() + 30,
            }
            self.show_help(question, self.remote.snapshot, source="clarification")
            return
        if not snapshot or not self.remote.fresh():
            self.speak("游戏状态暂时不可用，请检查游戏连接。", "clarify")
            return
        self.control_confirmation = None
        if route == "GAME_CHAT":
            if snapshot.view.game == "pvz_web":
                cue = "<|im_start|>user\nAnswer the current question briefly in the game context. You can recommend seeds and moves, "
                cue += "play one step, continuously until reclaim, for a stated time, or for this level. Never claim execution without confirmation.\n"
                cue += (
                    json.dumps(
                        {
                            "question": update.text,
                            "game": coach.summary(snapshot),
                            "autoplay": self.autoplay_context(),
                        },
                        ensure_ascii=False,
                    )
                    + "<|im_end|>"
                )
                self.speak_game(
                    None,
                    "game_help",
                    self.binding("game", snapshot=snapshot),
                    source_utterance=update.utterance_id,
                    user_text=cue,
                )
            else:
                self.show_help(interface_help(snapshot), snapshot)
            return
        if route == "PLAY":
            if snapshot.view.game == "pvz_web":
                self.command_pending = True
                self.autoplay_start_job = self.spawn(
                    self.start_pvz_request(
                        origin, snapshot, result, update.text, prior_advice=last_advice
                    )
                )
                return
            scope = result.get("scope", "ONCE")
            if snapshot.view.game == "pvz_web" and scope in {
                "CONTINUOUS",
                "TIMED",
                "LEVEL",
            }:
                if not self.remote.current(origin, require_window=True):
                    self.show_help("请等草坪准备好、松开鼠标后再交给我代打。", snapshot)
                    return
                self.autoplay_start_job = self.spawn(
                    self.begin_autoplay(
                        origin,
                        scope.lower(),
                        result.get("duration_ms") if scope == "TIMED" else None,
                        update.text,
                    )
                )
                return
            help_text = interface_help(snapshot, execution=True)
            if help_text:
                self.show_help(help_text, snapshot)
                return
            if not self.remote.current(origin, require_window=True):
                self.speak("局面已经变化，请重新授权当前这一步。", "clarify")
                return
            if self.action_job and (not self.action_job.done()):
                return
            self.action_job = self.spawn(
                self.execute_once(
                    origin,
                    snapshot,
                    speech_epoch,
                    request=update.text,
                    end_turn=result.get("end_turn", False),
                    prior_advice=last_advice if result.get("use_advice") else None,
                    require_advice=result.get("use_advice", False),
                )
            )
            return
        if route == "ADVICE":
            self.schedule_advice(snapshot, request=update.text)
            return
        if route in {"EXPLAIN", "EXPLAIN_HISTORY"}:
            if analysis_epoch is not None and analysis_epoch != self.analysis_epoch:
                return
            self.cancel_analysis()
            historical = (
                route == "EXPLAIN_HISTORY"
                and last_advice
                and self.valid(last_advice["binding"])
            )
            binding = self.binding(
                "history" if historical else "analysis", snapshot=snapshot
            )
            observation = (
                last_advice
                if historical
                else {"snapshot": coach.summary(snapshot), "scope": "position"}
            )
            task = Task(
                logical_request_id=update.utterance_id,
                source_utterance_id=update.utterance_id,
                objective=update.text,
                origin_session_id=self.session_id,
                skill="remote_game_analysis",
                expected_output="answer",
                allowed_tool_scope=["get_game_snapshot", "lookup_game_rule"],
                history=self.history[-4:],
                observations=[observation],
                response_language=self.response_language,
            )
            self.task_bindings[task.task_id] = binding
            self.current_analysis = task.task_id
            self.start_worker(update, replacement=task)
            self.speak("我分析一下，你可以继续操作。", "ack")
            return
        fact = result.get("fact")
        v = snapshot.view
        value = {
            "SUN": getattr(v, "sun", None),
            "HP": getattr(v, "hp", None),
            "ENERGY": getattr(v, "energy", None),
            "BLOCK": getattr(v, "block", None),
        }.get(fact)
        if value is not None:
            label = {"SUN": "阳光", "HP": "体力", "ENERGY": "能量", "BLOCK": "格挡"}[
                fact
            ]
            self.speak_game(
                f"刚才读取到的{label}是{value}。",
                "fast",
                self.binding(
                    "position",
                    snapshot=snapshot,
                    field={
                        "SUN": "sun",
                        "HP": "hp",
                        "ENERGY": "energy",
                        "BLOCK": "block",
                    }[fact],
                ),
                source_utterance=update.utterance_id,
            )
        else:
            cue = (
                "<|im_start|>user\nUse only these current public game facts to answer briefly.\n"
                + json.dumps(
                    {"question": update.text, "game": coach.summary(snapshot)},
                    ensure_ascii=False,
                )
                + "<|im_end|>"
            )
            self.speak_game(
                None,
                "fast",
                self.binding("position", snapshot=snapshot),
                source_utterance=update.utterance_id,
                user_text=cue,
            )

    def schedule_advice(self, snapshot, proactive=False, request="", attempt=0):
        if snapshot is None:
            return
        if proactive and self.autoplay_run:
            return
        if self.choice_job is not None:
            if not proactive or self.choice_pending is None or self.choice_pending[1]:
                self.choice_pending = (
                    snapshot,
                    proactive,
                    self.advice_epoch,
                    self.remote.binding,
                    request,
                    attempt,
                )
            return
        if proactive:
            if time.monotonic() - self.last_proactive < self.config[
                "game_expansion"
            ].get("proactive_cooldown_s", 15):
                return
            self.last_proactive = time.monotonic()
        self.choice_pending = None
        self.choice_job = self.spawn(self.advice(snapshot, proactive, request, attempt))

    async def advice(self, snapshot, proactive=False, request="", attempt=0):
        source_binding = self.remote.binding
        epoch = self.game_speech_epoch
        advice_epoch = self.advice_epoch
        generation = getattr(self, "choice_generation", 0)
        try:
            async with self.choice_lock:
                if (
                    source_binding != self.remote.binding
                    or advice_epoch != self.advice_epoch
                ):
                    return
                snapshot = self.remote.snapshot or snapshot
                if not self.remote.fresh():
                    snapshot = await self.remote.observe()
                if (
                    snapshot.view.game == "pvz_web"
                    and snapshot.view.ui_stage == "select_cards"
                    and (not proactive)
                ):
                    previous = self.seed_recommendation
                    roster = await coach.recommend_seeds(
                        self.controller,
                        snapshot,
                        request,
                        previous={k: v for k, v in previous.items() if k != "binding"}
                        if previous and previous["binding"] == source_binding
                        else None,
                        history=self.history,
                    )
                    if roster:
                        self.post(
                            "remote.help.ready",
                            {
                                "text": roster["text"]
                                if isinstance(roster, dict)
                                else roster,
                                "spoken": roster.get("spoken")
                                if isinstance(roster, dict)
                                else None,
                                "snapshot": snapshot,
                                "binding": source_binding,
                                "speech_epoch": epoch,
                                "advice_epoch": advice_epoch,
                                "source": "jev_seed_roster",
                                "roster": roster if isinstance(roster, dict) else None,
                            },
                        )
                        return
                help_text = interface_help(snapshot)
                if help_text and (
                    not (
                        snapshot.view.game == "pvz_web"
                        and snapshot.awaiting_action
                        and snapshot.legal_actions
                    )
                ):
                    if not proactive:
                        self.post(
                            "remote.help.ready",
                            {
                                "text": help_text,
                                "snapshot": snapshot,
                                "binding": source_binding,
                                "speech_epoch": epoch,
                                "advice_epoch": advice_epoch,
                            },
                        )
                    return
                decision = {}
                action, provider = await coach.choose(
                    self.controller,
                    snapshot,
                    proactive=proactive,
                    limit=self.config["game_expansion"]["candidate_limit"],
                    request=request,
                    prior=self.tactical_target,
                    decision=decision,
                )
            if action and source_binding == self.remote.binding:
                binding = self.binding("position", snapshot=snapshot, action=action)
                self.post(
                    "remote.advice.ready",
                    {
                        "action": action,
                        "provider": provider,
                        "snapshot": snapshot,
                        "binding": binding,
                        "speech_epoch": epoch,
                        "advice_epoch": advice_epoch,
                        "proactive": proactive,
                        "target": decision.get("target"),
                        "request": request,
                        "attempt": attempt,
                    },
                )
            elif not proactive:
                self.emit("remote.advice.failed", reason="abstain")
                self.post(
                    "remote.help.ready",
                    {
                        "text": failed_help(snapshot, "abstain"),
                        "snapshot": snapshot,
                        "binding": source_binding,
                        "speech_epoch": epoch,
                        "advice_epoch": advice_epoch,
                        "source": "abstain",
                    },
                )
        except Exception as error:
            if not proactive:
                self.emit(
                    "remote.advice.failed",
                    reason="decision_unavailable",
                    error=type(error).__name__,
                )
                text = (
                    "刚才没算出补选建议，可以再问我剩下哪些卡值得带。"
                    if snapshot.view.game == "pvz_web"
                    and snapshot.view.ui_stage == "select_cards"
                    else failed_help(snapshot, "decision_unavailable")
                )
                self.post(
                    "remote.help.ready",
                    {
                        "text": text,
                        "snapshot": snapshot,
                        "binding": source_binding,
                        "speech_epoch": epoch,
                        "advice_epoch": advice_epoch,
                        "source": "unavailable",
                    },
                )
        finally:
            self.post("remote.choice.finished", {"generation": generation})

    async def user_action(self, action, origin, speech_epoch, prior_advice=None):
        try:
            snapshot = await self.remote.observe()
            if action == "play_once":
                await self.execute_once(
                    origin,
                    snapshot,
                    speech_epoch,
                    prior_advice=prior_advice,
                    require_advice=prior_advice is not None,
                )
            elif origin.binding == self.remote.binding:
                self.schedule_advice(snapshot)
        except Exception:
            self.emit("remote.action.failed", reason="fresh_state_unavailable")
            if speech_epoch == self.game_speech_epoch:
                self.show_help(
                    failed_help(self.remote.snapshot, "fresh_state_unavailable"),
                    self.remote.snapshot,
                )

    async def execute_once(
        self,
        origin,
        snapshot,
        speech_epoch,
        request="",
        end_turn=False,
        prior_advice=None,
        require_advice=False,
    ):
        source_binding = self.remote.binding
        try:
            help_text = interface_help(self.remote.snapshot, execution=True)
            if help_text:
                self.show_help(help_text, self.remote.snapshot)
                return
            if self.choice_lock.locked():
                self.emit("remote.action.failed", reason="decision_busy")
                self.show_help(
                    failed_help(self.remote.snapshot, "decision_busy"),
                    self.remote.snapshot,
                )
                return
            snapshot = self.remote.contexts.get(
                origin.anchor.snapshot_seq, (None, None)
            )[0]
            if snapshot is None:
                raise BridgeError("Source snapshot expired")
            kinds = [
                k
                for k in self.remote.capabilities.actions
                if k != "WAIT" and (k != "END_TURN" or end_turn)
            ]
            if end_turn:
                kinds = ["END_TURN"]
            snapshot = snapshot.model_copy(
                update={
                    "legal_actions": [
                        a
                        for a in snapshot.legal_actions
                        if a.kind in kinds or a.kind == "WAIT"
                    ]
                }
            )
            action = None
            if prior_advice and self.valid(
                {**prior_advice["binding"], "scope": "position"}, referenced_action=True
            ):
                previous = prior_advice["action"]
                action = next(
                    (
                        a
                        for a in snapshot.legal_actions
                        if a.kind == previous["kind"]
                        and a.facts.model_dump(mode="json") == previous["facts"]
                    ),
                    None,
                )
                if action:
                    self.emit(
                        "remote.advice.reused",
                        advice_id=prior_advice["advice_id"],
                        action_id=action.action_id,
                    )
            if require_advice and action is None:
                self.emit("remote.action.failed", reason="referenced_advice_expired")
                self.show_help(
                    failed_help(self.remote.snapshot, "referenced_advice_expired"),
                    self.remote.snapshot,
                )
                return
            grant = await self.remote.arm_once(
                origin, kinds, 3000 if self.remote.game_id == "pvz_web" else 10000
            )
            if action is None:
                async with self.choice_lock:
                    action, provider = await coach.choose(
                        self.controller,
                        snapshot,
                        limit=self.config["game_expansion"]["candidate_limit"],
                        request=request,
                        execution=True,
                        reviewer=self.worker,
                    )
            if action is None or action.kind == "WAIT":
                await self.remote.revoke("abstain_or_wait", wait=False)
                self.emit(
                    "remote.action.failed", reason="wait" if action else "abstain"
                )
                if (
                    speech_epoch == self.game_speech_epoch
                    and source_binding == self.remote.binding
                ):
                    self.show_help(
                        failed_help(snapshot, "wait" if action else "abstain"), snapshot
                    )
                return
            result = await self.remote.commit_action(
                origin, grant.grant_id, action.action_id
            )
            if (
                result.status.startswith("CONFIRMED")
                and speech_epoch == self.game_speech_epoch
                and (source_binding == self.remote.binding)
            ):
                if snapshot.view.game != "pvz_web":
                    self.receipts.append(
                        (
                            "已执行" + action.label + "，接下来由你操作。",
                            self.binding("receipt", snapshot=snapshot),
                        )
                    )
                    await self.flush_advice()
                else:
                    self.emit(
                        "remote.action.completed",
                        text=action.label,
                        command_id=result.command_id,
                    )
            elif result.status == "UNKNOWN":
                self.speak("这次执行结果尚不确定，已停止自动操作。", "confirmation")
            elif (
                not result.status.startswith("CONFIRMED")
                and speech_epoch == self.game_speech_epoch
                and (source_binding == self.remote.binding)
            ):
                self.show_help(
                    failed_help(self.remote.snapshot, "rejected_or_unavailable"),
                    self.remote.snapshot,
                )
        except Exception:
            if source_binding == self.remote.binding:
                try:
                    await self.remote.revoke("decision_failed", wait=False)
                except BridgeError:
                    pass
            self.emit("remote.action.failed", reason="rejected_or_unavailable")
            if (
                speech_epoch == self.game_speech_epoch
                and source_binding == self.remote.binding
            ):
                self.show_help(
                    failed_help(self.remote.snapshot, "rejected_or_unavailable"),
                    self.remote.snapshot,
                )

    async def reclaim(self):
        try:
            await self.remote.revoke()
        except (BridgeError, TimeoutError):
            self.emit("remote.reclaim.pending", reason="waiting_for_game")

    async def start_pvz_request(
        self, origin, snapshot, result, request, prior_advice=None
    ):
        from jarvis.games.commands import understand

        source = self.remote.binding
        generation = getattr(self, "control_generation", 0)
        try:
            if result.get("use_advice") and prior_advice:
                await self.execute_once(
                    origin,
                    snapshot,
                    self.game_speech_epoch,
                    request=request,
                    prior_advice=prior_advice,
                    require_advice=True,
                )
                return
            command = await understand(
                self.controller,
                snapshot,
                request,
                history=self.history,
                active=self.autoplay_context(),
                reviewer=self.worker,
            )
            if source != self.remote.binding or generation != getattr(
                self, "control_generation", 0
            ):
                return
            if command:
                previous = self.autoplay_run
                resume = bool(previous and command.after == "CONTINUE")
                if command.operation == "COLLECT_SUN" and (
                    result.get("scope") in {"CONTINUOUS", "TIMED", "LEVEL"}
                    or result.get("duration_ms")
                ):
                    command.quantity = "ONGOING"
                if command.operation == "SHOVEL" and (not command.original_targets):
                    self.show_help(
                        "当前没有找到你指定的植物，没有进行铲除。",
                        snapshot,
                        source="command_status",
                    )
                    return
                if command.progress(snapshot)[2] == "complete":
                    self.show_help(
                        "你指定的目标已经满足，不需要重复操作。",
                        snapshot,
                        source="command_status",
                    )
                    return
                duration = result.get("duration_ms")
                await self.begin_autoplay(
                    origin,
                    "timed" if duration else "task",
                    duration,
                    request,
                    command=command,
                    resume_run=previous if resume else None,
                )
                return
            scope = result.get("scope", "CONTINUOUS")
            if scope == "TASK" and result.get("duration_ms"):
                scope = "TIMED"
            elif scope == "TASK":
                scope = "CONTINUOUS"
            if scope == "ONCE":
                await self.execute_once(
                    origin, snapshot, self.game_speech_epoch, request=request
                )
            elif scope in {"CONTINUOUS", "TIMED", "LEVEL"}:
                await self.begin_autoplay(
                    origin,
                    scope.lower(),
                    result.get("duration_ms") if scope == "TIMED" else None,
                    request,
                )
            else:
                self.show_help(
                    "你希望我具体完成什么操作？可以说明植物和位置。",
                    snapshot,
                    source="command_clarification",
                )
        except Exception as error:
            self.emit("remote.command.failed", reason=type(error).__name__)
            if source == self.remote.binding:
                self.show_help(
                    "这次操作的目标还没确认清楚，请说一下要操作哪种植物、哪一行或哪一格。",
                    self.remote.snapshot,
                    source="command_clarification",
                )
        finally:
            self.command_pending = False

    def cancel_analysis(self):
        self.analysis_epoch += 1
        task = self.tasks.get(self.current_analysis)
        if task:
            self.invalidate(task, TaskStatus.CANCELLED)
        self.current_analysis = None

    async def flush_advice(self):
        if self.user_speaking or self.route_pending or self.response_id or self.muted:
            return
        while self.receipts:
            text, binding = self.receipts.pop(0)
            if self.valid(binding):
                self.speak_game(text, "game_receipt", binding)
                return
        if self.pending_help:
            text, binding = self.pending_help
            self.pending_help = None
            if self.valid(binding):
                self.speak_game(text, "game_help", binding)
                return
        if not self.pending_advice:
            return
        action, text, binding = self.pending_advice
        self.pending_advice = None
        if self.valid(binding):
            rid = self.speak_game(text, "game_advice", binding)
            if rid and self.pending_advice_request:
                self.presentations[rid]["advice_request"] = self.pending_advice_request
                if self.last_game_request:
                    self.presentations[rid]["question_id"] = self.last_game_request[
                        "update"
                    ].utterance_id
            elif (
                self.text_mode
                and self.pending_advice_request
                and self.last_game_request
            ):
                self.game_request_answered.add(
                    self.last_game_request["update"].utterance_id
                )
        elif self.pending_advice_request:
            self.refresh_explicit_advice(self.pending_advice_request)

    def refresh_explicit_advice(self, request):
        self.pending_advice_request = None
        if (
            request["epoch"] != self.advice_epoch
            or request["binding"] != self.remote.binding
        ):
            return
        if request["attempt"] < 1:
            self.schedule_advice(
                self.remote.snapshot,
                request=request["request"],
                attempt=request["attempt"] + 1,
            )
            self.emit(
                "remote.advice.refreshed", reason="position_changed_before_delivery"
            )
        else:
            self.show_help(
                "局面变化很快，刚才的位置已错过，我先不报那个旧位置。",
                self.remote.snapshot,
                source="expired",
            )

    def show_help(
        self, text, snapshot, *, source="interface", proactive=False, spoken=None
    ):
        if not text:
            return
        binding = self.binding("interface", snapshot=snapshot) if snapshot else None
        if not self.valid(binding):
            return
        self.emit(
            "remote.help",
            text=text,
            binding=binding,
            source=source,
            proactive=proactive,
        )
        if self.user_speaking or self.route_pending or self.response_id:
            self.pending_help = (spoken or text, binding)
        else:
            self.speak_game(spoken or text, "game_help", binding)

    async def try_deliver(self):
        await super().try_deliver()
        await self.flush_advice()

    def venus_step(self, message):
        self.expire_game_speech("remote_context_changed")
        super().venus_step(message)

    def finish_presentation(self, response_id):
        presentation = self.presentations.get(response_id, {})
        previously = bool(presentation.get("history_recorded"))
        super().finish_presentation(response_id)
        if (
            self.mode == "voxel"
            and self.voxel
            and (not previously)
            and presentation.get("history_recorded")
        ):
            self.post("voxel.speech.flush", {})
        if (
            self.mode == "voxel"
            and self.voxel
            and (not previously)
            and presentation.get("history_recorded")
            and (presentation.get("remote_binding") or {}).get("voxel")
            and (not presentation.get("interrupted"))
            and presentation.get("audio_complete", True)
            and any((self.chunks[k]["played"] for k in presentation.get("chunks", [])))
        ):
            text = "".join(presentation.get("text", []))
            if text:
                self.emit(
                    "voxel.spoken",
                    text=text,
                    response_id=response_id,
                    purpose=presentation["purpose"],
                )
                if (
                    self.voxel.last_advice
                    and presentation.get("voxel_advice_id")
                    == self.voxel.last_advice["id"]
                ):
                    self.voxel.last_advice["text"] = text

    async def close(self):
        if self.voxel and self.mode == "voxel":
            await self.voxel.deactivate()
        self.stop_autoplay("session_closed", revoke=False)
        if self.registry:
            self.registry.remove(self.session_id)
        try:
            await self.remote.revoke("session_closed", wait=False)
        except Exception:
            pass
        self.remote.disconnect("session_closed")
        await super().close()
