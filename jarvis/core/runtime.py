from __future__ import annotations
import asyncio
import base64
import json
import re
import time
from pathlib import Path
from jarvis.core.jev import JevController
from jarvis.core.memory import MemoryStore
from jarvis.core.schemas import (
    Decision,
    Task,
    TaskResult,
    TaskStatus,
    TranscriptUpdate,
    new_id,
)
from jarvis.core.skills import SkillStore
from jarvis.core.tools import ToolRegistry
from jarvis.core.worker import DeepSeekWorker

TERMINAL = {TaskStatus.CANCELLED, TaskStatus.SUPERSEDED, TaskStatus.FAILED}


class SessionRuntime:
    def __init__(
        self,
        config,
        session_id,
        user_id,
        trace,
        send_client,
        send_venus,
        memory: MemoryStore,
        worker=None,
        controller=None,
    ):
        self.config, self.session_id, self.user_id = (config, session_id, user_id)
        self.trace, self.send_client, self.send_venus = (trace, send_client, send_venus)
        self.memory = memory
        self.profile = []
        self.task_catalog = []
        self.task_focus_id = None
        self.dialogue_focus = {}
        self.skills = SkillStore(Path(config["assets"]))
        self.examples = []
        self.tools = ToolRegistry(config["tools"], self.memory_search)
        self.worker = worker or DeepSeekWorker(config, self.tools, self.emit)
        self.controller = controller or JevController(config, self.emit, self.worker)
        self.speech_checker = None
        self.task_store = None
        if config["memory"].get("task_journal"):
            from jarvis.core.task_store import TaskStore

            self.task_store = TaskStore(memory.path)
        if config.get("speech_check", {}).get("enabled"):
            from jarvis.core.speech_check import SpeechChecker

            self.speech_checker = SpeechChecker(config["speech_check"])
        self.queue = asyncio.Queue()
        self.tasks: dict[str, Task] = {}
        self.results: dict[tuple[str, int], TaskResult] = {}
        self.logical_requests = {}
        self.submitted = set()
        self.committed = set()
        self.jobs = {}
        self.background = set()
        self.worker_slots = asyncio.Semaphore(config["runtime"]["max_active_workers"])
        self.transcripts = {}
        self.decision_epochs = {}
        self.history = []
        self.played = []
        self.current_utterance = None
        self.user_speaking = False
        self.route_pending = False
        self.muted = False
        self.generation_epoch = 0
        self.response_id = None
        self.presentations = {}
        self.chunks = {}
        self.last_progress = 0
        self.prefix_job = None
        self.prefix_timer = None
        self.prefix_pending = None
        self.last_prefix_time = 0
        self.delivery_inflight = set()
        self.delivery_attempts = {}
        self.delivery_retry_scheduled = set()
        self.ready_hints = set()
        self.ready_hints_pending = set()
        self.memory_jobs = set()
        self.memory_prefetch = {}
        self.closed = False
        self.focus_epoch = 0
        self.quiet_since = time.monotonic()
        self.last_spoken_purpose = None
        self.delivery_timer = None
        self.processed_batches = set()
        self.paused_response = None
        self.interrupted_deliveries = set()
        self.requested_results = []
        self.result_speech_attempts = {}
        self.playback_control_epoch = 0
        self.final_routes = set()
        self.applied_final_decisions = set()
        self.suppressed_announcement_sources = {}
        self.history_reset_pending = None
        self.history_audio_floor = -1
        self.response_language = config.get("speech_language", "auto")
        from jarvis.core.worker_dialogue import snapshot

        self.worker.dialogue_snapshot = lambda task: snapshot(self, task)

    def emit(self, type, **payload):
        if self.config["runtime"].get("task_dialogue") and type in {
            "worker.thinking",
            "tool.started",
            "tool.finished",
        }:
            task = self.tasks.get(payload.get("task_id"))
            if (
                task
                and task.version == payload.get("version")
                and (task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING})
            ):
                progress = dict(task.progress)
                progress["observed_at"] = time.time()
                if type == "worker.thinking":
                    progress["phase"] = "generating"
                else:
                    progress["tool"] = payload.get("name")
                    ok = payload.get("result", {}).get("ok", False)
                    progress["phase"] = (
                        "tool_running"
                        if type == "tool.started"
                        else "tool_complete"
                        if ok
                        else "tool_failed"
                    )
                    if type == "tool.finished" and ok:
                        progress["completed_tools"] = (
                            progress.get("completed_tools", 0) + 1
                        )
                task.progress = progress
                self.persist_task(task)
        event = self.trace.emit(type, **payload)
        self.send_client({"type": "event", "event": event.model_dump(mode="json")})
        return event

    def spawn(self, coroutine, memory=False):
        task = asyncio.create_task(coroutine)
        collection = self.memory_jobs if memory else self.background
        collection.add(task)

        def finished(job):
            collection.discard(job)
            if not job.cancelled() and job.exception():
                self.queue.put_nowait(
                    ("runtime.error", {"error": type(job.exception()).__name__})
                )

        task.add_done_callback(finished)
        return task

    def post(self, type, payload=None):
        self.queue.put_nowait((type, payload or {}))

    async def memory_search(self, query, limit=12):
        return await self.memory.search(self.user_id, query, limit)

    def context(self):
        cards = []
        for t in self.tasks.values():
            if t.status in {TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}:
                continue
            result = self.results.get((t.task_id, t.version))
            cards.append(
                {
                    "task_id": t.task_id,
                    "version": t.version,
                    "objective": t.objective[:140],
                    "status": t.status.value,
                    "provisional": t.provisional,
                    "created_at": t.created_at,
                    "files": [a["filename"] for a in result.artifacts]
                    if result
                    else [],
                }
            )
        if self.task_store:
            active = [
                c for c in cards if c["status"] in {"QUEUED", "RUNNING", "NEEDS_INPUT"}
            ]
            settled = sorted(
                (c for c in cards if c not in active), key=lambda c: c["created_at"]
            )
            relevant = set(getattr(self, "matched_task_ids", []))
            relevant.update((c["task_id"] for c in settled[-2:]))
            relevant.update(
                (c["task_id"] for c in [c for c in settled if c["files"]][-2:])
            )
            budget = max(
                0, self.config["runtime"].get("max_context_tasks", 12) - len(active)
            )
            selected = [c for c in settled if c["task_id"] in relevant]
            cards = (selected[-budget:] if budget else []) + active
        context = {
            "history": self.history[
                -self.config.get("context", {}).get("recent_turns", 6) :
            ],
            "tasks": cards[-self.config["runtime"].get("max_context_tasks", 4) :],
            "played": self.played[-4:],
            "skill_candidates": self.skills.cards(),
            "memory_candidates": [],
            "tools": self.tools.cards,
            "examples": self.examples[
                : self.config.get("context", {}).get("max_examples", 4)
            ],
            "user_speaking": self.user_speaking,
            "playback_active": bool(self.response_id),
            "language": self.response_language,
            "policy_version": self.config["policy_version"],
            "user_profile": self.profile,
        }
        if self.config["runtime"].get("task_dialogue"):
            from jarvis.core.task_dialogue import view

            context["task_dialogue"] = view(self)
        if self.config["runtime"].get("conversation_v3"):
            context["conversation_focus"] = self.dialogue_focus
        if self.config["runtime"].get("playback_control_v2") and self.response_id:
            response = self.presentations[self.response_id]
            key = response.get("task")
            task = self.delivery_task(key) if key else None
            context["active_playback"] = {
                "response_id": self.response_id,
                "purpose": response["purpose"],
                "task_id": key[0] if key else None,
                "version": key[1] if key else None,
                "request": task.objective[:160] if task else "",
                "available_text": (
                    response.get("approved_text") or "".join(response["text"])
                )[:240],
            }
        if self.config["runtime"].get("playback_control_v2"):
            for response in reversed(list(self.presentations.values())):
                key = response.get("task")
                task = self.delivery_task(key) if key else None
                if (
                    response["purpose"] == "result"
                    and response["interrupted"]
                    and task
                    and task.delivery_hold_reason
                ):
                    context["interrupted_playback"] = {
                        "task_id": key[0],
                        "version": key[1],
                    }
                    break
        return context

    def hold_announcements(self, reason, key=None):
        for task in self.tasks.values():
            current = (task.task_id, task.version)
            if (
                key is not None
                and current != key
                or current in self.committed
                or task.status in {TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}
            ):
                continue
            task.delivery_hold_reason = reason
            task.delivery_held_at = time.time()
            task.delivery_state = "awaiting_user"
            if current in self.requested_results:
                self.requested_results.remove(current)
            self.persist_task(task)
            self.emit(
                "task.delivery_held",
                task_id=task.task_id,
                version=task.version,
                reason=reason,
            )

    def fence_playback(self, reason, *, all_announcements=False):
        if not self.config["runtime"].get("playback_control_v3"):
            return
        self.playback_control_epoch += 1
        if all_announcements:
            sources = set(self.transcripts)
            if self.current_utterance:
                sources.add(self.current_utterance)
            self.suppressed_announcement_sources.update(dict.fromkeys(sources, reason))
        self.emit(
            "playback.intents_invalidated",
            reason=reason,
            playback_control_epoch=self.playback_control_epoch,
        )

    def announcement_hold_for(self, utterance_id):
        if self.config["runtime"].get("playback_control_v3"):
            return self.suppressed_announcement_sources.get(
                utterance_id.partition(":")[0]
            )

    def stale_playback_intent(self, decision, update):
        if not self.config["runtime"].get("playback_control_v3"):
            return False
        speech_controls = {
            "RESULT",
            "RESULTS",
            "STATUS",
            "PROGRESS",
            "RECALL",
            "TASK_SUMMARY",
            "TASK_CHOICES",
            "TASK_MATCHES",
            "TASK_NOT_FOUND",
            "STOP_SPEAKING",
            "HOLD_SPEECH",
            "DISMISS_SPEECH",
        }
        reads_existing = decision.control == "EXECUTE" and any(
            (
                (identifier, self.tasks[identifier].version) in self.results
                for identifier in decision.raw.get(
                    "task_ids", [decision.target_task_id]
                )
                if identifier in self.tasks
            )
        )
        if (
            decision.route not in {"FAST", "MEMORY", "CLARIFY", "IGNORE"}
            and decision.control not in speech_controls
            and (not reads_existing)
        ):
            return False
        return (
            decision.raw.get("playback_control_epoch", self.playback_control_epoch)
            != self.playback_control_epoch
            or bool(self.announcement_hold_for(update.utterance_id))
            or bool(
                self.current_utterance
                and update.utterance_id.partition(":")[0]
                != self.current_utterance.partition(":")[0]
            )
        )

    def persist_task(self, task):
        if self.task_store:
            self.spawn(
                self.task_store.save(
                    self.user_id,
                    task.model_copy(deep=True),
                    self.results.get((task.task_id, task.version)),
                ),
                memory=True,
            )

    async def restore_tasks(self):
        if not self.task_store:
            return
        if self.config["runtime"].get("task_dialogue"):
            self.task_catalog = await self.task_store.catalog(self.user_id)
            if self.task_catalog:
                self.task_focus_id = max(
                    self.task_catalog, key=lambda r: r["last_mentioned_at"]
                )["task_id"]
        for task, result in await self.task_store.load(self.user_id):
            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                task.status = TaskStatus.FAILED
                result = TaskResult(
                    task_id=task.task_id,
                    version=task.version,
                    status="failed",
                    spoken_answer="这个任务在上次连接结束前没有完成，可以让我继续。",
                    details="Previous session ended before completion; no tool automatically rerun.",
                )
            self.tasks[task.task_id] = task
            if result:
                key = (task.task_id, task.version)
                self.results[key] = result
                self.committed.add(key)
                self.emit(
                    "task.restored",
                    **result.model_dump(mode="json"),
                    objective=task.objective,
                    delivery_state=task.delivery_state,
                )
        self.emit("tasks.loaded", count=len(self.tasks))

    def artifact_context(self):
        rows = []
        for task in self.tasks.values():
            result = self.results.get((task.task_id, task.version))
            artifacts = (
                result.artifacts
                if result and result.artifacts
                else [
                    a
                    for row in task.observations
                    if row.get("task_id") == task.task_id
                    for a in row.get("artifacts", [])
                ]
            )
            if artifacts and task.status not in {
                TaskStatus.CANCELLED,
                TaskStatus.SUPERSEDED,
            }:
                rows.append(
                    {
                        "task_id": task.task_id,
                        "version": task.version,
                        "objective": task.objective,
                        "artifacts": artifacts,
                    }
                )
        return rows[-8:]

    async def refresh_profile(self, query=None):
        if self.config["memory"].get("profile_context"):
            self.profile = await self.memory.profile(self.user_id)
            self.emit(
                "memory.profile.loaded",
                memory_ids=[m["id"] for m in self.profile],
                phase="turn",
            )
        if query and self.task_store:
            matches = await self.task_store.search(self.user_id, query)
            self.matched_task_ids = [t.task_id for t, _ in matches]
            for task, result in reversed(matches):
                if task.task_id in self.tasks:
                    current = self.tasks.pop(task.task_id)
                    self.tasks[task.task_id] = current
                    continue
                if task.status in {
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                    TaskStatus.CANCELLED,
                    TaskStatus.SUPERSEDED,
                }:
                    continue
                self.tasks[task.task_id] = task
                if result:
                    key = (task.task_id, task.version)
                    self.results[key] = result
                    self.committed.add(key)
                    self.emit(
                        "task.restored",
                        **result.model_dump(mode="json"),
                        objective=task.objective,
                    )

    def clear_playback(self, reason):
        self.paused_response = None
        self.generation_epoch += 1
        old_response = self.response_id
        if old_response and old_response in self.presentations:
            self.presentations[old_response]["interrupted"] = True
            self.presentations[old_response]["interruption_reason"] = reason
        self.response_id = None
        self.send_client(
            {
                "type": "playback.clear",
                "generation_epoch": self.generation_epoch,
                "reason": reason,
            }
        )
        self.send_venus(
            {
                "type": "control",
                "generation_epoch": self.generation_epoch,
                "hold": True,
                "end_response": True,
            }
        )
        self.emit(
            "assistant.playback.clear_requested",
            generation_epoch=self.generation_epoch,
            response_id=old_response,
            reason=reason,
        )
        if old_response and self.config["runtime"].get("task_interaction_v2"):
            self.finish_presentation(old_response)

    def pause_playback(self):
        if not self.response_id or self.paused_response:
            return
        self.paused_response = self.response_id
        self.presentations[self.response_id]["paused_at"] = time.monotonic()
        if self.config["runtime"].get("conversation_v3"):
            response = self.presentations[self.response_id]
            if any((self.chunks[k]["played"] for k in response["chunks"])):
                from jarvis.core.conversation import remember_spoken

                remember_spoken(
                    self,
                    response,
                    response.get("approved_text") or "".join(response["text"]),
                    complete=False,
                )
        self.send_client(
            {"type": "playback.pause", "generation_epoch": self.generation_epoch}
        )
        self.send_venus(
            {"type": "control", "generation_epoch": self.generation_epoch, "hold": True}
        )
        self.emit(
            "assistant.playback.paused",
            response_id=self.response_id,
            reason="speech_candidate",
        )

    def resume_playback(self):
        rid = self.paused_response
        if rid and rid == self.response_id:
            p = self.presentations[rid]
            p["resumed_after_unconfirmed_input"] = True
            self.response_language = p.get("language", self.response_language)
            p["deadline"] += time.monotonic() - p.pop("paused_at", time.monotonic())
            self.send_client(
                {"type": "playback.resume", "generation_epoch": self.generation_epoch}
            )
            if not p["generated_done"] and (not p.get("speech_check_pending")):
                self.send_venus(
                    {
                        "type": "control",
                        "generation_epoch": self.generation_epoch,
                        "hold": False,
                    }
                )
            self.emit(
                "assistant.playback.resumed",
                response_id=rid,
                reason="no_new_directed_request",
            )
        self.paused_response = None

    def speak(
        self,
        backend=None,
        purpose="fast",
        task=None,
        source_utterance=None,
        user_text=None,
    ):
        if source_utterance and source_utterance != self.current_utterance:
            return None
        if self.user_speaking or self.muted:
            return None
        if (
            self.config["runtime"].get("conversational_delivery")
            and self.response_id
            and (purpose in {"result", "failure", "progress"})
        ):
            return None
        if (
            self.config["jev"].get("interactive_routing")
            and self.response_language == "en"
            and backend
        ):
            from jarvis.core.language import ENGLISH

            backend = ENGLISH.get(backend, backend)
        self.clear_playback("new_response")
        response_id = self.response_id = new_id()
        from jarvis.core.speech_budget import response_budget
        from jarvis.core.spoken_numbers import render

        spoken_backend = (
            render(backend, self.response_language)
            if self.config["venus"].get("verbatim_backend")
            else backend
        )
        budget = response_budget(
            spoken_backend, self.config["runtime"].get("response_timeout_s", 30)
        )
        self.presentations[response_id] = {
            "purpose": purpose,
            "task": task,
            "chunks": [],
            "language": self.response_language,
            "authoritative_backend": bool(backend),
            "approved_text": backend,
            "source_utterance_id": source_utterance,
            "text": [],
            "generated_done": False,
            "interrupted": False,
            "generated_samples": 0,
            "epoch": self.generation_epoch,
            "history_recorded": False,
            "deadline": time.monotonic() + budget,
        }
        self.send_venus(
            {
                "type": "control",
                "generation_epoch": self.generation_epoch,
                "response_id": response_id,
                "hold": False,
                "start_response": True,
                "approved_text": spoken_backend
                if backend and self.config["venus"].get("verbatim_backend")
                else None,
                "backend": backend or "",
                "question": user_text or "",
            }
        )
        if spoken_backend != backend:
            self.emit(
                "assistant.speech.normalized",
                response_id=response_id,
                original=backend,
                spoken=spoken_backend,
                method="english_number_words",
            )
        self.emit(
            "assistant.response.started",
            response_id=response_id,
            purpose=purpose,
            timeout_s=budget,
            generation_epoch=self.generation_epoch,
            task_id=task[0] if task else None,
            version=task[1] if task else None,
            source_utterance_id=source_utterance,
        )
        self.watch_response_timeout(response_id, budget)
        return response_id

    def watch_response_timeout(self, response_id, budget):

        async def watchdog():
            await asyncio.sleep(budget)
            self.post("response.timeout", {"response_id": response_id})

        self.spawn(watchdog())

    async def run(self):
        await self.restore_tasks()
        if self.config["jev"].get("prewarm_connection", True) and hasattr(
            self.controller, "warm_connection"
        ):
            self.spawn(self.controller.warm_connection())
        self.emit(
            "session.ready",
            policy_version=self.config["policy_version"],
            user_id=self.user_id,
            mode=self.config.get("execution_mode", "live"),
            prefix_enabled=self.config["prefix"]["enabled"],
        )
        while not self.closed:
            type, payload = await self.queue.get()
            if type == "shutdown":
                break
            try:
                await self.handle(type, payload)
            except Exception as error:
                if self.config.get("voxel", {}).get("enabled"):
                    import traceback

                    self.emit(
                        "voxel.actor.failure",
                        operation=type,
                        error=type_name(error),
                        frames=[
                            {
                                "file": f.filename.rsplit("/", 1)[-1],
                                "line": f.lineno,
                                "function": f.name,
                            }
                            for f in traceback.extract_tb(error.__traceback__)[-6:]
                        ],
                    )
                self.emit("runtime.error", operation=type, error=type_name(error))

    async def handle(self, type, payload):
        if type == "history.reset.ready":
            if self.history_reset_pending:
                text = self.history_reset_pending
                self.history_reset_pending = None
                if not self.current_utterance and (not self.user_speaking):
                    self.speak(text, "confirmation")
        elif type == "user.speech.started":
            self.focus_epoch += 1
            self.user_speaking = True
            self.route_pending = True
            self.muted = False
            self.current_utterance = payload["utterance_id"]
            if (
                self.config["runtime"].get("reversible_interruptions")
                and self.response_id
            ):
                self.pause_playback()
            else:
                self.clear_playback("speech_started")
            self.emit(type, **payload)
        elif type == "user.speech.ended":
            self.user_speaking = False
            self.emit(type, **payload)
        elif type in ("asr.partial", "asr.final"):
            await self.transcript(TranscriptUpdate.model_validate(payload))
        elif type == "decision.ready":
            await self.apply_decision(payload)
        elif type == "prefix.trigger":
            self.prefix_timer = None
            self.start_prefix()
        elif type == "worker.started":
            task = self.current_task(payload["task_id"], payload["version"])
            if task:
                task.status = TaskStatus.RUNNING
                self.emit("task.started", **task.model_dump(mode="json"))
        elif type == "worker.completed":
            await self.accept_result(payload)
        elif type == "fast.answer.checked":
            from jarvis.core.conversation import finish_fast

            await finish_fast(self, **payload)
        elif type == "delivery.ready":
            await self.apply_delivery(**payload)
        elif type == "delivery.wake":
            self.delivery_timer = None
            await self.try_deliver()
        elif type == "delivery.retry":
            self.delivery_retry_scheduled.discard(tuple(payload["key"]))
            await self.try_deliver()
        elif type == "result.ready_hint":
            self.ready_hints_pending.add(tuple(payload["key"]))
            await self.try_deliver()
        elif type == "venus.step":
            self.venus_step(payload)
        elif type == "speech.checked":
            if (
                payload["response_id"] != self.response_id
                or payload["generation_epoch"] != self.generation_epoch
            ):
                self.emit(
                    "assistant.speech.check_discarded",
                    response_id=payload["response_id"],
                    reason="stale_response",
                )
                return
            p = self.presentations[self.response_id]
            p["speech_check_pending"] = False
            self.emit("assistant.speech.checked", **payload)
            if payload.get("phase") == "tail":
                if not payload.get("matches"):
                    p["audio_complete"] = False
                self.emit(
                    "assistant.speech.tail_verified",
                    response_id=self.response_id,
                    verified=bool(payload.get("matches")),
                )
                p["generated_done"] = True
                self.finish_presentation(self.response_id)
                return
            suffix = payload.get("suffix")
            if (
                self.config["runtime"].get("task_interaction_v2")
                and p["authoritative_backend"]
                and payload.get("substantial_omission")
                and (not suffix)
            ):
                p["audio_complete"] = False
            if (
                suffix
                and p.get("resumed_after_unconfirmed_input")
                and (not p.get("authoritative_backend"))
            ):
                p["audio_complete"] = False
                self.emit(
                    "assistant.speech.tail_suppressed",
                    response_id=self.response_id,
                    reason="untrusted_continuation_after_ignored_input",
                )
                suffix = None
            if suffix and (not self.user_speaking) and (not self.muted):
                p["repair_active"] = True
                p["repair_start_samples"] = p["generated_samples"]
                p["repair_text"] = payload.get("repair_text") or suffix
                self.send_venus(
                    {
                        "type": "control",
                        "response_id": self.response_id,
                        "generation_epoch": self.generation_epoch,
                        "hold": False,
                        "start_response": True,
                        "approved_text": p["repair_text"],
                    }
                )
                self.emit(
                    "assistant.speech.tail_requested",
                    response_id=self.response_id,
                    text=p["repair_text"],
                    missing=suffix,
                )
            else:
                p["generated_done"] = True
                self.finish_presentation(self.response_id)
        elif type == "playback.ack":
            self.playback_ack(payload)
        elif type == "playback.event":
            self.emit(payload["type"], **payload.get("payload", {}))
        elif type == "stop_speaking":
            self.muted = True
            self.fence_playback("user_stopped_announcements", all_announcements=True)
            if self.config["runtime"].get("playback_control_v2"):
                self.hold_announcements("user_stopped_announcements")
            self.clear_playback("stop_button")
        elif type == "present_result":
            task = self.delivery_task((payload.get("task_id"), payload.get("version")))
            if (
                task
                and (not task.provisional)
                and ((task.task_id, task.version) in self.results)
            ):
                self.muted = False
                self.present(
                    task, self.results[task.task_id, task.version], replay=True
                )
        elif type == "response.timeout":
            identifier = payload["response_id"]
            if self.response_id == identifier:
                remaining = (
                    self.presentations[identifier]["deadline"] - time.monotonic()
                )
                if self.paused_response == identifier or remaining > 0:

                    async def later():
                        await asyncio.sleep(max(1, remaining))
                        self.post("response.timeout", {"response_id": identifier})

                    self.spawn(later())
                    return
                self.clear_playback("response_deadline")
                self.emit(
                    "assistant.response.failed",
                    response_id=identifier,
                    reason="response_deadline",
                )
                await self.try_deliver()
        elif type == "task.progress":
            if self.config["runtime"].get("conversational_delivery"):
                return
            task = self.current_task(payload["task_id"], payload["version"])
            if (
                task
                and task.status == TaskStatus.RUNNING
                and (not self.user_speaking)
                and (not self.response_id)
            ):
                now = time.monotonic()
                if (
                    now - self.last_progress
                    >= self.config["worker"]["progress_cooldown_s"]
                ):
                    self.last_progress = now
                    self.speak(
                        "这个任务还在处理。",
                        "progress",
                        task=(task.task_id, task.version),
                    )
        elif type == "feedback":
            event = self.emit(
                "user.feedback",
                text=payload["text"],
                response_id=payload.get("response_id"),
            )
            identifier = await self.memory.feedback(
                self.user_id,
                self.session_id,
                event.event_id,
                payload["text"],
                self.config["policy_version"],
                payload.get("response_id"),
                payload.get("task_id"),
            )
            self.emit(
                "feedback.saved",
                feedback_id=identifier,
                source_event_id=event.event_id,
                rsi_use="dev evidence; fixed gates still required",
            )
        elif type == "runtime.error":
            self.emit(type, **payload)

    async def transcript(self, update):
        if update.audio_end_sample <= self.history_audio_floor:
            self.emit(
                "asr.stale_ignored",
                utterance_id=update.utterance_id,
                reason="history_erased",
            )
            return
        previous = self.transcripts.get(update.utterance_id)
        if previous and (previous.revision >= update.revision or previous.is_final):
            self.emit(
                "asr.stale_ignored",
                utterance_id=update.utterance_id,
                revision=update.revision,
            )
            return
        self.transcripts[update.utterance_id] = update
        self.current_utterance = update.utterance_id
        event_type = (
            ("native.request.final" if update.is_final else "native.request.partial")
            if update.utterance_id in getattr(self, "native_sources", set())
            else "asr.final"
            if update.is_final
            else "asr.partial"
        )
        event = self.emit(event_type, **update.model_dump())
        if update.is_final:
            self.focus_epoch += 1
            from jarvis.core.language import response_language

            self.response_language = response_language(
                update.text, update.language, self.config.get("speech_language", "auto")
            )
            self.user_speaking = False
            self.route_pending = True
            normalized = re.sub("[\\s，。！？,.!?]", "", update.text).lower()
            if normalized in {
                "先别说了",
                "别说了",
                "停止说话",
                "stopspeaking",
                "stoptalking",
            }:
                self.muted = True
                self.clear_playback("recognized_stop_command")
            self.prefix_pending = None
            if self.prefix_job:
                self.prefix_job.cancel()
                self.prefix_job = None
            if not update.text.strip():
                self.route_pending = False
                self.resume_playback()
                await self.try_deliver()
                return
            self.history.append(
                {"role": "user", "text": update.text, "source_event_id": event.event_id}
            )
            if self.config["runtime"].get("playback_control_v2"):
                from jarvis.core.playback_control import literal_action, decision

                action = literal_action(update.text)
                if action:
                    epoch = self.decision_epochs.get(update.utterance_id, 0) + 1
                    self.decision_epochs[update.utterance_id] = epoch
                    await self.apply_decision(
                        decision(
                            update, epoch, action, self.context().get("active_playback")
                        )
                    )
                    return
            if (
                self.config["memory"].get("enabled")
                and getattr(self, "mode", None) != "voxel"
            ):
                observer_id = getattr(self, "_binding_observation_id", None)
                observed = getattr(self, "observed_memory", set())
                if not observer_id or observer_id not in observed:
                    if observer_id:
                        observed.add(observer_id)
                    self.schedule_personal_memory(update, event.event_id)
            await self.refresh_profile(update.text)
            self.start_route(update)
        elif self.config["prefix"]["enabled"]:
            self.prefix_pending = update
            if self.prefix_job is None and self.prefix_timer is None:
                delay = max(
                    0,
                    self.config["prefix"]["min_trigger_interval_ms"] / 1000
                    - (time.monotonic() - self.last_prefix_time),
                )

                async def trigger():
                    await asyncio.sleep(delay)
                    self.post("prefix.trigger")

                self.prefix_timer = self.spawn(trigger())

    def start_prefix(self):
        update = self.prefix_pending
        if (
            update is None
            or self.prefix_job is not None
            or self.transcripts[update.utterance_id].is_final
        ):
            return
        self.prefix_pending = None
        self.last_prefix_time = time.monotonic()
        self.prefix_job = self.start_route(update)

    def start_route(self, update):
        source = (update.utterance_id, update.revision)
        if update.is_final and self.config["runtime"].get("playback_control_v3"):
            if source in self.final_routes or source in self.applied_final_decisions:
                self.emit(
                    "jev.duplicate_route_suppressed", utterance_id=update.utterance_id
                )
                return None
            self.final_routes.add(source)
        epoch = self.decision_epochs.get(update.utterance_id, 0) + 1
        self.decision_epochs[update.utterance_id] = epoch
        context = self.context()
        playback_epoch = self.playback_control_epoch

        async def route():
            decision = await self.controller.route(update, epoch, context)
            if self.config["runtime"].get("playback_control_v3"):
                decision.raw["playback_control_epoch"] = playback_epoch
            self.post("decision.ready", decision)

        return self.spawn(route())

    async def apply_decision(self, decision: Decision):
        update = self.transcripts.get(decision.utterance_id)
        if (
            not update
            or update.revision != decision.revision
            or self.decision_epochs.get(decision.utterance_id)
            != decision.decision_epoch
        ):
            self.emit(
                "jev.stale_ignored",
                utterance_id=decision.utterance_id,
                revision=decision.revision,
            )
            if self.prefix_job and self.prefix_job.done():
                self.prefix_job = None
                self.start_prefix()
            return
        if update.is_final and self.config["runtime"].get("playback_control_v3"):
            source = (update.utterance_id, update.revision)
            if source in self.applied_final_decisions:
                self.emit(
                    "jev.duplicate_decision_suppressed",
                    utterance_id=update.utterance_id,
                )
                return
            self.applied_final_decisions.add(source)
            if self.stale_playback_intent(decision, update):
                if update.utterance_id == self.current_utterance:
                    self.route_pending = False
                self.emit(
                    "playback.control_discarded",
                    reason="superseded_playback_intent",
                    utterance_id=update.utterance_id,
                    control=decision.control,
                )
                return
        self.emit("decision.applied", **decision.model_dump(mode="json"))
        if not update.is_final:
            self.prefix_job = None
            if decision.route == "PREFETCH_WORKER" and (
                not any(
                    (
                        t.provisional and t.status not in TERMINAL
                        for t in self.tasks.values()
                    )
                )
            ):
                self.start_worker(update, provisional=True)
            elif decision.route == "PREFETCH_MEMORY":

                async def prefetch():
                    candidates = await self.memory_search(
                        update.text, self.config["memory"]["candidate_k"]
                    )
                    self.memory_prefetch[update.utterance_id] = candidates
                    self.emit(
                        "memory.prefetch.ready",
                        utterance_id=update.utterance_id,
                        revision=update.revision,
                        candidate_ids=[m["id"] for m in candidates],
                    )

                self.spawn(prefetch())
            self.start_prefix()
            return
        is_current = update.utterance_id == self.current_utterance
        if decision.raw.get("playback_control") and (not is_current):
            self.emit("playback.control_discarded", reason="newer_user_turn")
            return
        if is_current:
            self.route_pending = False
            if self.paused_response:
                if decision.route == "IGNORE":
                    self.resume_playback()
                else:
                    self.clear_playback("confirmed_user_request")
        provisional = next(
            (
                t
                for t in self.tasks.values()
                if t.source_utterance_id == update.utterance_id
                and t.provisional
                and (t.status not in TERMINAL)
            ),
            None,
        )
        targeted_provisional = (
            provisional
            and decision.route == "CONTROL"
            and (
                (decision.target_task_id, decision.target_version)
                == (provisional.task_id, provisional.version)
            )
        )
        if provisional and (not targeted_provisional):
            expected = f"REUSE:{provisional.task_id}:{provisional.version}"
            if decision.route == "WORKER" and decision.prefetch_resolution == expected:
                provisional.provisional = False
                provisional.objective = update.text
                self.emit(
                    "task.promoted",
                    task_id=provisional.task_id,
                    version=provisional.version,
                )
                if is_current:
                    self.speak(
                        "我来处理一下。", "ack", source_utterance=update.utterance_id
                    )
                await self.try_deliver()
                return
            self.invalidate(provisional, TaskStatus.SUPERSEDED)
            self.logical_requests.pop(update.utterance_id, None)
        if decision.route == "BATCH":
            if decision.utterance_id in self.processed_batches:
                return
            self.processed_batches.add(decision.utterance_id)
            if is_current:
                self.route_pending = True
            self.spawn(self.handle_batch(update, decision))
        elif decision.route == "WORKER":
            self.start_worker(update, skill=decision.skill_choice)
            if is_current:
                if self.config["runtime"].get("conversation_v3") and any(
                    (self.result_eligible(k) for k in self.results)
                ):
                    await self.try_deliver()
                else:
                    ack = (
                        ("Okay." if self.response_language == "en" else "好。")
                        if self.config["runtime"].get("conversation_v3")
                        else "我来处理一下。"
                    )
                    self.speak(ack, "ack", source_utterance=update.utterance_id)
        elif decision.route == "FAST":
            observed_text = (
                "<|im_start|>user\n" + update.text + "<|im_end|>"
                if self.config["venus"].get("fast_asr_context")
                else None
            )
            if self.config["runtime"].get("conversation_v3"):
                from jarvis.core.conversation import fast_context

                observed_text = fast_context(self, update.text)
            conflict = decision.raw.get("choices", {}).get("constraint", "NONE")
            fact = next((m for m in self.profile if m["id"] == conflict), None)
            answer = None
            recall = decision.raw.get("choices", {}).get("recall_fact", "NONE")
            if (
                self.config["runtime"].get("conversation_v3")
                and decision.raw.get("confidences", {}).get("recall_fact", 0)
                >= self.config["jev"]["control_accept"]
            ):
                recalled = next((m for m in self.profile if m["id"] == recall), None)
                if recalled:
                    answer = (
                        recalled.get("english_text") or recalled["text"]
                        if self.response_language == "en"
                        else "你刚才提到的限制是：" + recalled["text"]
                    )
                    self.emit(
                        "memory.recall.applied",
                        memory_id=recalled["id"],
                        utterance_id=update.utterance_id,
                    )
            if fact:
                if self.response_language == "en":
                    answer = (
                        "I wouldn't recommend that for you. " + fact["english_text"]
                        if fact.get("english_text")
                        else "I wouldn't recommend that, because it conflicts with a restriction you told me about."
                    )
                    if self.config["runtime"].get("task_interaction_v2") and fact.get(
                        "english_text"
                    ):
                        answer = fact["english_text"] + " Please avoid that."
                else:
                    answer = "这个选择不适合你。你之前告诉我：" + fact["text"]
                self.emit(
                    "memory.constraint.applied",
                    memory_id=fact["id"],
                    utterance_id=update.utterance_id,
                )
            self.speak(
                backend=answer,
                purpose="fast",
                source_utterance=update.utterance_id,
                user_text=observed_text,
            )
        elif decision.route == "IGNORE":
            self.quiet_since = time.monotonic()
        elif decision.route == "MEMORY":
            self.spawn(self.retrieve_memory(update, decision))
        elif decision.route == "CONTROL":
            await self.control(decision, update)
        elif decision.route == "CLARIFY":
            prompt = (
                "现在暂时无法处理，请稍后再试。"
                if decision.decision_origin == "terminal_fallback"
                else {
                    "TARGET": "你指的是哪一个任务？",
                    "CONSTRAINT": "你最在意的条件是什么？",
                    "GOAL": "你想让我帮你完成什么？",
                    "NONE": "能再具体说一下你的要求吗？",
                    "PERSONAL_CONSTRAINT": "这可能与你之前告诉我的限制有冲突。你是在修改之前的信息吗？",
                }.get(decision.missing_slot, "还需要哪些条件？")
            )
            self.speak(prompt, "clarify", source_utterance=update.utterance_id)
        elif decision.route == "WAIT":
            self.speak(
                "还有需要补充的信息吗？",
                "clarify",
                source_utterance=update.utterance_id,
            )
        await self.try_deliver()

    async def handle_batch(self, update, decision):
        if decision.raw.get("typed_batch"):
            from jarvis.core.admitted_request import decompose
            from jarvis.core.delegated_route import route

            try:
                parts = await decompose(self, update.text)
            except Exception as error:
                self.emit("requests.split_failed", error=type(error).__name__)
                parts = [update.text]
            try:
                if update.audio_end_sample <= self.history_audio_floor or self.closed:
                    return
                self.emit(
                    "requests.planned",
                    utterance_id=update.utterance_id,
                    items=[{"text": part} for part in parts],
                )
                for index, text in enumerate(parts):
                    part = update.model_copy(
                        update={
                            "utterance_id": f"{update.utterance_id}:item:{index}",
                            "text": text,
                        }
                    )
                    selected = await route(
                        self.controller,
                        part,
                        decision.decision_epoch,
                        self.context(),
                        allow_batch=False,
                    )
                    if (
                        update.audio_end_sample <= self.history_audio_floor
                        or self.closed
                    ):
                        return
                    if selected.route == "CONTROL":
                        await self.control(
                            selected, update.model_copy(update={"text": text})
                        )
                    elif selected.route == "MEMORY":
                        await self.retrieve_memory(
                            update.model_copy(update={"text": text}), selected
                        )
                    else:
                        self.start_worker(
                            part,
                            group_id=update.utterance_id,
                            index=index + 1,
                            count=len(parts),
                        )
            finally:
                if self.current_utterance == update.utterance_id:
                    self.route_pending = False
            await self.try_deliver()
            return
        items = decision.raw.get("items")
        if not items:
            try:
                data = await self.worker.complete(
                    [
                        {
                            "role": "system",
                            "content": 'Split the user\'s independent requests. Return JSON {"requests":["exact verbatim span from input",...]}. Each span must be a continuous exact substring, 2 to 5 requests. Preserve all requests and shared constraints; never invent wording.',
                        },
                        {"role": "user", "content": update.text},
                    ],
                    json_mode=True,
                    max_tokens=2048,
                )
                choice = data["choices"][0]
                assert choice["finish_reason"] == "stop"
                parts = json.loads(choice["message"]["content"])["requests"]
                assert 2 <= len(parts) <= 5 and all(
                    (
                        isinstance(p, str) and p.strip() and (p in update.text)
                        for p in parts
                    )
                )
                cursor = 0
                covered = 0
                for part in parts:
                    start = update.text.find(part, cursor)
                    assert start >= cursor
                    cursor = start + len(part)
                    covered += len(part)
                assert covered >= len(update.text) * 0.8, (
                    "Incomplete decomposition; retain the full objective"
                )
                from jarvis.core.requests import route_batch

                routed = await route_batch(
                    self.controller,
                    update,
                    decision.decision_epoch,
                    self.context(),
                    parts,
                )
                items = routed.raw.get("items")
            except Exception as error:
                self.emit("batch.plan_failed", error=type(error).__name__)
        if not items:
            self.start_worker(update)
            if update.utterance_id == self.current_utterance:
                self.route_pending = False
            self.speak(
                "我会一起处理这些要求。", "ack", source_utterance=update.utterance_id
            )
            return
        self.emit("requests.planned", utterance_id=update.utterance_id, items=items)
        if self.config["runtime"].get("conversation_v3"):
            from jarvis.core.conversation import handle_batch

            await handle_batch(self, update, items)
            return
        if self.config["runtime"].get("task_dialogue"):
            for i, item in enumerate(items):
                part = update.model_copy(
                    update={
                        "utterance_id": f"{update.utterance_id}:{i}",
                        "text": item["text"],
                    }
                )
                if item["route"] == "CONTROL":
                    control = await self.controller.route(part, 1, self.context())
                    if control.route == "CONTROL":
                        await self.control(control, part)
                        continue
                self.start_worker(
                    part, group_id=update.utterance_id, index=i + 1, count=len(items)
                )
            if update.utterance_id == self.current_utterance:
                self.route_pending = False
                text = (
                    f"I've recorded all {len(items)} requests and will handle each one."
                    if self.response_language == "en"
                    else f"{len(items)}项要求都记下了，我会逐项处理。"
                )
                if self.config["runtime"].get("task_interaction_v2"):
                    text = (
                        f"I'll handle all {len(items)} requests."
                        if self.response_language == "en"
                        else f"这{len(items)}项我分别处理。"
                    )
                self.speak(text, "ack", source_utterance=update.utterance_id)
            return
        foreground = []
        started = []
        rejected = []
        for i, item in enumerate(items):
            if item["route"] == "FAST":
                foreground.append(item)
            else:
                part = update.model_copy(
                    update={
                        "utterance_id": f"{update.utterance_id}:{i}",
                        "text": item["text"],
                    }
                )
                if item["route"] == "CONTROL":
                    control = await self.controller.route(part, 1, self.context())
                    target = self.delivery_task(
                        (control.target_task_id, control.target_version)
                    )
                    if control.route == "CONTROL" and control.control in {
                        "REFINE",
                        "REPLACE",
                        "CANCEL",
                        "STOP_SPEAKING",
                    }:
                        await self.control(control, part)
                        started.append(item)
                        continue
                    if target and control.control in {"RECALL", "STATUS"}:
                        result = self.results.get((target.task_id, target.version))
                        answer = (
                            self.recall_text(target)
                            if control.control == "RECALL"
                            else result.spoken_answer
                            if result
                            else "这项任务还在处理中。"
                        )
                    else:
                        answer = (
                            "关于这项要求，需要你说明具体指哪一个任务：" + item["text"]
                        )
                    foreground.append({**item, "answer": answer})
                    continue
                task = self.start_worker(part)
                (started if task else rejected).append(item)
        facts = [
            m
            for m in self.profile
            if any((i.get("constraint") == m["id"] for i in foreground))
        ]
        if facts:
            retained = []
            for i, item in enumerate(foreground):
                if item.get("constraint", "NONE") != "NONE" or item.get("answer"):
                    retained.append(item)
                else:
                    part = update.model_copy(
                        update={
                            "utterance_id": f"{update.utterance_id}:answer:{i}",
                            "text": item["text"],
                        }
                    )
                    task = self.start_worker(part)
                    (started if task else rejected).append(item)
            foreground = retained
        if update.utterance_id != self.current_utterance or self.user_speaking:
            return
        self.route_pending = False
        if foreground:
            if facts:
                prefix = (
                    (
                        f"I've started {len(started)} background tasks. "
                        if self.response_language == "en"
                        else f"已开始处理另外{len(started)}项任务。"
                    )
                    if started
                    else ""
                )
                answer = (
                    prefix
                    + (
                        "Please avoid that choice. "
                        if self.response_language == "en"
                        else "这个选择不适合你。"
                    )
                    + " ".join(
                        (
                            m.get("english_text") or m["text"]
                            if self.response_language == "en"
                            else m["text"]
                            for m in facts
                        )
                    )
                )
                answer += " ".join((i["answer"] for i in foreground if i.get("answer")))
                self.speak(answer, "fast", source_utterance=update.utterance_id)
                for m in facts:
                    self.emit(
                        "memory.constraint.applied",
                        memory_id=m["id"],
                        utterance_id=update.utterance_id,
                    )
            else:
                text = "；".join((i["text"] for i in foreground))
                approved = (
                    "；".join((i["answer"] for i in foreground))
                    if all((i.get("answer") for i in foreground))
                    else None
                )
                self.speak(
                    backend=approved,
                    purpose="fast",
                    source_utterance=update.utterance_id,
                    user_text="<|im_start|>user\n" + text + "<|im_end|>",
                )
        else:
            self.speak(
                f"已拆成{len(started)}项任务，分别开始处理。",
                "ack",
                source_utterance=update.utterance_id,
            )
        if rejected:
            self.emit(
                "requests.not_started",
                reason="queue_capacity",
                requests=[i["text"] for i in rejected],
            )

    def start_worker(
        self,
        update,
        provisional=False,
        skill="NONE",
        replacement=None,
        group_id=None,
        index=1,
        count=1,
        execution_mode="worker",
        expected_output=None,
        title=None,
    ):
        if not replacement and update.utterance_id in self.logical_requests:
            self.emit(
                "task.duplicate_suppressed", logical_request_id=update.utterance_id
            )
            return self.tasks[self.logical_requests[update.utterance_id]]
        execution_mode = replacement.execution_mode if replacement else execution_mode
        if replacement:
            replacement.delivery_hold_reason = None
            replacement.delivery_held_at = 0
            replacement.delivery_state = "pending"
        active = sum(
            (
                t.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
                and t.execution_mode == "worker"
                for t in self.tasks.values()
            )
        )
        capacity = (
            execution_mode == "worker"
            and active
            >= self.config["runtime"]["max_active_workers"]
            + self.config["runtime"]["max_queued_tasks"]
        )
        if capacity and (not self.config["runtime"].get("task_dialogue")):
            self.emit("task.rejected", reason="queue_capacity")
            return None
        task = replacement or Task(
            logical_request_id=update.utterance_id,
            source_utterance_id=update.utterance_id,
            objective=update.text,
            provisional=provisional,
            history=self.history[-6:],
            skill=self.skills.body(skill),
            request_group_id=group_id or update.utterance_id,
            request_index=index,
            request_count=count,
            origin_session_id=self.session_id,
            last_mentioned_at=time.time(),
            memory=list(self.profile),
            observations=self.artifact_context()
            if self.task_store
            and (not self.config["runtime"].get("task_interaction_v2"))
            else [],
            response_language=self.response_language,
            execution_mode=execution_mode,
            allowed_tool_scope=["read_only"]
            + (
                ["artifact_write"]
                if self.config["tools"].get("artifacts_enabled") and (not provisional)
                else []
            ),
        )
        if expected_output:
            task.expected_output = expected_output
        if title:
            task.progress["title"] = title
        if getattr(self, "workspace_attachment", None):
            task.history = task.history + [
                {
                    "role": "user",
                    "text": "User-provided workspace attachment: "
                    + self.workspace_attachment["text"],
                    "source": "user_pasted",
                }
            ]
        hold = self.announcement_hold_for(update.utterance_id)
        if hold:
            task.delivery_hold_reason = hold
            task.delivery_held_at = time.time()
            task.delivery_state = "awaiting_user"
        key = (task.task_id, task.version)
        if key in self.submitted:
            return task
        self.submitted.add(key)
        self.tasks[task.task_id] = task
        self.task_focus_id = task.task_id
        self.logical_requests[task.logical_request_id] = task.task_id
        self.emit("task.queued", **task.model_dump(mode="json"))
        self.persist_task(task)
        if execution_mode == "fast":
            task.expected_output = "answer"
            self.transcripts[update.utterance_id] = update
            return task
        if capacity:
            task.status = TaskStatus.FAILED
            result = TaskResult(
                task_id=task.task_id,
                version=task.version,
                status="failed",
                spoken_answer="The task queue is full; this request is saved for retry."
                if self.response_language == "en"
                else "队列已满，这项要求已记下，稍后可以继续。",
            )
            self.results[key] = result
            self.persist_task(task)
            self.emit(
                "task.failed", **result.model_dump(mode="json"), reason="queue_capacity"
            )
            return task

        async def run():
            async with self.worker_slots:
                self.post(
                    "worker.started", {"task_id": task.task_id, "version": task.version}
                )
                if (
                    not self.config.get("native_routing", {}).get("responsive_demo")
                    and self.config["runtime"].get("task_interaction_v2")
                    and (task.expected_output == "auto")
                    and hasattr(self.controller, "judge")
                ):
                    try:
                        values, confidence, _ = await self.controller.judge(
                            "work_contract",
                            {"objective": task.objective},
                            {
                                "output": {
                                    "instructions": "What observable output is required to complete ONLY this objective? A request to write code/create or modify a game requires an actual saved file. Current weather/news requires a successful live lookup. Advice/explanations require an answer, not an artifact.",
                                    "criteria": {
                                        "file": "Create or modify a usable file, code, game or document.",
                                        "lookup": "Find current external facts with a live tool.",
                                        "answer": "Give an explanation, advice, calculation or other answer.",
                                    },
                                }
                            },
                            allow_fallback=False,
                        )
                        if confidence["output"] >= self.config["jev"]["route_accept"]:
                            task.expected_output = values["output"]
                    except Exception as error:
                        self.emit(
                            "task.contract_unavailable",
                            task_id=task.task_id,
                            error=type(error).__name__,
                        )
                result = await self.worker.run_task(task)
                self.post("worker.completed", result)

        job = self.jobs[key] = self.spawn(run())

        async def progress():
            await asyncio.sleep(self.config["worker"]["first_progress_after_s"])
            self.post(
                "task.progress", {"task_id": task.task_id, "version": task.version}
            )

        if not provisional:
            self.spawn(progress())
        return task

    def current_task(self, task_id, version):
        task = self.tasks.get(task_id)
        return (
            task
            if task and task.version == version and (task.status not in TERMINAL)
            else None
        )

    def invalidate(self, task, status):
        task.status = status
        self.persist_task(task)
        job = self.jobs.get((task.task_id, task.version))
        if job and (not job.done()):
            job.cancel()
        if self.response_id and self.presentations[self.response_id]["task"] == (
            task.task_id,
            task.version,
        ):
            self.clear_playback("task_invalidated")
        self.emit(
            "task.cancelled" if status == TaskStatus.CANCELLED else "task.superseded",
            task_id=task.task_id,
            version=task.version,
        )

    async def control(self, decision, update):
        if decision.control == "SAVE_MEMORY":
            from jarvis.core.memory_receipts import confirmation

            self.spawn(
                confirmation(self, update.utterance_id, self.response_language == "en")
            )
            return
        if decision.control == "NONE":
            return
        if self.stale_playback_intent(decision, update):
            self.emit(
                "playback.control_discarded",
                reason="superseded_playback_intent",
                utterance_id=update.utterance_id,
                control=decision.control,
            )
            return
        if self.config["runtime"].get("playback_control_v2"):
            if decision.control in {"STOP_SPEAKING", "HOLD_SPEECH"}:
                self.muted = True
                self.fence_playback(
                    "user_stopped_announcements"
                    if decision.control == "STOP_SPEAKING"
                    else "routing_unavailable",
                    all_announcements=True,
                )
                self.hold_announcements(
                    "user_stopped_announcements"
                    if decision.control == "STOP_SPEAKING"
                    else "routing_unavailable"
                )
                self.clear_playback("semantic_stop_speaking")
                return
            if decision.control == "DISMISS_SPEECH":
                self.fence_playback("user_dismissed")
                if decision.target_task_id and decision.target_version:
                    self.hold_announcements(
                        "user_dismissed",
                        (decision.target_task_id, decision.target_version),
                    )
                self.clear_playback("user_dismissed")
                self.quiet_since = time.monotonic()
                self.last_spoken_purpose = "dismissed"
                return
        if self.config["runtime"].get("task_interaction_v2"):
            if decision.control == "CLEAR_HISTORY":
                from jarvis.core.history_control import clear

                await clear(self, decision.raw.get("include_profile", False))
                return
            if decision.control == "HISTORY_INFO":
                self.speak(
                    "Clearing history removes conversations, recordings, and task records. Saved personal information stays unless you ask to remove it too."
                    if self.response_language == "en"
                    else "清空历史会删除对话、录音和任务记录，保存的个人信息默认保留。",
                    "confirmation",
                    source_utterance=update.utterance_id,
                )
                return
            if decision.control in {"UNRESOLVED", "HISTORY_CLARIFY"}:
                text = (
                    (
                        "I can clear conversation and task history. Should saved personal information also be deleted?"
                        if self.response_language == "en"
                        else "可以清空对话和任务历史。保存的个人信息也要一起删除吗？"
                    )
                    if decision.control == "HISTORY_CLARIFY"
                    else "I could not identify that operation. Please state what you want me to do."
                    if self.response_language == "en"
                    else "这次没能识别你要的操作，请直接说要我做什么。"
                )
                self.speak(text, "clarify", source_utterance=update.utterance_id)
                return
        if self.config["runtime"].get("task_dialogue"):
            if await self.task_control(decision, update):
                return
        if decision.control == "STOP_SPEAKING":
            self.muted = True
            self.clear_playback("semantic_stop_speaking")
            return
        if decision.control == "OVERVIEW":
            active = [
                t
                for t in self.tasks.values()
                if t.status in {TaskStatus.RUNNING, TaskStatus.QUEUED}
            ]
            if active:
                prefix = (
                    "I'm working on: "
                    if self.response_language == "en"
                    else "正在处理："
                )
                text = prefix + "; ".join((t.objective[:160] for t in active))
            else:
                text = (
                    "No background task is running right now."
                    if self.response_language == "en"
                    else "现在没有正在运行的后台任务。"
                )
            self.speak(text, "progress", source_utterance=update.utterance_id)
            return
        task = self.delivery_task((decision.target_task_id, decision.target_version))
        if not task:
            self.speak(
                "你指的是哪一个任务？", "clarify", source_utterance=update.utterance_id
            )
            return
        if decision.control == "CANCEL":
            self.invalidate(task, TaskStatus.CANCELLED)
            self.speak(
                "这个任务已经取消。", "ack", source_utterance=update.utterance_id
            )
        elif decision.control in {"REFINE", "REPLACE"}:
            old_result = self.results.get((task.task_id, task.version))
            self.invalidate(task, TaskStatus.SUPERSEDED)
            new = task.model_copy(
                deep=True,
                update={
                    "version": task.version + 1,
                    "status": TaskStatus.QUEUED,
                    "objective": update.text
                    if decision.control == "REPLACE"
                    else task.objective + "\nLatest user change:" + update.text,
                    "source_utterance_id": update.utterance_id,
                    "provisional": False,
                    "last_mentioned_at": time.time(),
                    "progress": {},
                    "observations": old_result.evidence
                    + [
                        {
                            "task_id": task.task_id,
                            "artifacts": old_result.artifacts,
                            "previous_answer": old_result.spoken_answer,
                            "details": old_result.details,
                        }
                    ]
                    if old_result
                    else task.observations,
                    "history": self.history[-6:],
                    "memory": list(self.profile),
                },
            )
            self.emit(
                "task.updated",
                task_id=new.task_id,
                version=new.version,
                objective=new.objective,
            )
            self.start_worker(update, replacement=new)
            self.logical_requests[update.utterance_id] = new.task_id
            self.speak(
                "好的，按最新要求处理。", "ack", source_utterance=update.utterance_id
            )
        elif decision.control == "STATUS":
            key = (task.task_id, task.version)
            if self.config["jev"].get("interactive_routing") and key in self.results:
                self.present(task, self.results[key], replay=True)
                return
            status_text = {
                TaskStatus.QUEUED: "这个任务正在等待处理。",
                TaskStatus.RUNNING: "这个任务还在处理。",
                TaskStatus.NEEDS_INPUT: "这个任务还需要你补充信息。",
                TaskStatus.COMPLETED: "结果已经准备好了。",
                TaskStatus.DELIVERED: "这个任务的结果已经告诉你了。",
            }.get(task.status, "这个任务已经结束了。")
            self.speak(
                status_text, "progress", task=key, source_utterance=update.utterance_id
            )
        elif decision.control == "RECALL":
            self.speak_recall(self.recall_text(task), [task], update)
        elif decision.control == "RESUME":
            key = (task.task_id, task.version)
            if key in self.results:
                self.present(task, self.results[key], replay=True)

    async def task_control(self, decision, update):
        from jarvis.core.task_dialogue import label, status_text

        ids = decision.raw.get("task_ids", []) or (
            [decision.target_task_id] if decision.target_task_id else []
        )
        selected = []
        for identifier in ids:
            if identifier not in self.tasks and self.task_store:
                record = await self.task_store.get(self.user_id, identifier)
                if record:
                    task, result = record
                    if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                        task.status = TaskStatus.FAILED
                        result = TaskResult(
                            task_id=task.task_id,
                            version=task.version,
                            status="failed",
                            spoken_answer="上次连接结束前未完成，可以继续。",
                        )
                    self.tasks[identifier] = task
                    if result:
                        self.results[identifier, task.version] = result
                        self.committed.add((identifier, task.version))
            if identifier in self.tasks:
                selected.append(self.tasks[identifier])
        action = decision.control
        if action in {"TASK_MATCHES", "TASK_NOT_FOUND"}:
            en = self.response_language == "en"
            if not selected:
                text = (
                    "I could not identify a saved task matching that description. Do you remember its name?"
                    if en
                    else "没找到能确定匹配这段描述的任务。你还记得它的名字吗？"
                )
            elif len(selected) == 1:
                text = self.recall_text(selected[0])
                self.task_focus_id = selected[0].task_id
            else:
                from jarvis.core.task_recall import file_label

                titles = []
                representatives = {}
                for task in selected:
                    result = self.results.get((task.task_id, task.version))
                    title = (
                        file_label(result.artifacts[0], self.response_language)
                        if result and result.artifacts
                        else label(task, result)
                    )
                    representatives.setdefault(title, task)
                    item = (
                        title
                        + (" — " if en else "，")
                        + status_text(task, self.response_language)
                    )
                    if item not in titles:
                        titles.append(item)
                text = (
                    ("Matching saved work: " if en else "找到的相关任务是：")
                    + ("; " if en else "；").join(titles[:3])
                    + ("." if en else "。")
                )
                if len(representatives) == 1:
                    text = self.recall_text(next(iter(representatives.values())))
                elif en:
                    text = (
                        "I found these saved projects: "
                        + "; ".join(list(representatives)[:3])
                        + ". You can open them from their result cards."
                    )
                if len(titles) > 3:
                    text += (
                        f" There are {len(titles)} matches; these are the latest three."
                        if en
                        else f"共找到{len(titles)}项，先说最近三项。"
                    )
            self.emit(
                "task.recalled",
                task_ids=[t.task_id for t in selected],
                scope="matching_subject",
            )
            self.speak_recall(text, selected, update)
            return True
        if action == "RESULTS":
            selected.sort(key=lambda t: (t.created_at, t.request_index))
            for task in selected:
                key = (task.task_id, task.version)
                if key in self.results and self.delivery_task(key):
                    self.committed.discard(key)
                    self.result_speech_attempts.pop(key, None)
                    task.delivery_hold_reason = None
                    task.delivery_state = "pending"
                    self.persist_task(task)
                    if key not in self.requested_results:
                        self.requested_results.append(key)
            if self.requested_results:
                self.quiet_since = 0
                await self.try_deliver()
            else:
                self.speak(
                    "No final result yet."
                    if self.response_language == "en"
                    else "还没有最终结果，完成后会逐项告诉你。",
                    "progress",
                    source_utterance=update.utterance_id,
                )
            return True
        if action in {"TASK_SUMMARY", "TASK_CHOICES"}:
            en = self.response_language == "en"
            rows = []
            selected = selected[:3]
            if (
                selected
                and len({t.request_group_id for t in selected}) == 1
                and (selected[0].request_count > 1)
            ):
                selected.sort(key=lambda t: t.request_index)
            for task in selected:
                result = self.results.get((task.task_id, task.version))
                ordinal = (
                    (
                        f"Item {task.request_index}: "
                        if en
                        else f"第{task.request_index}项，"
                    )
                    if task.request_count > 1
                    else ""
                )
                title = label(task, result)[:24]
                if self.config["runtime"].get("task_interaction_v2") and result:
                    from jarvis.core.task_delivery import short_title

                    title = short_title(task, result, self.response_language)
                rows.append(
                    ordinal
                    + title
                    + (" — " if en else "，")
                    + status_text(task, self.response_language)
                )
            text = (
                ("Recent tasks: " if en else "最近的任务有：") + "；".join(rows) + "。"
                if rows
                else "I have no saved tasks in this scope."
                if en
                else "这个范围内没有已记录的任务。"
            )
            if not rows and self.config["runtime"].get("task_interaction_v2"):
                text = (
                    "There are no saved task records." if en else "没有保存的任务记录。"
                )
            if decision.raw.get("choices", {}).get("action") == "PROGRESS" and (
                not any(
                    (
                        t.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
                        for t in self.tasks.values()
                    )
                )
            ):
                text = (
                    "No background work is running. "
                    if en
                    else "现在没有正在运行的后台任务。"
                ) + text
            if action == "TASK_CHOICES":
                text += " Which should I change?" if en else "你要操作其中哪一项？"
            self.speak_recall(text, selected, update)
            return True
        if not selected:
            return False
        task = selected[0]
        task.last_mentioned_at = time.time()
        self.task_focus_id = task.task_id
        self.persist_task(task)
        if (
            decision.target_version is not None
            and task.version != decision.target_version
        ):
            self.speak(
                "任务版本已更新，请再说一次要怎样处理。",
                "clarify",
                source_utterance=update.utterance_id,
            )
            return True
        if action == "PROGRESS" or (
            action == "EXECUTE"
            and task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
        ):
            result = self.results.get((task.task_id, task.version))
            if (
                self.config["runtime"].get("task_interaction_v2")
                and result
                and (task.status in {TaskStatus.COMPLETED, TaskStatus.DELIVERED})
            ):
                self.present(task, result, replay=True)
                return True
            text = (
                label(task, result)
                + "，"
                + status_text(task, self.response_language)
                + "。"
            )
            if (
                task.status == TaskStatus.RUNNING
                and task.progress.get("phase") in {None, "generating"}
                and (not task.progress.get("completed_tools"))
            ):
                game = bool(re.search("\\bgame\\b|游戏|贪吃蛇", task.objective, re.I))
                if self.response_language == "en":
                    text = (
                        "I'm still working on the game. It isn't ready yet."
                        if game
                        else "I'm still working on that task. No final result yet."
                    )
                else:
                    text = (
                        "游戏还在制作，暂时没有最终结果。"
                        if game
                        else "这项任务还在处理，暂时没有最终结果。"
                    )
            self.speak(text, "progress", source_utterance=update.utterance_id)
            return True
        if action == "RESULT" or (
            action == "EXECUTE"
            and task.status in {TaskStatus.COMPLETED, TaskStatus.DELIVERED}
        ):
            result = self.results.get((task.task_id, task.version))
            if result:
                self.present(task, result, replay=True)
            else:
                self.speak_recall(self.recall_text(task), [task], update)
            return True
        if action == "EXECUTE":
            if task.status in {TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}:
                self.speak(
                    "这项任务已取消或被替代，请明确要重新执行的内容。",
                    "clarify",
                    source_utterance=update.utterance_id,
                )
                return True
            self.invalidate(task, TaskStatus.SUPERSEDED)
            retry = task.model_copy(
                deep=True,
                update={
                    "version": task.version + 1,
                    "status": TaskStatus.QUEUED,
                    "source_utterance_id": update.utterance_id,
                    "progress": {},
                    "last_mentioned_at": time.time(),
                    "memory": list(self.profile),
                    "history": self.history[-6:],
                },
            )
            self.start_worker(update, replacement=retry)
            self.speak(
                "I'll continue that task."
                if self.response_language == "en"
                else "我继续处理这项任务。",
                "ack",
                source_utterance=update.utterance_id,
            )
            return True
        return False

    def speak_recall(self, text, tasks, update):
        targeted = self.config["runtime"].get("targeted_task_recall")
        key = (
            (tasks[0].task_id, tasks[0].version)
            if targeted and len(tasks) == 1
            else None
        )
        response = self.speak(
            text, "recall", task=key, source_utterance=update.utterance_id
        )
        if response and targeted:
            self.presentations[response]["recall_task_ids"] = [t.task_id for t in tasks]
            if tasks:
                from jarvis.core.conversation import remember_spoken

                remember_spoken(
                    self, self.presentations[response], text, complete=False
                )

    def recall_text(self, task):
        if not self.task_store:
            return task.objective
        result = self.results.get((task.task_id, task.version))
        files = [a["filename"] for a in result.artifacts] if result else []
        if files:
            title = result.artifacts[0].get("title", files[0])
            if self.config["runtime"].get("targeted_task_recall"):
                from jarvis.core.task_recall import file_label

                title = file_label(result.artifacts[0], self.response_language)
                if self.response_language == "en":
                    if files[0].lower().endswith(".py"):
                        return f"The Python code I saved for you implements {title}. You can open {files[0]} from its result card."
                    return f"The saved project is {title}. Its file is {files[0]}. You can open it from the result card."
                return f"之前做的是《{title}》，文件是 {files[0]}，可以在结果卡片打开。"
            if self.response_language == "en":
                return "Yes, I still have your saved file. You can open it from the result card."
            return (
                f"记得，保存了《{title}》，可以在页面打开。"
                if len(title) <= 24
                else "记得，之前保存的文件还在，可以在页面打开。"
            )
        return (
            "I have the earlier task: "
            if self.response_language == "en"
            else "记得，之前的任务是："
        ) + task.objective.split("\n")[0][:120]

    async def accept_result(self, result: TaskResult):
        key = (result.task_id, result.version)
        task = self.current_task(*key)
        if not task or key in self.results:
            self.emit(
                "task.result_discarded",
                task_id=result.task_id,
                version=result.version,
                reason="stale_cancelled_or_duplicate",
            )
            return
        self.results[key] = result
        task.completed_at = time.time()
        task.status = {
            "completed": TaskStatus.COMPLETED,
            "needs_input": TaskStatus.NEEDS_INPUT,
            "failed": TaskStatus.FAILED,
            "cancelled": TaskStatus.CANCELLED,
        }[result.status]
        self.emit(
            "task.completed"
            if result.status in {"completed", "needs_input"}
            else "task.failed",
            **result.model_dump(mode="json"),
            provisional=task.provisional,
        )
        self.persist_task(task)
        if result.status in {"completed", "needs_input"}:

            async def ready_hint():
                await asyncio.sleep(
                    self.config["runtime"].get("result_ready_hint_s", 12)
                )
                self.post("result.ready_hint", {"key": key})

            self.spawn(ready_hint())
        if (
            result.status == "failed"
            and (not self.config["runtime"].get("conversational_delivery"))
            and (not self.user_speaking)
            and (not self.route_pending)
        ):
            self.speak(result.spoken_answer, "failure")
        await self.try_deliver()

    def delivery_task(self, key):
        task = self.tasks.get(key[0])
        if (
            task
            and task.version == key[1]
            and (task.status not in {TaskStatus.CANCELLED, TaskStatus.SUPERSEDED})
        ):
            return task

    def wake_delivery(self, delay):
        if self.delivery_timer is None:

            async def wake():
                await asyncio.sleep(max(0, delay))
                self.post("delivery.wake")

            self.delivery_timer = self.spawn(wake())

    def result_eligible(self, key):
        task = self.delivery_task(key)
        return bool(
            task
            and (not task.provisional)
            and (key not in self.committed)
            and (
                not (
                    self.config["runtime"].get("intentional_delivery_interruptions")
                    and task.delivery_hold_reason
                )
            )
            and (
                not self.config["runtime"].get("task_interaction_v2")
                or self.result_speech_attempts.get(key, 0) < 2
            )
        )

    async def try_deliver(self):
        if (
            self.user_speaking
            or self.route_pending
            or self.muted
            or self.response_id
            or self.history_reset_pending
            or self.closed
        ):
            return
        if self.config["runtime"].get("conversation_v3"):
            from jarvis.core.conversation import start_fast

            pending_fast = [
                t
                for t in self.tasks.values()
                if t.execution_mode == "fast"
                and t.status == TaskStatus.QUEUED
                and (
                    not (
                        self.config["runtime"].get("playback_control_v2")
                        and t.delivery_hold_reason
                    )
                )
            ]
            ready_result = any((self.result_eligible(k) for k in self.results))
            if (
                pending_fast
                and (not ready_result)
                and start_fast(self, min(pending_fast, key=lambda t: t.created_at))
            ):
                return
        conversational = self.config["runtime"].get("conversational_delivery")
        if conversational:
            if self.delivery_inflight:
                return
            pending = any((self.result_eligible(k) for k in self.results))
            if not pending:
                return
            pause = self.config["runtime"].get("result_pause_ms", 1800)
            if (
                self.config["runtime"].get("conversation_v3")
                and self.last_spoken_purpose == "result"
            ):
                pause = self.config["runtime"].get("result_chain_pause_ms", 180)
            remaining = pause / 1000 - (time.monotonic() - self.quiet_since)
            if remaining > 0:
                self.wake_delivery(remaining)
                return
        entries = list(self.results.items())
        if self.config["runtime"].get("task_interaction_v2"):
            priority = {key: i for i, key in enumerate(self.requested_results)}
            entries.sort(
                key=lambda item: (
                    priority.get(item[0], len(priority)),
                    self.tasks.get(item[0][0]).created_at
                    if item[0][0] in self.tasks
                    else 0,
                )
            )
            if self.config["runtime"].get("conversation_v3"):
                entries.sort(
                    key=lambda item: (
                        priority.get(item[0], len(priority)),
                        0
                        if item[0] in priority
                        else self.result_speech_attempts.get(item[0], 0),
                        self.tasks[item[0][0]].completed_at
                        if item[0][0] in self.tasks
                        else 0,
                    )
                )
        for key, result in entries:
            if not self.result_eligible(key):
                continue
            task = (
                self.delivery_task(key) if conversational else self.current_task(*key)
            )
            if (
                not task
                or task.provisional
                or key in self.committed
                or (key in self.delivery_inflight)
                or (key in self.delivery_retry_scheduled)
                or (
                    self.delivery_attempts.get(key, 0)
                    >= self.config["runtime"].get("delivery_max_attempts", 2)
                )
            ):
                continue
            if (
                self.config["runtime"].get("task_interaction_v2")
                and self.result_speech_attempts.get(key, 0) >= 2
            ):
                continue
            self.delivery_inflight.add(key)
            if self.config["runtime"].get("task_orchestration"):
                await self.apply_delivery(key, "DELIVER", focus_epoch=self.focus_epoch)
                return
            self.delivery_attempts[key] = self.delivery_attempts.get(key, 0) + 1
            context = self.context()
            context["delivery_attempt"] = self.delivery_attempts[key]

            async def judge(k=key, r=result, ctx=context, epoch=self.focus_epoch):
                choice = await self.controller.delivery(r.model_dump(mode="json"), ctx)
                self.post(
                    "delivery.ready", {"key": k, "choice": choice, "focus_epoch": epoch}
                )

            self.spawn(judge())
            if conversational:
                return
        if conversational:
            return
        if not self.delivery_inflight:
            for key in tuple(self.ready_hints_pending):
                self.ready_hints_pending.discard(key)
                task = self.current_task(*key)
                if (
                    task
                    and (not task.provisional)
                    and (key not in self.committed)
                    and (key not in self.ready_hints)
                ):
                    self.ready_hints.add(key)
                    self.speak("结果准备好了。", "progress")
                    self.emit("task.ready_hint", task_id=key[0], version=key[1])
                    break

    async def apply_delivery(self, key, choice, focus_epoch=None):
        self.delivery_inflight.discard(key)
        conversational = self.config["runtime"].get("conversational_delivery")
        task = self.delivery_task(key) if conversational else self.current_task(*key)
        if not task or not self.result_eligible(key):
            return
        if conversational and (
            focus_epoch is not None
            and focus_epoch != self.focus_epoch
            or self.user_speaking
            or self.route_pending
            or self.response_id
        ):
            self.delivery_attempts[key] = max(0, self.delivery_attempts.get(key, 0) - 1)
            self.emit(
                "task.delivery_deferred",
                task_id=key[0],
                version=key[1],
                reason="conversation_changed",
            )
            await self.try_deliver()
            return
        if choice == "DISCARD":
            self.committed.add(key)
            self.emit("task.delivery_discarded", task_id=key[0], version=key[1])
        elif choice in {"DELIVER", "CLARIFY"} and (
            not (
                self.user_speaking
                or self.route_pending
                or self.muted
                or self.response_id
            )
        ):
            explicit_replay = (
                self.config["runtime"].get("intentional_delivery_interruptions")
                and key in self.requested_results
                and (self.result_speech_attempts.get(key, 0) == 0)
            )
            self.present(task, self.results[key], replay=explicit_replay)
        else:
            self.emit(
                "task.delivery_deferred", task_id=key[0], version=key[1], reason=choice
            )
            if self.delivery_attempts.get(key, 0) < self.config["runtime"].get(
                "delivery_max_attempts", 2
            ):
                self.delivery_retry_scheduled.add(key)

                async def retry():
                    await asyncio.sleep(
                        self.config["runtime"].get("delivery_retry_s", 3)
                    )
                    self.post("delivery.retry", {"key": key})

                self.spawn(retry())

    def present(self, task, result, replay=False):
        key = (task.task_id, task.version)
        if not replay and (
            key in self.committed
            or (
                self.config["runtime"].get("intentional_delivery_interruptions")
                and task.delivery_hold_reason
            )
        ):
            return
        text = result.spoken_answer
        v2 = self.config["runtime"].get("task_interaction_v2")
        if v2:
            from jarvis.core.task_delivery import announcement

            text = announcement(
                task,
                result,
                self.response_language,
                contextual=self.config["runtime"].get("conversation_v3", False),
            )
            if (
                self.config.get("native_routing", {}).get("delegated_continuity")
                and task.request_count == 1
                and (not result.artifacts)
            ):
                text = result.spoken_answer
        response = self.speak(text, "result", task=key)
        if response:
            if v2:
                if replay:
                    self.committed.discard(key)
                    self.result_speech_attempts[key] = 0
                task.delivery_hold_reason = None
                self.result_speech_attempts[key] = (
                    self.result_speech_attempts.get(key, 0) + 1
                )
                task.delivery_state = "playing"
                self.persist_task(task)
            else:
                self.committed.add(key)
            self.emit(
                "task.presentation.started",
                task_id=task.task_id,
                version=task.version,
                response_id=response,
                replay=replay,
            )

    def venus_step(self, message):
        pcm = message.pop("pcm", None)
        self.emit("venus.step", **message)
        for payload in message.get("delegates", []):
            self.emit(
                "frontend.delegate",
                payload=payload,
                utterance_id=self.current_utterance,
            )
            if self.config["runtime"].get("playback_control_v3"):
                self.emit(
                    "frontend.delegate_merged",
                    utterance_id=self.current_utterance,
                    reason="final_asr_owns_routing",
                )
                continue
            update = self.transcripts.get(self.current_utterance)
            if (
                update
                and update.is_final
                and (update.utterance_id not in self.logical_requests)
            ):
                self.start_route(update)
        if (
            not self.response_id
            or message["generation_epoch"] != self.generation_epoch
            or message["response_id"] != self.response_id
        ):
            if pcm:
                self.emit("assistant.audio.discarded", reason="stale_epoch_or_response")
            return
        presentation = self.presentations[self.response_id]
        if message.get("audio_complete") is False:
            presentation["audio_complete"] = False
        if (
            self.config["runtime"].get("conversational_delivery")
            and presentation["purpose"] == "progress"
            and presentation["task"]
            and (not presentation["generated_samples"])
        ):
            task = self.current_task(*presentation["task"])
            if not task or task.status not in {TaskStatus.RUNNING, TaskStatus.QUEUED}:
                self.clear_playback("stale_progress")
                self.spawn(self.try_deliver())
                return
        if message.get("text") and (not presentation.get("repair_active")):
            presentation["text"].append(message["text"])
        if pcm:
            samples = len(base64.b64decode(pcm)) // 4
            if presentation["purpose"] in {"ack", "progress"} and (
                not self.config["runtime"].get("conversational_delivery")
            ):
                budget = self.config["runtime"]["pending_audio_budget_ms"] * 24
                remaining = max(0, budget - presentation["generated_samples"])
                if samples > remaining:
                    pcm = base64.b64encode(
                        base64.b64decode(pcm)[: remaining * 4]
                    ).decode()
                    samples = remaining
                if samples < 1:
                    self.finish_generation(presentation)
                    return
            identifier = new_id()
            presentation["generated_samples"] += samples
            presentation["chunks"].append(identifier)
            self.chunks[identifier] = {
                "pcm": pcm,
                "samples": samples,
                "played": 0,
                "response_id": self.response_id,
                "epoch": self.generation_epoch,
            }
            self.emit(
                "assistant.audio.generated",
                generation_epoch=self.generation_epoch,
                chunk_id=identifier,
                response_id=self.response_id,
                samples=samples,
                sample_rate=24000,
                task_id=presentation["task"][0] if presentation["task"] else None,
                version=presentation["task"][1] if presentation["task"] else None,
            )
            self.send_client(
                {
                    "type": "audio",
                    "generation_epoch": self.generation_epoch,
                    "chunk_id": identifier,
                    "response_id": self.response_id,
                    "sample_rate": 24000,
                    "pcm": pcm,
                }
            )
        budget_done = (
            not self.config["runtime"].get("conversational_delivery")
            and presentation["purpose"] in {"ack", "progress"}
            and (
                presentation["generated_samples"]
                >= self.config["runtime"]["pending_audio_budget_ms"] * 24
            )
        )
        if message.get("end_of_turn") or budget_done:
            self.finish_generation(presentation)

    def finish_generation(self, presentation):
        if presentation.get("speech_check_pending") or presentation["generated_done"]:
            return
        self.send_venus(
            {
                "type": "control",
                "generation_epoch": self.generation_epoch,
                "hold": True,
                "end_response": True,
            }
        )
        phase = "tail" if presentation.get("repair_active") else "initial"
        check_flag = "repair_checked" if phase == "tail" else "speech_checked"
        if (
            self.speech_checker
            and (not presentation.get(check_flag))
            and presentation["chunks"]
            and presentation["text"]
        ):
            presentation[check_flag] = True
            presentation["speech_check_pending"] = True
            pcm = b"".join(
                (
                    base64.b64decode(self.chunks[k]["pcm"])
                    for k in presentation["chunks"]
                )
            )
            if phase == "tail":
                pcm = pcm[presentation["repair_start_samples"] * 4 :]
            expected = (
                presentation["repair_text"]
                if phase == "tail"
                else presentation.get("approved_text") or "".join(presentation["text"])
                if self.config["runtime"].get("task_dialogue")
                else "".join(presentation["text"])
            )

            async def check(
                rid=self.response_id,
                epoch=self.generation_epoch,
                language=presentation.get("language", self.response_language),
            ):
                result = await self.speech_checker.check(pcm, expected, language)
                self.post(
                    "speech.checked",
                    {
                        "response_id": rid,
                        "generation_epoch": epoch,
                        "phase": phase,
                        **result,
                    },
                )

            self.spawn(check())
            return
        if (
            presentation.get("repair_active")
            and presentation["generated_samples"]
            == presentation["repair_start_samples"]
        ):
            presentation["audio_complete"] = False
        presentation["generated_done"] = True
        self.finish_presentation(self.response_id)

    def playback_ack(self, ack):
        chunk = self.chunks.get(ack["chunk_id"])
        if not chunk or ack["generation_epoch"] != chunk["epoch"]:
            return
        samples = int(ack["played_samples"])
        if not chunk["played"] <= samples <= chunk["samples"]:
            self.emit("assistant.playback.invalid_ack", chunk_id=ack["chunk_id"])
            return
        chunk["played"] = samples
        self.emit("assistant.playback.progress", **ack)
        response = self.presentations[chunk["response_id"]]
        if ack.get("interrupted"):
            response["interrupted"] = True
        self.finish_presentation(chunk["response_id"])

    def finish_presentation(self, response_id):
        if response_id is None or response_id not in self.presentations:
            return
        response = self.presentations[response_id]
        if response["history_recorded"]:
            return
        chunks = [self.chunks[key] for key in response["chunks"]]
        audible = any((c["played"] for c in chunks))
        uncertain_receipt = self.config["runtime"].get("playback_control_v3") and any(
            (c["samples"] for c in chunks)
        )
        drained = response["generated_done"] and all(
            (c["played"] == c["samples"] for c in chunks)
        )
        complete = (
            drained
            and response.get("audio_complete", True)
            and (not response["interrupted"])
        )
        if not drained and (not response["interrupted"]):
            return
        if drained and (not response.get("audio_complete", True)):
            self.emit(
                "assistant.response.failed",
                response_id=response_id,
                reason="native_tts_incomplete",
            )
        if any((c["played"] for c in chunks)) and "".join(response["text"]).strip():
            text = (
                "".join(response["text"])
                if complete and (not response["interrupted"])
                else "[部分播出，具体字词未对齐]"
            )
            self.played.append(
                {"response_id": response_id, "text": text, "complete": complete}
            )
            self.history.append(
                {"role": "assistant", "text": text, "actually_played": True}
            )
            if self.config["runtime"].get("conversation_v3") and complete:
                from jarvis.core.conversation import remember_spoken, check_task_answer

                remember_spoken(self, response, text)
                if response["purpose"] == "fast_task":
                    task = self.delivery_task(response["task"])
                    if task:
                        task.progress["phase"] = "checking_answer"
                        self.spawn(
                            check_task_answer(self, response["task"], response_id, text)
                        )
            if (
                self.config["runtime"].get("task_interaction_v2")
                and self.config["runtime"].get("native_answer_repair", True)
                and (response["purpose"] == "fast")
                and (not response["authoritative_backend"])
                and complete
            ):
                source = self.transcripts.get(response.get("source_utterance_id"))
                if source and hasattr(self.controller, "judge"):
                    self.spawn(self.check_fast_answer(source, text))
            if (
                response["task"]
                and complete
                and (
                    not self.config["runtime"].get("task_dialogue")
                    or response["purpose"] == "result"
                )
            ):
                task = (
                    self.delivery_task(response["task"])
                    if self.config["runtime"].get("task_interaction_v2")
                    else self.current_task(*response["task"])
                )
                if task:
                    if task.status == TaskStatus.COMPLETED:
                        task.status = TaskStatus.DELIVERED
                    if self.config["runtime"].get("task_interaction_v2"):
                        self.committed.add(response["task"])
                        task.delivery_state = "delivered"
                        if response["task"] in self.requested_results:
                            self.requested_results.remove(response["task"])
                    self.persist_task(task)
                    self.emit(
                        "task.delivered",
                        task_id=task.task_id,
                        version=task.version,
                        response_id=response_id,
                    )
        elif complete and (not response["interrupted"]):
            self.emit(
                "assistant.response.failed",
                response_id=response_id,
                reason="empty_native_response",
            )
        response["history_recorded"] = True
        self.emit(
            "assistant.presentation.finished",
            response_id=response_id,
            complete=complete,
            interrupted=response["interrupted"],
        )
        key = response["task"]
        if (
            self.config["runtime"].get("conversation_v3")
            and response["purpose"] == "fast_task"
            and key
            and self.delivery_task(key)
        ):
            if not complete or not any((c["played"] for c in chunks)):
                task = self.delivery_task(key)
                if self.config["runtime"].get("playback_control_v2") and response.get(
                    "interruption_reason"
                ) in {
                    "confirmed_user_request",
                    "stop_button",
                    "semantic_stop_speaking",
                    "recognized_stop_command",
                    "user_dismissed",
                }:
                    self.hold_announcements(
                        task.delivery_hold_reason or "user_interrupted", key
                    )
                elif (
                    self.config["runtime"].get("no_audible_result_retry")
                    and (audible or uncertain_receipt)
                    and (not task.delivery_hold_reason)
                ):
                    self.hold_announcements(
                        "speech_incomplete_after_playback"
                        if audible
                        else "playback_receipt_uncertain",
                        key,
                    )
                exhausted = task.progress.get("native_attempts", 0) >= 2
                task.status = TaskStatus.FAILED if exhausted else TaskStatus.QUEUED
                task.delivery_state = (
                    "awaiting_user"
                    if exhausted or task.delivery_hold_reason
                    else "pending"
                )
                if exhausted:
                    result = TaskResult(
                        task_id=task.task_id,
                        version=task.version,
                        status="failed",
                        spoken_answer="That answer was interrupted. You can ask me to retry."
                        if task.response_language == "en"
                        else "这项回答没有完整播出，可以让我重说。",
                    )
                    self.results[key] = result
                    self.result_speech_attempts[key] = 2
                    self.emit(
                        "task.failed",
                        **result.model_dump(mode="json"),
                        reason="native_answer_incomplete",
                    )
                self.persist_task(task)
                self.emit(
                    "task.delivery_pending",
                    task_id=key[0],
                    version=key[1],
                    reason=task.delivery_hold_reason or "native_answer_interrupted",
                    automatic_retry=not (exhausted or task.delivery_hold_reason),
                )
        elif (
            self.config["runtime"].get("task_interaction_v2")
            and response["purpose"] == "result"
            and key
            and (key not in self.committed)
            and self.delivery_task(key)
        ):
            exhausted = self.result_speech_attempts.get(key, 0) >= 2
            task = self.delivery_task(key)
            intentional = (
                self.config["runtime"].get("intentional_delivery_interruptions")
                and response["purpose"] == "result"
                and (
                    response.get("interruption_reason")
                    in {
                        "confirmed_user_request",
                        "stop_button",
                        "semantic_stop_speaking",
                        "recognized_stop_command",
                    }
                )
            )
            if intentional:
                task.delivery_hold_reason = (
                    task.delivery_hold_reason or "user_interrupted"
                )
                task.delivery_held_at = time.time()
                if key in self.requested_results:
                    self.requested_results.remove(key)
            if (
                self.config["runtime"].get("no_audible_result_retry")
                and (audible or uncertain_receipt)
                and (not task.delivery_hold_reason)
            ):
                self.hold_announcements(
                    "speech_incomplete_after_playback"
                    if audible
                    else "playback_receipt_uncertain",
                    key,
                )
            task.delivery_state = (
                "awaiting_user" if exhausted or task.delivery_hold_reason else "pending"
            )
            self.persist_task(task)
            self.emit(
                "task.delivery_pending",
                task_id=key[0],
                version=key[1],
                reason=task.delivery_hold_reason
                or ("interrupted" if response["interrupted"] else "speech_incomplete"),
                automatic_retry=not (exhausted or task.delivery_hold_reason),
            )
        elif (
            self.config["runtime"].get("task_orchestration")
            and key
            and response["interrupted"]
            and (not self.muted)
            and (key not in self.interrupted_deliveries)
        ):
            self.interrupted_deliveries.add(key)
            self.committed.discard(key)
            self.emit(
                "task.delivery_requeued",
                task_id=key[0],
                version=key[1],
                reason="interrupted_before_complete",
                retry_limit=1,
            )
        if self.response_id == response_id:
            self.response_id = None
            self.quiet_since = time.monotonic()
            self.last_spoken_purpose = response["purpose"]
        self.spawn(self.try_deliver())

    async def check_fast_answer(self, source, answer):
        try:
            values, scores, _ = await self.controller.judge(
                "answer_coverage",
                {"question": source.text, "spoken_reply": answer},
                {
                    "coverage": {
                        "instructions": "Has the reply addressed the latest request? A relevant answer, greeting, acknowledgement of a stated personal fact, or a genuinely necessary clarification is ADDRESSED. Merely repeating a fully specified question or promising help without any answer is NONANSWER. Do not assess factual accuracy or demand long explanations.",
                        "criteria": {
                            "ADDRESSED": "Answers or appropriately responds to the user.",
                            "NONANSWER": "Only parrots the question, stalls or acknowledges an answerable request.",
                        },
                    }
                },
                allow_fallback=False,
            )
            if (
                values["coverage"] == "NONANSWER"
                and scores["coverage"] >= self.config["jev"]["control_accept"]
                and (source.utterance_id not in self.logical_requests)
                and (source.utterance_id in self.transcripts)
            ):
                if self.announcement_hold_for(source.utterance_id):
                    self.emit(
                        "answer.repair_discarded",
                        utterance_id=source.utterance_id,
                        reason="speech_stopped",
                    )
                    return
                if self.config["runtime"].get("conversation_v3"):
                    if source.utterance_id != self.current_utterance:
                        self.emit(
                            "answer.repair_discarded",
                            utterance_id=source.utterance_id,
                            reason="conversation_moved_on",
                        )
                        return
                    task = self.start_worker(source, execution_mode="fast")
                    task.progress["native_attempts"] = 1
                    self.emit(
                        "answer.native_retry",
                        task_id=task.task_id,
                        utterance_id=source.utterance_id,
                    )
                    await self.try_deliver()
                    return
                task = self.start_worker(source)
                task.expected_output = "answer"
                self.emit(
                    "answer.repair_started",
                    task_id=task.task_id,
                    utterance_id=source.utterance_id,
                )
        except Exception as error:
            self.emit("answer.coverage_unavailable", error=type(error).__name__)

    async def retrieve_memory(self, update, decision):
        selected = []
        prefetched = self.memory_prefetch.pop(update.utterance_id, [])
        for expansion in range(self.config["memory"]["max_expansions"] + 1):
            if expansion == 0 and prefetched:
                candidates = await self.memory.active_ids(
                    self.user_id, [m["id"] for m in prefetched]
                )
                self.emit(
                    "memory.prefetch.used",
                    utterance_id=update.utterance_id,
                    candidate_ids=[m["id"] for m in candidates],
                )
            else:
                candidates = await self.memory.search(
                    self.user_id,
                    update.text,
                    self.config["memory"]["candidate_k"],
                    expand=bool(expansion),
                )
            self.emit(
                "memory.candidates.ready", candidates=candidates, expansion=expansion
            )
            selected = await self.controller.select_memory(
                update.text, candidates, self.context()
            )
            if selected:
                break
        if self.current_utterance != update.utterance_id or self.user_speaking:
            return
        if selected:
            evidence = [
                {"text": m["text"], "source": m["id"], "updated_at": m["updated_at"]}
                for m in selected
            ]
            self.emit(
                "memory.selected", evidence=evidence, utterance_id=update.utterance_id
            )
            prefix = (
                "You previously told me: "
                if self.response_language == "en"
                and self.config["jev"].get("interactive_routing")
                else "你之前告诉过我："
            )
            self.deliver_memory_recall(
                update, selected, prefix + "；".join((m["text"] for m in selected))
            )
        else:
            self.deliver_memory_recall(
                update,
                [],
                "我还没有找到这方面的记录。你能再告诉我一下吗？",
                purpose="clarify",
            )

    async def close(self):
        self.closed = True
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(*list(self.background), return_exceptions=True)
        if self.memory_jobs:
            try:
                async with asyncio.timeout(30):
                    await asyncio.gather(
                        *list(self.memory_jobs), return_exceptions=True
                    )
            except TimeoutError:
                for task in list(self.memory_jobs):
                    task.cancel()
        await self.controller.close()
        await self.worker.close()
        if self.speech_checker:
            await self.speech_checker.close()

    def schedule_personal_memory(self, update, source_event_id):
        if (
            self.config["memory"].get("enabled")
            and getattr(self, "mode", None) != "voxel"
        ):
            self.spawn(
                self.memory.extract(
                    self.user_id, update.text, source_event_id, self.worker, self.emit
                ),
                memory=True,
            )

    def clarify_decision(self, update, decision):
        prompt = (
            "现在暂时无法处理，请稍后再试。"
            if decision.decision_origin == "terminal_fallback"
            else {
                "TARGET": "你指的是哪一个任务？",
                "CONSTRAINT": "你最在意的条件是什么？",
                "GOAL": "你想让我帮你完成什么？",
                "NONE": "能再具体说一下你的要求吗？",
                "PERSONAL_CONSTRAINT": "这可能与你之前告诉我的限制有冲突。你是在修改之前的信息吗？",
            }.get(decision.missing_slot, "还需要哪些条件？")
        )
        return self.speak(prompt, "clarify", source_utterance=update.utterance_id)

    def mutate_task(self, task, update, action):
        if action not in {"CANCEL", "REFINE", "REPLACE"}:
            raise ValueError("Unsupported task mutation")
        if self.delivery_task((task.task_id, task.version)) is not task:
            return None
        if action == "CANCEL":
            self.invalidate(task, TaskStatus.CANCELLED)
            return task
        old_result = self.results.get((task.task_id, task.version))
        self.invalidate(task, TaskStatus.SUPERSEDED)
        new = task.model_copy(
            deep=True,
            update={
                "version": task.version + 1,
                "status": TaskStatus.QUEUED,
                "objective": update.text
                if action == "REPLACE"
                else task.objective + "\n用户最新修改：" + update.text,
                "source_utterance_id": update.utterance_id,
                "provisional": False,
                "last_mentioned_at": time.time(),
                "progress": {},
                "observations": old_result.evidence
                + [{"task_id": task.task_id, "artifacts": old_result.artifacts}]
                if old_result
                else task.observations,
                "history": self.history[-6:],
                "memory": list(self.profile),
            },
        )
        self.emit(
            "task.updated",
            task_id=new.task_id,
            version=new.version,
            objective=new.objective,
        )
        result = self.start_worker(update, replacement=new)
        self.logical_requests[update.utterance_id] = new.task_id
        return result

    def deliver_memory_recall(
        self, update, selected, answer, *, purpose="memory", user_text=None
    ):
        return self.speak(
            answer, purpose, source_utterance=update.utterance_id, user_text=user_text
        )


def type_name(error):
    return type(error).__name__
