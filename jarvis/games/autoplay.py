import asyncio
import time
from jarvis.games import coach
from jarvis.games.remote import BridgeError
from jarvis.core.schemas import new_id


class PvZAutoplay:
    def init_autoplay(self):
        self.autoplay_run = None
        self.autoplay_job = None
        self.autoplay_start_job = None
        self.autoplay_wake = asyncio.Event()
        self.control_confirmation = None
        self.command_pending = False

    def autoplay_context(self):
        run = self.autoplay_run
        return (
            {
                "scope": run["scope"],
                "request": run["request"],
                "task": run["command"].public() if run.get("command") else None,
            }
            if run
            else None
        )

    def stop_autoplay(self, reason, *, revoke=True):
        run = self.autoplay_run
        self.autoplay_run = None
        self.command_pending = False
        current = asyncio.current_task()
        if (
            self.autoplay_start_job
            and self.autoplay_start_job is not current
            and (not self.autoplay_start_job.done())
        ):
            self.autoplay_start_job.cancel()
        if (
            self.autoplay_job
            and self.autoplay_job is not current
            and (not self.autoplay_job.done())
        ):
            self.autoplay_job.cancel()
        self.autoplay_job = None
        self.autoplay_wake.set()
        self.control_confirmation = None
        if revoke:
            self.remote.fence_control()
            if self.remote.connected:
                self.spawn(self.remote.revoke(reason, wait=False))
        if not run:
            return
        text = {
            "time_expired": "时间到了，交还给你。",
            "battle_ended": "本关结束，交还给你。",
            "user_reclaimed": "好的，接下来你来。",
            "native_input": "你已接手，代打停止。",
            "decision_uncertain": "这一步还没判断清楚，已暂停代打。你想继续交给我，还是自己来？",
            "decision_unavailable": "暂时没取得下一步判断，代打已停止。",
            "action_outcome_unknown": "这次操作结果还没确认，已停止后续代打。",
            "disconnected": "连接中断，代打已停止。",
            "game_changed": "游戏已切换，代打结束。",
            "host_stopped": "游戏已交还给你。",
            "task_completed": "你交给我的操作已完成，交还给你。",
            "task_blocked": "剩余位置有植物占用或操作暂不可用；我已停下，请先处理这些位置。",
        }.get(reason, "代打已停止，交还给你。")
        self.emit(
            "remote.autoplay.stopped",
            autoplay_id=run["id"],
            reason=reason,
            text=text,
            completed=run["completed"],
        )
        if (
            not getattr(self, "story", None)
            and reason
            in {
                "time_expired",
                "battle_ended",
                "user_reclaimed",
                "decision_uncertain",
                "action_outcome_unknown",
                "task_completed",
                "task_blocked",
            }
            and (not self.response_id)
            and (not self.user_speaking)
            and (not self.route_pending)
        ):
            self.speak_game(text, "game_status", self.binding("game"))

    async def begin_autoplay(
        self, origin, scope, duration_ms, request, *, command=None, resume_run=None
    ):
        binding = self.remote.binding
        generation = getattr(self, "control_generation", 0)
        try:
            if self.autoplay_run:
                old = self.autoplay_run
                self.autoplay_run = None
                job = self.autoplay_job
                self.autoplay_job = None
                if job and (not job.done()):
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
                for grant_id in list(self.remote.grants):
                    await self.remote.discard_step(grant_id)
                self.emit(
                    "remote.autoplay.stopped",
                    autoplay_id=old["id"],
                    reason="scope_changed",
                    text="正在按新要求调整代打范围。",
                    completed=old["completed"],
                )
            kinds = command.allowed() if command else ["PLANT", "COLLECT_SUN"]
            if command and command.after == "CONTINUE":
                kinds = list(dict.fromkeys(kinds + ["PLANT", "COLLECT_SUN"]))
            if resume_run and resume_run.get("command"):
                kinds = list(dict.fromkeys(kinds + resume_run["command"].allowed()))
            if resume_run and resume_run.get("expires"):
                remaining = int((resume_run["expires"] - time.monotonic()) * 1000)
                if remaining < 1000:
                    await self.remote.revoke("time_expired", wait=False)
                    return
                scope = "timed"
                duration_ms = remaining
            issued = await self.remote.start_autoplay(origin, scope, duration_ms, kinds)
            if binding != self.remote.binding:
                return
            if generation != getattr(self, "control_generation", 0):
                await self.remote.revoke("newer_control_request", wait=False)
                return
            self.pending_advice = None
            self.pending_help = None
            self.receipts.clear()
            self.choice_pending = None
            self.advice_epoch += 1
            if (
                not getattr(self, "story", None)
                and self.response_id
                and self.presentations[self.response_id].get("remote_binding")
            ):
                self.clear_playback("autoplay_started")
            run = {
                **issued,
                "request": request,
                "completed": 0,
                "uncertain_count": 0,
                "wait_key": None,
                "next_decision": 0.0,
                "target": None,
                "command": command,
                "resume_request": resume_run["request"] if resume_run else request,
                "resume_command": resume_run.get("command") if resume_run else None,
            }
            self.autoplay_run = run
            duration_text = (
                f"{duration_ms / 60000:g}分钟"
                if duration_ms and duration_ms % 60000 == 0
                else f"{duration_ms / 1000:g}秒"
                if duration_ms
                else ""
            )
            text = {
                "continuous": "好，我来持续代打。你说“我自己来”就交还。",
                "timed": f"好，我帮你代打{duration_text}，到时交还。",
                "level": "好，这一关我来，结束后交还给你。",
                "task": "好，我按你说的目标操作，完成后告诉你。",
            }[scope]
            if command:
                self.emit(
                    "remote.command.started",
                    command=command.public(),
                    autoplay_id=run["id"],
                )
                text = "好，我来" + command.description(self.remote.snapshot) + "。"
            self.emit(
                "remote.autoplay.started",
                autoplay_id=run["id"],
                scope=scope,
                duration_ms=duration_ms,
                text=text,
            )
            if (
                not getattr(self, "story", None)
                and (not self.user_speaking)
                and (not self.route_pending)
                and (not self.response_id)
            ):
                self.speak_game(text, "game_status", self.binding("game"))
            self.autoplay_job = self.spawn(self.autoplay_loop(run))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.emit("remote.autoplay.failed", reason=type(error).__name__)
            if binding == self.remote.binding:
                await self.remote.revoke("autoplay_start_failed", wait=False)
                self.show_help(
                    "这次没能接上代打，请等草坪准备好、松开鼠标后再交给我。",
                    self.remote.snapshot,
                )

    async def autoplay_loop(self, run):
        try:
            while self.autoplay_run is run:
                if run["expires"] is not None and time.monotonic() >= run["expires"]:
                    self.stop_autoplay("time_expired")
                    return
                if not self.remote.autoplay_current(run["id"]):
                    self.stop_autoplay("host_stopped")
                    return
                talking = self.user_speaking or self.route_pending
                if self.command_pending or self.choice_lock.locked():
                    await asyncio.sleep(0.08)
                    continue
                if talking:
                    command = run.get("command")
                    if (
                        command
                        and "COLLECT_SUN" not in command.allowed()
                        or not self.remote.snapshot
                        or (not self.remote.snapshot.view.suns)
                    ):
                        await asyncio.sleep(0.08)
                        continue
                now = time.monotonic()
                snapshot = self.remote.snapshot
                key = self.autoplay_position(snapshot)
                if now < run["next_decision"] and key == run["wait_key"]:
                    self.autoplay_wake.clear()
                    try:
                        await asyncio.wait_for(
                            self.autoplay_wake.wait(),
                            min(0.5, run["next_decision"] - now),
                        )
                    except TimeoutError:
                        pass
                    continue
                command = run.get("command")
                target_before = run.get("target")
                focus = (
                    command.focus()
                    if command
                    else {
                        "kinds": ["PLANT"],
                        "plant_types": [target_before["plant_type"]],
                        "rows": [target_before["row"]],
                        "cols": [],
                    }
                    if target_before and target_before.get("plant_type")
                    else None
                )
                snapshot = (
                    await self.remote.observe(focus)
                    if focus
                    else await self.remote.observe()
                )
                if (
                    not self.remote.autoplay_current(run["id"])
                    or not snapshot.awaiting_action
                ):
                    self.stop_autoplay("host_stopped")
                    return
                if command:
                    done, total, status = command.progress(snapshot)
                    if status != "working":
                        self.emit(
                            "remote.command.finished",
                            command=command.public(),
                            done=done,
                            total=total,
                            status=status,
                        )
                        if status == "complete" and command.after == "CONTINUE":
                            run["command"] = run.pop("resume_command", None)
                            run["request"] = (
                                "指定任务已完成："
                                + command.request
                                + "。现在执行完成后的其余玩法要求，继续代打。"
                            )
                            run["target"] = None
                            if run["resume_request"] != command.request:
                                run["request"] += (
                                    "先前代打要求：" + run["resume_request"]
                                )
                            self.emit(
                                "remote.command.continuing",
                                text="指定操作已完成，继续代打。",
                            )
                            continue
                        self.stop_autoplay(
                            "task_completed" if status == "complete" else "task_blocked"
                        )
                        return
                origin = self.remote.origin(new_id())
                async with self.choice_lock:
                    if self.command_pending:
                        continue
                    try:
                        decision = {}
                        if talking:
                            from jarvis.games.tactics import collect_choice

                            update = self.transcripts.get(self.current_utterance)
                            action = await collect_choice(
                                self.controller,
                                snapshot,
                                request=run["request"],
                                current_speech=update.text if update else "",
                            )
                            provider = "jev_collection_while_talking"
                            decision["target"] = run.get("target")
                            if action is None:
                                await asyncio.sleep(0.25)
                                continue
                        elif command:
                            from jarvis.games.commands import choose as command_step

                            action = await command_step(
                                self.controller, command, snapshot
                            )
                            provider = "jev_command"
                        else:
                            action, provider = await coach.choose(
                                self.controller,
                                snapshot,
                                request=run["request"],
                                autoplay=True,
                                execution=True,
                                reviewer=self.worker,
                                limit=self.config["game_expansion"]["candidate_limit"],
                                prior=run.get("target"),
                                decision=decision,
                            )
                    except TimeoutError:
                        run["uncertain_count"] += 1
                        run.setdefault("outage_started", time.monotonic())
                        self.emit("remote.autoplay.wait", reason="decision_timeout")
                        if time.monotonic() - run["outage_started"] >= 12:
                            self.stop_autoplay("decision_unavailable")
                            return
                        run["next_decision"] = time.monotonic() + 1
                        run["wait_key"] = self.autoplay_position(self.remote.snapshot)
                        continue
                if self.autoplay_run is not run or not self.remote.autoplay_current(
                    run["id"]
                ):
                    return
                if self.command_pending or (
                    (self.user_speaking or self.route_pending)
                    and (not action or action.kind != "COLLECT_SUN")
                ):
                    continue
                run.pop("outage_started", None)
                run["target"] = decision.get("target")
                self.tactical_target = run["target"]
                if (
                    not command
                    and action
                    and (action.kind == "PLANT")
                    and run["target"]
                    and (
                        not target_before
                        or run["target"]["seed_id"] != target_before["seed_id"]
                        or run["target"]["row"] != target_before["row"]
                    )
                ):
                    run["next_decision"] = 0.0
                    run["wait_key"] = None
                    continue
                run["wait_key"] = self.autoplay_position(self.remote.snapshot)
                run["next_decision"] = time.monotonic() + 0.7
                if action is None:
                    run["uncertain_count"] += 1
                    self.emit(
                        "remote.autoplay.wait", reason="uncertain", provider=provider
                    )
                    run.setdefault("uncertain_started", time.monotonic())
                    if time.monotonic() - run["uncertain_started"] >= 12:
                        self.stop_autoplay("decision_uncertain")
                        return
                    continue
                run["uncertain_count"] = 0
                run.pop("uncertain_started", None)
                if action.kind == "WAIT":
                    self.emit(
                        "remote.autoplay.wait", reason="jev_wait", provider=provider
                    )
                    continue
                if (
                    not self.remote.fresh()
                    or time.monotonic()
                    - self.remote.contexts.get(origin.anchor.snapshot_seq, (None, 0))[1]
                    > 2.3
                ):
                    old = self.binding("position", snapshot=snapshot, action=action)
                    latest = await self.remote.observe()
                    if self.autoplay_run is not run or not self.remote.autoplay_current(
                        run["id"]
                    ):
                        return
                    if not self.valid(old, referenced_action=True):
                        continue
                    action = next(
                        (
                            a
                            for a in latest.legal_actions
                            if a.kind == action.kind and a.facts == action.facts
                        ),
                        None,
                    )
                    if action is None:
                        continue
                    origin = self.remote.origin(new_id())
                if not self.remote.current(origin, require_window=True):
                    continue
                grant = await self.remote.arm_once(
                    origin, [action.kind], autoplay_id=run["id"]
                )
                if self.command_pending or (
                    (self.user_speaking or self.route_pending)
                    and action.kind != "COLLECT_SUN"
                ):
                    await self.remote.discard_step(grant.grant_id)
                    continue
                result = await self.remote.commit_action(
                    origin, grant.grant_id, action.action_id
                )
                if self.autoplay_run is not run:
                    return
                if result.status == "UNKNOWN":
                    self.stop_autoplay("action_outcome_unknown")
                    return
                if result.status.startswith("CONFIRMED"):
                    run["completed"] += 1
                    if getattr(self, "story", None):
                        self.story.action({"action": action.model_dump(mode="json")})
                    if command and action.kind == command.operation:
                        command.confirmed += 1
                    self.emit(
                        "remote.autoplay.progress",
                        autoplay_id=run["id"],
                        completed=run["completed"],
                        text=action.label,
                        action=action.model_dump(mode="json"),
                        provider=provider,
                    )
                    if (
                        run.get("target")
                        and action.kind == "PLANT"
                        and (action.facts.seed_id == run["target"]["seed_id"])
                        and (action.facts.row == run["target"]["row"])
                    ):
                        run["target"] = None
                    run["next_decision"] = time.monotonic()
                    run["wait_key"] = None
        except asyncio.CancelledError:
            raise
        except BridgeError:
            if self.autoplay_run is run:
                self.stop_autoplay("host_stopped")
        except Exception as error:
            self.emit(
                "remote.autoplay.error",
                reason=type(error).__name__,
                detail=str(error)[:120] if isinstance(error, ValueError) else "",
            )
            if self.autoplay_run is run:
                self.stop_autoplay("decision_unavailable")

    @staticmethod
    def autoplay_position(snapshot):
        if not snapshot:
            return None
        v = snapshot.view
        return (
            snapshot.phase,
            getattr(v, "sun", None),
            tuple(((s.seed_id, s.ready) for s in v.seed_bank)),
            tuple(((p.entity_id, p.row, p.col, p.state) for p in v.plants)),
            tuple(((z.entity_id, int(z.x // 35), z.state) for z in v.zombies)),
            tuple((s.entity_id for s in v.suns)),
        )

    def autoplay_snapshot(self):
        run = self.autoplay_run
        s = self.remote.snapshot
        if not run:
            return
        self.autoplay_wake.set()
        if run["expires"] is not None and time.monotonic() >= run["expires"]:
            self.stop_autoplay("time_expired")
            return
        if not s or not self.remote.connected:
            self.stop_autoplay("disconnected")
            return
        if s.phase == "ended":
            self.stop_autoplay("battle_ended")
            return
        if run["binding"] != self.remote.binding:
            self.stop_autoplay("game_changed")
            return
        if not self.remote.autoplay_current(run["id"]):
            reason = s.control_reason
            self.stop_autoplay(
                "time_expired"
                if reason == "time_expired"
                else "battle_ended"
                if reason == "battle_ended"
                else "native_input"
                if reason.startswith("native_input")
                else "host_stopped"
            )
