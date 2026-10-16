from __future__ import annotations
import asyncio
import copy
import time
from collections import deque
from jarvis.core.runtime import SessionRuntime
from jarvis.core.schemas import Task, TaskStatus, TranscriptUpdate, new_id
from . import coach
from .adapter import IDENTITY
from .memory import WorldMemory
from .episodes import Episodes


class VoxelMode:
    def __init__(self, owner):
        self.r = owner
        self.bridge = None
        self.origins = {}
        self.job = None
        self.proactive_job = None
        from .reasoning import ReadOnlyReasoning

        self.reasoning = ReadOnlyReasoning(owner.worker, owner.config, owner.emit)
        self.memory = WorldMemory(owner.memory, owner.user_id)
        self.previous = None
        self.last_proactive = 0.0
        self.pending = None
        self.last_advice = None
        self.advice_offered = None
        self.confirmation = None
        self.epoch = 0
        self.speech_epoch = 0
        self.unhandled_turns = {}
        self.speech_queue = deque(maxlen=8)
        self.speech_turn = 0
        self.last_spoken_at = 0.0
        self.episodes = Episodes()
        from .focus import Focus
        from .companion import Discoveries

        self.focus = Focus()
        self.discoveries = Discoveries()
        self.discovery_job = None
        self.reply_job = None
        self.building = None
        if getattr(owner, "config", {}).get("voxel", {}).get("cooperative_build"):
            from .building import Building

            self.building = Building(self)
        self.operations = None
        if getattr(owner, "config", {}).get("voxel", {}).get("survival_tasks"):
            from .operations import Operations

            self.operations = Operations(self)

    async def activate(self):
        self.bridge = self.r.registry.voxel
        self.epoch += 1
        self.speech_epoch += 1
        self.pending = None
        self.previous = None
        self.last_advice = None
        self.confirmation = None
        self.unhandled_turns.clear()
        self.origins.clear()
        self.episodes.reset()
        self.focus.offers.clear()
        self.discoveries.reset()
        return self.bridge.attach(self.r)

    async def deactivate(self):
        if self.operations:
            self.operations.stop("game_changed")
        if self.building:
            self.building.stop("game_changed")
        self.epoch += 1
        self.pending = None
        self.confirmation = None
        self.unhandled_turns.clear()
        self.origins.clear()
        self.speech_queue.clear()
        self.speech_turn += 1
        if self.job:
            self.job.cancel()
            self.job = None
        if self.proactive_job:
            self.proactive_job.cancel()
            self.proactive_job = None
        if self.discovery_job:
            self.discovery_job.cancel()
            self.discovery_job = None
        if self.reply_job:
            self.reply_job.cancel()
            self.reply_job = None
        if self.bridge:
            self.bridge.detach(self.r)
            await self.bridge.display.release()

    def binding(self, scope="game", s=None, field=None):
        s = s or (self.bridge.snapshot if self.bridge else None)
        if not s:
            return None
        b = {
            "voxel": True,
            "scope": scope,
            "epoch": self.epoch,
            "session_id": self.r.session_id,
            **{k: s[k] for k in IDENTITY},
            "expires": time.monotonic() + 10,
        }
        if field:
            b["field"] = field
            b["value"] = copy.deepcopy(s["view"]["self"].get(field))
        if scope == "advice":
            b["day_phase"] = s["view"]["environment"].get("day_phase")
            b["hp"] = s["view"]["self"].get("hp")
            b["hunger"] = s["view"]["self"].get("hunger")
            b["goal"] = self.memory.goal
            b["inventory"] = copy.deepcopy(s["view"]["self"].get("inventory_summary"))
        return b

    def valid(self, b):
        s = self.bridge.snapshot if self.bridge else None
        if not b:
            return True
        if b.get("scope") == "spoken":
            return bool(
                self.r.mode == "voxel"
                and b.get("epoch") == self.epoch
                and s
                and all(
                    (
                        b.get(k) == s[k]
                        for k in ("world_id", "game_instance_id", "player_life_id")
                    )
                )
            )
        if (
            self.r.mode != "voxel"
            or b.get("epoch") != self.epoch
            or (not s)
            or any((b.get(k) != s[k] for k in IDENTITY))
        ):
            return False
        if b.get("project_generation") is not None and (
            not self.building or b["project_generation"] != self.building.generation
        ):
            return False
        if b["scope"] in {"history", "receipt", "analysis", "game"}:
            return True
        if time.monotonic() > b["expires"] or not self.bridge.fresh():
            return False
        if b["scope"] == "reminder":
            return self.episodes.still_relevant(b.get("evidence", []))
        if b.get("field"):
            return b["value"] == s["view"]["self"].get(b["field"])
        if b["scope"] == "advice":
            return (
                b["day_phase"] == s["view"]["environment"].get("day_phase")
                and b["hp"] == s["view"]["self"].get("hp")
                and (b["hunger"] == s["view"]["self"].get("hunger"))
                and (b["goal"] == self.memory.goal)
                and (b["inventory"] == s["view"]["self"].get("inventory_summary"))
            )
        return True

    def capture(self, update):
        if update.utterance_id not in self.origins:
            self.origins[update.utterance_id] = self.bridge.origin(update.utterance_id)

    def start_route(self, update):
        self.speech_turn += 1
        self.speech_queue.clear()
        self.unhandled_turns[update.utterance_id] = {
            "update": update,
            "at": time.monotonic(),
        }
        if self.memory.world and getattr(self.r, "config", {}).get("memory", {}).get(
            "enabled"
        ):

            async def remember():
                try:
                    await self.memory.remember_request(
                        self.r.controller, update.text, update.utterance_id
                    )
                except Exception as error:
                    self.r.emit("voxel.memory.failed", error=type(error).__name__)

            self.r.spawn(remember())
        self.unhandled_turns = {
            k: v
            for k, v in self.unhandled_turns.items()
            if k == update.utterance_id
            or (
                not getattr(self.r, "text_mode", False)
                and time.monotonic() - v["at"] < 2
            )
        }
        turns = list(self.unhandled_turns.values())[-6:]
        update = update.model_copy(
            update={"text": "\n".join((v["update"].text for v in turns))}
        )
        if self.job:
            self.job.cancel()
        if self.proactive_job:
            self.proactive_job.cancel()
            self.proactive_job = None
        if self.discovery_job:
            self.discovery_job.cancel()
            self.discovery_job = None
        if self.reply_job:
            self.reply_job.cancel()
            self.reply_job = None
        origin = self.origins.pop(update.utterance_id, None)
        s = copy.deepcopy(origin["snapshot"] if origin else self.bridge.snapshot)
        epoch = self.epoch
        prior = copy.deepcopy(self.last_advice)
        analysis_epoch = self.r.analysis_epoch
        if self.confirmation and (
            time.monotonic() > self.confirmation["expires"]
            or not self.bridge.current(self.confirmation["origin"])
        ):
            self.confirmation = None

        async def judge_route():
            if (
                self.operations
                and s
                and s.get("capabilities", {}).get("survival_tasks")
            ):
                from .focus import resolve
                from .rules import answer_rule

                intent = asyncio.create_task(resolve(self, update.text, s))
                facts = asyncio.create_task(
                    answer_rule(self.r.controller, update.text, s, self.r.history)
                )
                try:
                    done, _ = await asyncio.wait(
                        {intent, facts}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if facts in done:
                        try:
                            answer = facts.result()
                        except Exception:
                            answer = None
                        if answer:
                            return ("voxel.knowledge_route", answer)
                    turn = await intent
                    if turn["act"] == "EXPLAIN":
                        try:
                            answer = await facts
                        except Exception:
                            answer = None
                        if answer:
                            return ("voxel.knowledge_route", answer)
                finally:
                    for job in (intent, facts):
                        if not job.done():
                            job.cancel()
                    await asyncio.gather(intent, facts, return_exceptions=True)
                if turn["act"] != "OTHER":
                    return ("voxel.focus_route", turn)
            result = await coach.interpret(
                self.r.controller,
                update.text,
                s,
                self.r.history,
                self.memory.goal,
                self.memory.landmarks,
                prior,
                self.confirmation["text"] if self.confirmation else None,
                project=self.building.project if self.building else None,
                cooperative=bool(self.building),
            )
            return ("voxel.route", result)

        async def resolve():
            envelope = {
                "update": update,
                "origin": origin,
                "snapshot": s,
                "epoch": epoch,
                "prior": prior,
                "analysis_epoch": analysis_epoch,
            }
            for attempt in range(2):
                try:
                    kind, result = await judge_route()
                    self.r.post(kind, {**envelope, "result": result})
                    return
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    if (
                        not attempt
                        and epoch == self.epoch
                        and (update.utterance_id == self.r.current_utterance)
                    ):
                        self.r.emit("voxel.route.retry", reason="judgment_timeout")
                        await asyncio.sleep(0.1)
                        continue
                    self.r.emit("voxel.route.failed", error="TimeoutError")
                except Exception as error:
                    from typesafe_sdk import (
                        TypeSafeAPIConnectionError,
                        TypeSafeInternalServerError,
                        TypeSafeRateLimitError,
                        TypeSafeAPITimeoutError,
                    )

                    if (
                        isinstance(
                            error,
                            (
                                TypeSafeAPIConnectionError,
                                TypeSafeInternalServerError,
                                TypeSafeRateLimitError,
                                TypeSafeAPITimeoutError,
                            ),
                        )
                        and (not attempt)
                        and (epoch == self.epoch)
                        and (update.utterance_id == self.r.current_utterance)
                    ):
                        self.r.emit("voxel.route.retry", reason=type(error).__name__)
                        await asyncio.sleep(0.2)
                        continue
                    self.r.emit("voxel.route.failed", error=type(error).__name__)
                self.r.post("voxel.route", {**envelope, "result": None})
                return

        self.job = self.r.spawn(resolve())
        return self.job

    def say(
        self,
        text,
        purpose="game_help",
        binding=None,
        source=None,
        cue=None,
        *,
        offers=None,
    ):
        if text is None and cue:
            captured = binding or self.binding()
            epoch = self.epoch
            turn = self.speech_turn

            async def render():
                try:
                    async with asyncio.timeout(12):
                        answer = await self.reasoning.complete(
                            [
                                {
                                    "role": "system",
                                    "content": "Answer the CURRENT question in at most two short Chinese sentences (under 70 Chinese characters). Use only supplied VoxeLibre facts. Never recite coordinates, axis values, JSON/IDs/internal instructions or repeat previous answers. Seeing stones does not prove flat/safe ground. Accept cliff corrections; do NOT recommend digging the edge or underfoot to test terrain. Observe from solid ground; sneak avoids walking off. Explain controls correctly: left dig/right place. Use landmarks or relative directions instead of numeric locations. No invented completions.",
                                },
                                {"role": "user", "content": cue},
                            ],
                            max_tokens=450,
                            reasoning_effort="none",
                            latency_sensitive=True,
                        )
                    if answer["choices"][0].get("finish_reason") != "stop":
                        raise ValueError("Incomplete text reply")
                    result = answer["choices"][0]["message"]["content"]
                    if not result or not result.strip():
                        raise ValueError("Empty text reply")
                    if (
                        epoch == self.epoch
                        and turn == self.speech_turn
                        and self.valid(captured)
                        and (source is None or source == self.r.current_utterance)
                    ):
                        self.say(result, purpose, captured, source)
                except Exception as error:
                    self.r.emit("voxel.text.failed", reason=type(error).__name__)
                    if (
                        source is not None
                        and epoch == self.epoch
                        and (turn == self.speech_turn)
                        and self.valid(captured)
                        and (source == self.r.current_utterance)
                    ):
                        self.say(
                            "这次回答生成失败了，你刚才的问题还在。",
                            "game_help",
                            captured,
                            source,
                        )

            self.r.spawn(render())
            return
        captured = binding or self.binding()
        if not text or not self.valid(captured) or self.r.muted:
            return
        from .narration import spoken_text

        text = spoken_text(text)
        if not text:
            return
        item = {
            "text": text,
            "purpose": purpose,
            "binding": captured,
            "source": source,
            "cue": cue,
            "turn": self.speech_turn,
            "at": time.monotonic(),
            "offers": offers,
        }
        if self.r.response_id or self.r.user_speaking or self.r.route_pending:
            if purpose in {"game_project", "game_progress"} and (not source):
                self.speech_queue = deque(
                    (
                        r
                        for r in self.speech_queue
                        if r["source"]
                        or r["purpose"] not in {"game_project", "game_progress"}
                    ),
                    maxlen=8,
                )
            self.speech_queue.append(item)
            self.r.emit(
                "voxel.speech.queued", text=text, purpose=purpose, source=source
            )
            return
        return self.deliver_speech(item)

    def deliver_speech(self, item):
        if (
            item["turn"] != self.speech_turn
            or not self.valid(item["binding"])
            or time.monotonic() - item["at"] > 45
        ):
            return
        if item["source"] and item["source"] != self.r.current_utterance:
            return
        bound = {
            k: v
            for k, v in item["binding"].items()
            if k not in {"expires", "field", "value", "project_generation", "evidence"}
        }
        bound["scope"] = "spoken"
        bound["expires"] = (
            time.monotonic()
            + self.r.config["runtime"].get("response_timeout_s", 30)
            + 10
        )
        rid = self.r.speak_game(
            item["text"],
            item["purpose"],
            bound,
            source_utterance=item["source"],
            user_text=item["cue"],
        )
        self.last_spoken_at = time.monotonic()
        self.focus.present(
            item["text"], item.get("offers"), bound.get("world_id"), item["source"]
        )
        if item["purpose"] in {"game_help", "game_advice", "fast"}:
            self.focus.discuss(item["text"], bound.get("world_id"), item["source"])
        self.r.emit(
            "voxel.speech.presented",
            text=item["text"],
            purpose=item["purpose"],
            response_id=rid,
            source=item["source"],
        )
        return rid

    async def apply_focus(self, p):
        r = self.r
        u = p["update"]
        turn = p["result"]
        origin = p["origin"]
        s = self.bridge.snapshot
        if p["epoch"] != self.epoch or u.utterance_id != r.current_utterance:
            return
        r.route_pending = False
        self.unhandled_turns.clear()
        if not s or any(
            (
                p["snapshot"].get(k) != s.get(k)
                for k in ("world_id", "game_instance_id", "player_life_id")
            )
        ):
            return
        origin = self.bridge.synced_origin(origin) or origin
        if r.paused_response:
            r.clear_playback("confirmed_user_request")
        act = turn["act"]
        record = turn.get("record") or {}
        owner = record.get("owner")
        if act == "CONTACT":
            self.say("能听到，你刚才的话已经收到了。", source=u.utterance_id)
            return
        if act == "CONNECTION":
            text = (
                "游戏客户端正在恢复，存档和对话都保留着；画面恢复后就能继续。"
                if self.bridge.screen_status() != "ready"
                else "游戏画面已经连上了，可以点击进入游戏或旁观。"
            )
            self.say(text, source=u.utterance_id)
            return
        if act == "IGNORE":
            r.resume_playback()
            return
        if act == "SILENCE":
            await self.handle("stop_speaking", {})
            await SessionRuntime.handle(r, "stop_speaking", {})
            return
        if act == "RECLAIM":
            self.operations.stop("semantic_reclaim")
            if self.building:
                self.building.stop("semantic_reclaim")
                await self.building.persist(u.utterance_id)
            if self.bridge.input_mode == "watch":
                self.bridge.revoke("semantic_reclaim")
            self.operations.emit()
            await self.operations.save(u.utterance_id)
            self.say("好的，接下来你自己操作。", source=u.utterance_id)
            return
        if act == "CANCEL":
            target = turn["reference"]
            if turn.get("cancellation_unconfirmed"):
                title = (
                    record.get("title")
                    or record.get("objective")
                    or record.get("request")
                    or "当前任务"
                )
                self.say(
                    "刚才那句没听清，是想取消“" + title[:45] + "”吗？",
                    source=u.utterance_id,
                )
                return
            if target not in {"TASK", "PROJECT", "ALL"} and owner != "PROJECT":
                self.say("你想取消哪一项？告诉我任务名就可以。", source=u.utterance_id)
                return
            await self.cancel_goal(target, u.utterance_id, record=record)
            return
        if act == "STATUS":
            if turn["preview"]:
                self.operations.stop("preview_only")
                if self.building:
                    self.building.stop("preview_only")
            if record.get("historical"):
                self.respond_companion(u.text, u.utterance_id)
                return
            if turn.get("reference") == "ALL":
                project_text = (
                    self.building.status_text()
                    if self.building and self.building.project
                    else ""
                )
                task_text = (
                    self.operations.status_text()
                    if self.operations and self.operations.task
                    else ""
                )
                self.say(
                    " ".join((t for t in (project_text, task_text) if t))
                    or "当前没有正在执行的任务。",
                    source=u.utterance_id,
                )
                return
            if (
                owner == "PROJECT"
                and self.building
                and self.building.project
                and (self.building.project.project_id == record["id"])
            ):
                text = self.building.status_text()
            elif (
                owner == "PROJECT"
                and self.building
                and (record["id"] in self.building.project_history)
            ):
                text = self.building.status_text(
                    self.building.project_history[record["id"]]
                )
            elif (
                owner == "TASK"
                and self.operations.task
                and (self.operations.task["id"] == record["id"])
            ):
                text = self.operations.status_text()
            else:
                records, current = self.focus.catalog(self)
                text = (
                    self.building.status_text()
                    if current == "PROJECT"
                    else self.operations.status_text()
                )
            self.say(text, source=u.utterance_id)
            return
        if act in {"ADVICE", "EXPLAIN"} or (
            turn["preview"] and turn["executor"] != "BUILD"
        ):
            if s.get("phase") == "disconnected":
                self.say(
                    "游戏客户端暂时断开，正在恢复画面；你的存档和刚才的任务都保留着。",
                    source=u.utterance_id,
                )
                return
            self.respond_companion(
                u.text,
                u.utterance_id,
                information=act == "EXPLAIN",
                advice=act == "ADVICE",
            )
            return
        delegates = act in {"EXECUTE", "CONTINUE", "REVISE"} and (not turn["preview"])
        delegates |= bool(
            act == "DESIGN"
            and self.building
            and self.building.project
            and self.building.project.execution_intent
            and (not turn["preview"])
        )
        if delegates and self.bridge.input_mode == "look":
            origin = await self.bridge.takeover(origin)
            if u.utterance_id != r.current_utterance or p["epoch"] != self.epoch:
                return
            if not origin:
                self.say(
                    "刚才接管时操作状态发生了变化，要求已经保留；停稳后说继续就能接着做。",
                    source=u.utterance_id,
                )
                return
            s = self.bridge.snapshot
        if (
            act == "DESIGN"
            or turn["executor"] == "BUILD"
            or (owner == "PROJECT" and act in {"CONTINUE", "REVISE"})
        ):
            if not self.building:
                return
            if (
                owner == "PROJECT"
                and record.get("id") in self.building.project_history
                and (
                    not self.building.project
                    or self.building.project.project_id != record["id"]
                )
            ):
                self.building.stop("switch_project")
                if self.building.project:
                    await self.building.persist(u.utterance_id)
                self.building.project = copy.deepcopy(
                    self.building.project_history[record["id"]]
                )
                self.building.project.authorization = None
            project = self.building.project
            same = bool(
                project
                and owner == "PROJECT"
                and (project.project_id == record.get("id"))
            )
            route = (
                "PROJECT_CHANGE"
                if act in {"DESIGN", "REVISE"} and same
                else "PROJECT_START"
                if same
                else "PROJECT_NEW"
            )
            authorized = act in {"EXECUTE", "CONTINUE", "REVISE"} and (
                not turn["preview"]
            )
            if (
                act == "DESIGN"
                and same
                and project.execution_intent
                and (not turn["preview"])
            ):
                authorized = True
            current = bool(origin and self.bridge.current(origin))
            self.operations.stop("building_request")
            if current:
                origin = self.bridge.origin(u.utterance_id)
            decision = {
                "route": {"choice": route, "confidence": 1},
                "project_authorize": {"noul": float(authorized and current)},
                "delegate_design": {"noul": float(turn["delegate_design"])},
                "project_update": {"noul": float(route == "PROJECT_CHANGE")},
                "keep_original_request": {
                    "noul": float(same and (not turn["preview"]))
                },
                "project_hold": {"noul": float(turn["preview"])},
                "supply": {"noul": 1},
                "duration_ms": turn["duration_ms"],
            }
            await self.building.apply_intent(u, decision, origin, s)
            if self.building.project:
                self.focus.activate("PROJECT", self.building.project.project_id)
            return
        if act == "CONTINUE" and (not record):
            self.say(
                "刚才没有确定要接着做哪件事，告诉我目标就可以。", source=u.utterance_id
            )
            return
        if not self.bridge.fresh():
            self.say(
                "游戏画面暂时没有更新，刚才的要求还在；连接恢复后可以继续。",
                source=u.utterance_id,
            )
            return
        from .focus import operation_decision

        decision = operation_decision(turn)
        if record.get("historical") and act == "CONTINUE":
            decision["effective_route"] = record.get("kind") or turn["executor"]
            u = u.model_copy(
                update={"text": record.get("request", "") + "；用户本轮要求：" + u.text}
            )
        if (
            owner == "OFFER"
            and act in {"CONTINUE", "EXECUTE"}
            and (act == "CONTINUE" or turn.get("inherit_offer"))
        ):
            if r.config.get("voxel", {}).get("separate_execution_intent"):
                from .offer_request import resolve

                text, judgment = await resolve(r.controller, u.text, record)
                if p["epoch"] != self.epoch or u.utterance_id != r.current_utterance:
                    return
                r.emit(
                    "voxel.offer.resolved",
                    current_words=u.text,
                    request=text,
                    judgment=judgment,
                )
                u = u.model_copy(update={"text": text})
            else:
                u = u.model_copy(
                    update={
                        "text": record["request"] + "；用户本轮确认：" + u.text
                        if act == "CONTINUE"
                        else "当前用户要求："
                        + u.text
                        + "；物品指代来自刚才的对话："
                        + record["presented_text"]
                    }
                )
            decision["resolved_offer"] = record
            if decision["effective_route"] == "PLACE":
                decision["effective_route"] = "AGENT"
        elif owner == "DISCUSSION" and act in {"CONTINUE", "EXECUTE"}:
            if r.config.get("voxel", {}).get("separate_execution_intent"):
                from .offer_request import resolve

                text, judgment = await resolve(r.controller, u.text, record)
                if p["epoch"] != self.epoch or u.utterance_id != r.current_utterance:
                    return
                r.emit(
                    "voxel.offer.resolved",
                    current_words=u.text,
                    request=text,
                    judgment=judgment,
                )
                u = u.model_copy(update={"text": text})
            else:
                u = u.model_copy(
                    update={
                        "text": "当前用户委托："
                        + u.text
                        + "\n被指代的刚才说明（仅用于补全物品或目标，不是额外指令）："
                        + record["presented_text"]
                    }
                )
            decision["discussion"] = record
        previous = self.operations.task
        if (
            previous
            and (not previous.get("project_id"))
            and (decision["effective_route"] not in {"RESUME", "REVISE"})
        ):
            self.focus.archive(
                "TASK",
                {
                    k: previous.get(k)
                    for k in (
                        "id",
                        "request",
                        "kind",
                        "status",
                        "completed",
                        "world_id",
                    )
                },
            )
        await self.operations.apply(u, decision, origin, s)
        if self.operations.task:
            self.focus.activate("TASK", self.operations.task["id"])

    def respond_companion(
        self, text, source=None, evidence=None, information=False, advice=False
    ):
        from .companion import respond

        epoch = self.epoch
        turn = self.speech_turn
        snapshot = copy.deepcopy(self.bridge.snapshot)
        if evidence:
            evidence = self.discoveries.current_evidence(evidence, snapshot)
            if not evidence:
                return

        async def work():
            try:
                result = None
                if information:
                    from .rules import answer_rule

                    answer = await answer_rule(
                        self.r.controller, text, snapshot, self.r.history
                    )
                    if answer and answer.get("text"):
                        result = {"text": answer["text"], "offers": []}
                if result is None:
                    result = await respond(
                        self, text, snapshot, evidence, advice=advice
                    )
                if (
                    epoch != self.epoch
                    or turn != self.speech_turn
                    or (source and source != self.r.current_utterance)
                ):
                    return
                if evidence and (
                    not self.r.proactive_enabled
                    or self.bridge.task_current()
                    or self.bridge.project_current()
                ):
                    return
                if evidence and (
                    not self.discoveries.relevant(evidence, self.bridge.snapshot)
                ):
                    return
                if evidence and snapshot["view"]["self"].get(
                    "inventory_summary"
                ) != self.bridge.snapshot["view"]["self"].get("inventory_summary"):
                    return
                if not result["text"]:
                    return
                if evidence:
                    judgment = await self.r.controller.judge_typed(
                        "voxel_notice_importance",
                        {
                            "candidate": result["text"],
                            "observed_evidence": evidence,
                            "player": snapshot["view"]["self"],
                            "goal": self.memory.goal,
                            "recent_speech": self.r.history[-4:],
                        },
                        {
                            "important": {
                                "type": "noul",
                                "instructions": "Would this NEW unsolicited message convey important information worth interrupting quiet play? Actual immediate danger, a newly discovered needed resource or a major useful opportunity for the current goal qualify. Ordinary block definitions, scenery descriptions, repetitive advice and routine inventory changes do not. User prefers important information only. Explicit user questions are handled separately and must still be answered.",
                            }
                        },
                        deadline_ms=1500,
                    )
                    if judgment["important"]["noul"] < 0.8:
                        return
                    if (
                        epoch != self.epoch
                        or turn != self.speech_turn
                        or self.bridge.task_current()
                        or self.bridge.project_current()
                    ):
                        return
                self.r.emit(
                    "voxel.companion.reply",
                    text=result["text"],
                    offers=result["offers"],
                    evidence=evidence,
                    source=source,
                )
                self.say(
                    result["text"], "game_help", source=source, offers=result["offers"]
                )
                if (
                    evidence
                    and self.memory.world
                    and self.r.config.get("memory", {}).get("enabled")
                ):
                    ids = list(
                        dict.fromkeys(
                            (
                                r.get("item_id") or r.get("entity_kind")
                                for r in evidence
                                if r.get("item_id") or r.get("entity_kind")
                            )
                        )
                    )
                    await self.memory.save(
                        "discovery",
                        "已介绍的发现：" + result["text"][:450],
                        source or "observed_discovery",
                        snapshot["view"]["self"].get("position"),
                        provenance="native_observation",
                        evidence_ids=ids,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.r.emit(
                    "voxel.companion.failed",
                    reason=type(error).__name__,
                    detail=str(error)[:180],
                )
                if source and source == self.r.current_utterance:
                    self.say("刚才的回答生成失败了，我还记着你的问题。", source=source)

        job = self.r.spawn(work())
        if evidence:
            self.discovery_job = job
        else:
            self.reply_job = job
        return job

    async def apply(self, p):
        r = self.r
        u = p["update"]
        a = p["result"]
        s = p["snapshot"]
        origin = p["origin"]
        if p["epoch"] != self.epoch or u.utterance_id != r.current_utterance:
            return
        r.route_pending = False
        if a is None:
            self.say(
                "判断服务这次没有及时返回，刚才的要求已保留，暂时由你操作。",
                source=u.utterance_id,
            )
            return
        self.unhandled_turns.clear()
        route = a["route"]["choice"]
        confidence = a["route"]["confidence"]
        design_update = bool(
            self.building and a.get("project_update", {}).get("noul", 0) >= 0.8
        )
        if a["reclaim"]["noul"] >= r.config["jev"]["control_accept"]:
            if design_update:
                self.building.stop("hold_and_revise")
                a["project_authorize"] = {"noul": 0}
                a["keep_original_request"] = {"noul": 0}
                a["route"] = {**a["route"], "choice": "PROJECT_CHANGE"}
                route = "PROJECT_CHANGE"
            else:
                route = "RECLAIM"
        r.emit(
            "voxel.route.applied",
            route=route,
            confidence=confidence,
            provider="jev",
            utterance_id=u.utterance_id,
        )
        if route == "ASSISTANT":
            if r.config["memory"].get("enabled"):
                source = next(
                    (
                        h.get("source_event_id")
                        for h in reversed(r.history)
                        if h.get("role") == "user" and h.get("text") == u.text
                    ),
                    u.utterance_id,
                )
                r.spawn(
                    r.memory.extract(
                        r.user_id, u.text, source or u.utterance_id, r.worker, r.emit
                    ),
                    memory=True,
                )
            SessionRuntime.start_route(r, u)
            return
        if route == "IGNORE":
            r.resume_playback()
            return
        if r.paused_response:
            r.clear_playback("confirmed_user_request")
        if route == "STOP":
            await self.handle("stop_speaking", {})
            await SessionRuntime.handle(r, "stop_speaking", {})
            return
        if route == "RECLAIM":
            if self.operations:
                self.operations.stop("semantic_reclaim")
                self.operations.emit()
                await self.operations.save(u.utterance_id)
            if self.building:
                self.building.stop("semantic_reclaim")
                self.building.emit()
                await self.building.persist(u.utterance_id)
            self.bridge.revoke("semantic_reclaim")
            self.confirmation = None
            self.say("好的，接下来你自己操作。")
            return
        if route == "CANCEL_ANALYSIS":
            r.cancel_analysis()
            self.say("已取消分析，你可以继续玩。")
            return
        if not s or not self.bridge.fresh():
            r.speak(
                "游戏状态暂时没连接上，等画面恢复后再问我。",
                "confirmation",
                source_utterance=u.utterance_id,
            )
            return
        if any((s[k] != self.bridge.snapshot[k] for k in IDENTITY)):
            r.emit(
                "voxel.route.discarded",
                reason="source_world_or_life_changed",
                utterance_id=u.utterance_id,
            )
            return
        if self.memory.world != s["world_id"]:
            await self.memory.load(s["world_id"])
        if route.startswith("PROJECT_") and self.building:
            current = bool(origin and self.bridge.current(origin))
            if self.operations:
                self.operations.stop("building_request")
            if current:
                origin = self.bridge.origin(u.utterance_id)
            if origin and origin["generation"] != self.bridge.generation:
                if route == "PROJECT_START":
                    self.say("刚才控制状态已变化，建造暂未开始；你可以让我继续。")
                    return
                a = {
                    **a,
                    "project_authorize": {"noul": 0},
                    "keep_original_request": {"noul": 0},
                }
            await self.building.apply_intent(u, a, origin, s)
            return
        if route == "PLAY":
            allowed = (
                a["authorize"]["noul"] >= 0.9
                and confidence >= r.config["jev"]["control_accept"]
                and (a["action"]["confidence"] >= r.config["jev"]["control_accept"])
            )
            action = a["action"]["choice"]
            if allowed and action != "ABSTAIN":
                self.confirmation = None
                action_epoch = self.epoch
                receipt_binding = self.binding("receipt", s)

                async def execute():
                    result = await self.bridge.execute(origin, action)
                    r.post(
                        "voxel.action_result",
                        {
                            "result": result,
                            "binding": receipt_binding,
                            "epoch": action_epoch,
                        },
                    )

                r.spawn(execute())
                return
            if a["authorize"]["noul"] < 0.4:
                self.say(
                    "当前只能帮你在准星这里放一块手持泥土，其他操作需要你自己完成。"
                )
                return
            if not s["action_candidates"]:
                self.say(
                    "现在还不能代放。请手持泥土，停稳并瞄准身旁空格的方块面；其他操作先由你来。"
                )
                return
            self.confirmation = {
                "text": "在当前瞄准面放一块当前手持泥土，完成后立刻交还。",
                "origin": origin,
                "expires": time.monotonic() + 15,
            }
            self.say("你是想让我在准星这里放一块手持泥土吗？")
            return
        if a.get("goal_explicit", {}).get("noul", 0) >= 0.9:
            await self.memory.save("goal", u.text, u.utterance_id)
            r.emit("voxel.goal", text=self.memory.goal, world_id=s["world_id"])
        if route == "GOAL":
            self.say(
                "记住了，这个世界里我会按你刚才的目标陪你玩。"
                if a.get("goal_explicit", {}).get("noul", 0) >= 0.9
                else "你是想把刚才说的作为当前目标吗？"
            )
            return
        if route == "MARK":
            if a["mark_explicit"]["noul"] >= 0.9:
                await self.memory.save(
                    "landmark", u.text, u.utterance_id, s["view"]["self"]["position"]
                )
                self.say("记住这个地点了；以后提到它，我会说明这是当时观察的位置。")
            else:
                self.say("请说明要记住当前地点的什么用途。")
            return
        if route == "RECALL":
            epoch = self.epoch

            async def recall():
                try:
                    landmarks = await coach.memory_relevance(
                        r.controller, u.text, self.memory.landmarks
                    )
                except Exception:
                    landmarks = self.memory.landmarks[-4:]
                if epoch == self.epoch and u.utterance_id == r.current_utterance:
                    self.say(
                        None,
                        source=u.utterance_id,
                        cue=coach.cue(u.text, s, self.memory.goal, landmarks),
                    )

            r.spawn(recall())
            return
        if route in {"EXPLAIN", "HISTORY"}:
            if p.get("analysis_epoch", r.analysis_epoch) != r.analysis_epoch:
                return
            r.cancel_analysis()
            prior = p.get("prior") if route == "HISTORY" else None
            historical = bool(prior and self.valid(prior["binding"]))
            observation = {
                "snapshot": prior["snapshot"] if historical else coach.summary(s),
                "scope": "history" if historical else "game",
                "text": prior["text"] if historical else "",
                "explicit_goal": self.memory.goal,
                "landmarks": copy.deepcopy(self.memory.landmarks),
            }
            task = Task(
                logical_request_id=u.utterance_id,
                source_utterance_id=u.utterance_id,
                objective=u.text,
                origin_session_id=r.session_id,
                skill="remote_game_analysis",
                expected_output="answer",
                allowed_tool_scope=["get_game_snapshot", "lookup_game_rule"],
                history=r.history[-4:],
                observations=[observation],
                response_language=r.response_language,
            )
            r.task_bindings[task.task_id] = self.binding(
                "history" if historical else "analysis", s
            )
            r.current_analysis = task.task_id
            r.start_worker(u, replacement=task)
            self.say("我分析一下，你可以继续操作。", "ack")
            return
        if route == "FACT":
            field = a["fact"]["choice"]
            text = coach.fact(s, field)
            bound_field = {
                "HP": "hp",
                "HUNGER": "hunger",
                "HELD": "wielded",
                "INVENTORY": "inventory_summary",
            }.get(field)
            self.say(
                text,
                "fast",
                self.binding("fact", s, bound_field),
                u.utterance_id,
                coach.cue(
                    u.text,
                    s,
                    self.memory.goal,
                    self.memory.landmarks,
                    history=r.history,
                ),
            )
            return
        if route == "ADVICE":
            epoch = self.epoch

            async def respond():
                from .agenda import advice

                try:
                    text = await advice(self, u.text, self.bridge.snapshot)
                    if epoch == self.epoch and u.utterance_id == r.current_utterance:
                        binding = self.binding("game")
                        self.advice_offered = {
                            "id": new_id(),
                            "text": text,
                            "snapshot": coach.summary(self.bridge.snapshot),
                            "binding": self.binding("history"),
                        }
                        r.emit(
                            "voxel.advice",
                            advice_id=self.advice_offered["id"],
                            text=text,
                            provider="jev_and_reasoning",
                        )
                        self.say(text, "game_help", binding, u.utterance_id)
                except Exception as error:
                    self.r.emit("voxel.advice.failed", error=type(error).__name__)
                    if epoch == self.epoch and u.utterance_id == r.current_utterance:
                        self.say(
                            "建议暂时没有生成出来，你的问题和当前目标都还在。",
                            source=u.utterance_id,
                        )

            self.r.spawn(respond())
            return
        direction = a["advice"]["choice"] if route == "ADVICE" else None
        if route == "ADVICE":
            self.advice_offered = {
                "id": new_id(),
                "text": coach.LABELS[direction],
                "snapshot": coach.summary(s),
                "binding": self.binding("history", s),
            }
            r.emit(
                "voxel.advice",
                advice_id=self.advice_offered["id"],
                text=coach.LABELS[direction],
                direction=direction,
                provider="jev",
            )
        rid = self.say(
            None,
            "game_help",
            self.binding("advice" if route == "ADVICE" else "game", s),
            u.utterance_id,
            coach.cue(
                u.text,
                s,
                self.memory.goal,
                self.memory.landmarks,
                direction,
                history=r.history,
            ),
        )
        if rid and route == "ADVICE":
            r.presentations[rid]["voxel_advice_id"] = self.advice_offered["id"]

    async def flush(self):
        r = self.r
        if not (r.user_speaking or r.route_pending or r.response_id or r.muted):
            while self.speech_queue:
                item = self.speech_queue.popleft()
                self.deliver_speech(item)
                if r.response_id:
                    return
        if (
            not self.pending
            or r.user_speaking
            or r.route_pending
            or r.response_id
            or r.muted
        ):
            return
        data = self.pending
        self.pending = None
        if self.valid(data["binding"]):
            self.say(None, "game_advice", data["binding"], cue=data["cue"])

    async def cancel_goal(self, target, source, *, record=None):
        p = self.building.project if self.building else None
        t = self.operations.task if self.operations else None
        if (
            record
            and record.get("owner") == "PROJECT"
            and (not p or record["id"] != p.project_id)
        ):
            historical = self.building.project_history.get(record["id"])
            if historical:
                historical.status = "CANCELLED"
                historical.question = None
                historical.authorization = None
                historical.execution_intent = False
                await self.building.store.save(historical, source, current=False)
                self.say(
                    "已取消“" + historical.title + "”，已经放好的方块仍保留。",
                    source=source,
                )
                return
        cancel_project = (
            target in {"PROJECT", "ALL"}
            and p is not None
            or bool(p and record and (record.get("id") == p.project_id))
        )
        cancel_task = (
            target in {"TASK", "ALL"}
            and t is not None
            or (cancel_project and t and (t.get("project_id") == p.project_id))
        )
        if cancel_task:
            self.operations.stop("task_cancelled_by_user")
            t["status"] = "CANCELLED"
            t["pending_question"] = None
            self.operations.emit()
            await self.operations.save(source)
        if cancel_project:
            self.building.stop("project_cancelled_by_user")
            p.status = "CANCELLED"
            p.question = None
            p.authorization = None
            self.building.emit()
            await self.building.persist(source)
            if self.memory.world:
                await self.memory.save(
                    "cancelled_goal",
                    p.title + " " + str((p.blueprint or {}).get("title", "")),
                    source,
                )
        if target == "ALL" and self.building:
            for old in self.building.project_history.values():
                if (
                    p
                    and old.project_id == p.project_id
                    or old.status in {"CANCELLED", "COMPLETED"}
                ):
                    continue
                old.status = "CANCELLED"
                old.authorization = None
                old.execution_intent = False
                old.question = None
                await self.building.store.save(old, source, current=False)
        self.confirmation = None
        self.pending = None
        self.say(
            "已取消这项建造任务，后续不会再为它采集或施工。"
            if cancel_project
            else "已取消当前任务。",
            source=source,
        )

    async def handle(self, kind, payload):
        r = self.r
        if kind == "user.speech.started":
            self.speech_turn += 1
            self.speech_queue.clear()
            self.pending = None
            if self.bridge:
                self.origins[payload["utterance_id"]] = self.bridge.origin(
                    payload["utterance_id"]
                )
                self.r.spawn(self.bridge.interrupt_step("user_speech"))
        if kind == "voxel.speech.flush":
            await self.flush()
            return True
        if kind == "voxel.focus_route":
            await self.apply_focus(payload)
            return True
        if kind == "voxel.knowledge_route":
            if (
                payload["epoch"] != self.epoch
                or payload["update"].utterance_id != r.current_utterance
            ):
                return True
            r.route_pending = False
            self.unhandled_turns.clear()
            if (
                not self.bridge.snapshot
                or payload["snapshot"]["world_id"] != self.bridge.snapshot["world_id"]
            ):
                return True
            if r.paused_response:
                r.clear_playback("confirmed_user_request")
            answer = payload["result"]
            source = payload["update"].utterance_id
            if answer.get("documentation"):
                epoch = self.epoch

                async def explain_item():
                    from .rules import render_item

                    try:
                        text = await render_item(
                            self.reasoning,
                            payload["update"].text,
                            answer["documentation"],
                        )
                        if epoch == self.epoch and source == r.current_utterance:
                            r.emit(
                                "voxel.knowledge",
                                topic=answer["topic"],
                                text=text,
                                source=answer["source"],
                            )
                            self.say(text, "game_help", source=source)
                    except Exception as error:
                        r.emit(
                            "voxel.knowledge.failed",
                            topic=answer["topic"],
                            reason=type(error).__name__,
                        )
                        if epoch == self.epoch and source == r.current_utterance:
                            self.say(
                                "我查到了物品说明，回答暂时没有完整生成；你可以让我重说。",
                                source=source,
                            )

                r.spawn(explain_item())
                return True
            r.emit("voxel.knowledge", **answer)
            self.say(answer["text"], "game_help", source=source)
            return True
        if kind == "voxel.cancel_route":
            if (
                payload["epoch"] != self.epoch
                or payload["update"].utterance_id != r.current_utterance
            ):
                return True
            r.route_pending = False
            self.unhandled_turns.clear()
            if (
                not self.bridge.snapshot
                or payload["snapshot"]["world_id"] != self.bridge.snapshot["world_id"]
            ):
                return True
            target = payload["result"].get("cancel_target", {})
            selection = target.get("choice", "NONE")
            if selection == "NONE" or target.get("confidence", 0) < 0.6:
                task = self.operations.task if self.operations else None
                project = self.building.project if self.building else None
                review = await self.r.controller.judge_typed(
                    "voxel_cancel_referent",
                    {
                        "current_request": payload["update"].text,
                        "task": task and task.get("request"),
                        "project": project and project.title,
                    },
                    {
                        "target": {
                            "type": "choice",
                            "instructions": "Resolve which existing goal records the user wants retired. 全部清空/取消所有 means ALL. Named house/project means PROJECT; current task means TASK. Never remove blocks or general memory. NONE only if the requested record is unclear.",
                            "criteria": {
                                "ALL": "All current task/project records",
                                "TASK": "Current gameplay task",
                                "PROJECT": "Current construction project",
                                "NONE": "Unclear referent",
                            },
                        }
                    },
                    deadline_ms=2000,
                )
                target = review["target"]
                selection = target["choice"]
                if selection == "NONE" or target["confidence"] < 0.65:
                    self.say(
                        "要取消哪项任务？可以说任务名，或取消所有任务。",
                        source=payload["update"].utterance_id,
                    )
                    return True
            await self.cancel_goal(selection, payload["update"].utterance_id)
            return True
        if self.operations and await self.operations.handle(kind, payload):
            return True
        if kind == "voxel.survival_route" and self.operations:
            if (
                payload["epoch"] != self.epoch
                or payload["update"].utterance_id != r.current_utterance
            ):
                return True
            s = payload["snapshot"]
            if not self.bridge.fresh() or any(
                (s[k] != self.bridge.snapshot[k] for k in IDENTITY)
            ):
                r.route_pending = False
                self.unhandled_turns.clear()
                self.say(
                    "刚才游戏连接发生了变化，我还没开始这项任务；连接恢复后可以让我继续。",
                    source=payload["update"].utterance_id,
                )
                return True
            self.unhandled_turns.clear()
            r.route_pending = False
            if r.paused_response:
                r.clear_playback("confirmed_user_request")
            a = payload["result"]
            if a.get("effective_route", a["route"]["choice"]) == "RECLAIM":
                self.operations.stop("semantic_reclaim")
                if self.building:
                    self.building.stop("semantic_reclaim")
                    self.building.emit()
                self.bridge.revoke("semantic_reclaim")
                self.operations.emit()
                await self.operations.save(payload["update"].utterance_id)
                if a["change"]["noul"] < 0.8:
                    self.say("好的，后面由你操作。")
                    return True
            await self.operations.apply(payload["update"], a, payload["origin"], s)
            return True
        if self.building and await self.building.handle(kind, payload):
            return True
        if kind == "voxel.route":
            await self.apply(payload)
            return True
        if kind == "voxel.action_result":
            result = payload["result"]
            r.emit("voxel.action.completed", **result)
            if payload["epoch"] != self.epoch or not self.valid(payload["binding"]):
                return True
            text = {
                "SUCCEEDED": "已经放好一块，接下来由你操作。",
                "UNKNOWN": "结果还没确认，自动操作已停止，正在保留原操作记录。",
                "CANCELLED": "已经交还控制，没有继续执行旧操作。",
            }.get(result["status"], "这次没有放置成功，请停稳并重新瞄准后再授权。")
            self.say(text, "game_receipt", payload["binding"])
            return True
        if kind == "voxel.event":
            event = payload["kind"]
            if event == "snapshot":
                s = payload["snapshot"]
                r.emit("voxel.snapshot", snapshot=s)
                if self.bridge.input_mode == "released":
                    self.r.spawn(self.bridge.display.release())
                if self.operations:
                    await self.operations.on_snapshot(s)
                if self.building:
                    await self.building.on_snapshot(s)
                self.episodes.observe(s, self.previous)
                self.discoveries.observe(s, self.previous)
                if self.previous and any((s[k] != self.previous[k] for k in IDENTITY)):
                    self.pending = None
                    self.confirmation = None
                    self.last_advice = None
                    self.advice_offered = None
                    if s["world_id"] != self.previous["world_id"]:
                        self.focus.current = None
                        self.focus.offers.clear()
                        self.focus.history.clear()
                        self.focus.discussion = None
                if self.memory.world != s["world_id"]:
                    await self.memory.load(s["world_id"])
                r.expire_game_speech("voxel_relevance_changed")
                for task_id, b in list(r.task_bindings.items()):
                    task = r.tasks.get(task_id)
                    if (
                        b
                        and b.get("voxel")
                        and (not self.valid(b))
                        and task
                        and (
                            task.status
                            in {
                                TaskStatus.RUNNING,
                                TaskStatus.QUEUED,
                                TaskStatus.COMPLETED,
                            }
                        )
                    ):
                        r.invalidate(task, TaskStatus.SUPERSEDED)
                if (
                    r.proactive_enabled
                    and self.episodes.pending
                    and (not self.proactive_job)
                    and (self.job is None or self.job.done())
                    and (
                        time.monotonic() - self.last_proactive
                        >= (
                            3
                            if any(
                                (
                                    k in self.episodes.pending
                                    for k in (
                                        "underwater",
                                        "nearby_threat",
                                        "health_critical",
                                        "breath_low",
                                    )
                                )
                            )
                            else 12
                        )
                    )
                ):
                    previous = self.previous
                    self.last_proactive = time.monotonic()
                    epoch = self.epoch
                    speech = self.speech_epoch
                    evidence = self.episodes.take()

                    async def choose():
                        try:
                            a = await coach.proactive(
                                r.controller,
                                s,
                                previous,
                                self.memory.goal,
                                self.memory.landmarks,
                                evidence,
                            )
                            r.post(
                                "voxel.proactive",
                                {
                                    "answers": a,
                                    "snapshot": s,
                                    "epoch": epoch,
                                    "speech_epoch": speech,
                                    "evidence": evidence,
                                },
                            )
                        except Exception as error:
                            r.emit(
                                "voxel.proactive.skipped", reason=type(error).__name__
                            )
                        finally:
                            self.proactive_job = None

                    self.proactive_job = r.spawn(choose())
                if (
                    r.proactive_enabled
                    and (not r.user_speaking)
                    and (not r.route_pending)
                    and (not r.response_id)
                    and (not self.discovery_job or self.discovery_job.done())
                    and (not self.proactive_job)
                    and (not self.reply_job or self.reply_job.done())
                    and (not self.bridge.task_current())
                    and (not self.bridge.project_current())
                    and (time.monotonic() - self.last_spoken_at >= 60)
                ):
                    evidence = self.discoveries.take()
                    if evidence:
                        self.respond_companion(
                            "陪我观察刚发现的东西，讲一个有用的用途或下一步；不要自动操作。",
                            evidence=evidence,
                        )
                self.previous = s
                await self.flush()
            else:
                if event == "native" and self.building:
                    self.building.observe_event(payload["event"])
                r.emit(
                    "voxel." + event,
                    **{k: v for k, v in payload.items() if k != "kind"},
                )
            return True
        if kind == "voxel.proactive":
            if (
                payload["epoch"] != self.epoch
                or payload["speech_epoch"] != self.speech_epoch
            ):
                return True
            if not self.episodes.still_relevant(payload.get("evidence", [])):
                return True
            a = payload["answers"]
            direction = a["advice"]["choice"]
            if direction == "SILENT" or a["value"]["score"] < 2:
                r.emit("voxel.proactive.silent", provider="jev")
                return True
            original = payload["snapshot"]
            s = self.bridge.snapshot
            if not s or any((s[k] != original[k] for k in IDENTITY)):
                return True
            b = self.binding("reminder", s)
            b["evidence"] = payload["evidence"]
            if self.valid(b):
                self.pending = {
                    "binding": b,
                    "cue": coach.cue(
                        "请简短提醒一句，说明眼下值得留意的事。",
                        s,
                        self.memory.goal,
                        self.memory.landmarks,
                        direction,
                        evidence=payload["evidence"],
                    ),
                }
                r.emit("voxel.proactive.ready", direction=direction, provider="jev")
                await self.flush()
            return True
        if kind == "game.command":
            action = payload.get("action")
            if action == "inspect_state":
                now = time.monotonic()
                if (
                    self.bridge.snapshot
                    and now - getattr(self, "last_inspection", 0) >= 1
                ):
                    self.last_inspection = now
                    r.emit("voxel.inspection", snapshot=self.bridge.snapshot)
                return True
            if action == "task_resume" and self.operations and self.operations.task:
                if self.operations.task.get("status") == "CANCELLED":
                    return True
                if payload.get("task_id") != self.operations.task["id"]:
                    return True
                origin = self.bridge.origin(
                    new_id(), client=payload.get("voxel_anchor")
                )
                if origin:
                    self.operations.task.update(
                        authorized=True,
                        generation=self.bridge.generation,
                        source=origin["source_event_id"],
                    )
                    self.operations.begin(origin)
                return True
            if action == "project_start" and self.building and self.building.project:
                if self.building.project.status == "CANCELLED":
                    return True
                if (
                    payload.get("project_id") != self.building.project.project_id
                    or payload.get("revision") != self.building.project.revision
                ):
                    return True
                self.r.muted = False
                origin = self.bridge.origin(
                    new_id(), client=payload.get("voxel_anchor")
                )
                if origin:
                    self.building.project.authorization = {
                        "source": origin["source_event_id"],
                        "duration_ms": 900000,
                        "generation": self.bridge.generation,
                    }
                    self.building.begin(origin)
                return True
            if action == "project_pause" and self.building:
                self.building.stop("button_project_pause", speak=True)
                self.building.emit()
                return True
            if action == "advice_seen":
                if (
                    self.advice_offered
                    and payload.get("advice_id") == self.advice_offered["id"]
                ):
                    self.last_advice = copy.deepcopy(self.advice_offered)
                return True
            if action == "reclaim":
                self.unhandled_turns.clear()
                self.origins.clear()
                if self.operations:
                    self.operations.stop("button_reclaim")
                    self.operations.emit()
                if self.building:
                    self.building.stop("button_reclaim")
                self.bridge.revoke("button_reclaim")
                self.confirmation = None
                return True
            if action == "cancel_analysis":
                r.cancel_analysis()
                return True
            if action == "proactive":
                r.proactive_enabled = bool(payload.get("enabled"))
                return True
            if action in {"text", "ask_move", "play_once"}:
                text = {
                    "ask_move": "根据当前局面和我的目标，下一步做什么？",
                    "play_once": "请在当前准星面放一块当前手持的泥土。",
                }.get(action, payload.get("text", ""))
                text = str(text).strip()[:1200]
                if text:
                    uid = new_id()
                    await SessionRuntime.handle(
                        r,
                        "user.speech.started",
                        {"utterance_id": uid, "audio_sample": 0},
                    )
                    if action == "play_once":
                        self.origins[uid] = self.bridge.origin(
                            uid, client=payload.get("voxel_anchor", {})
                        )
                    await r.transcript(
                        TranscriptUpdate(
                            utterance_id=uid,
                            revision=1,
                            text=text,
                            is_final=True,
                            audio_end_sample=1,
                        )
                    )
                return True
            return True
        if kind == "voxel.advice.seen":
            if (
                self.advice_offered
                and payload.get("advice_id") == self.advice_offered["id"]
            ):
                self.last_advice = copy.deepcopy(self.advice_offered)
            return True
        if kind == "stop_speaking":
            self.pending = None
            self.episodes.pending.clear()
            self.speech_epoch += 1
            self.speech_turn += 1
            self.speech_queue.clear()
            if self.building:
                self.building.pending_voice = None
        if kind == "remote.lease":
            r.expire_game_speech("voxel_ttl")
            await self.flush()
            if self.building:
                await self.building.flush()
            return True
        if kind.startswith(("remote.", "game.bridge")):
            return True
        return False
