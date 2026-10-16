from .capture import now_ms


class NativeSceneMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_scene_pending = None
        self.native_scene_seen = {}
        self.scene_admission = self.config["native_routing"].get(
            "scene_admission", "native"
        )
        if self.scene_admission not in {"native", "jev"}:
            raise ValueError("Unknown scene admission owner")

    async def watch_frames(self):
        if self.scene_admission == "jev":
            self.emit(
                "native.scene.admission_owner",
                owner="jev",
                microphone_owner="venus_grpo",
            )
            return await super().watch_frames()
        while not self.closed:
            await self.capture.timeline.next_frame()
            self.belief.prune(
                at_ms=now_ms(),
                pinned=self.scheduler.active.claim_ids if self.scheduler.active else (),
            )

    def release_native_scene(self, scope, reason, *, allow_resume=True):
        self.send_venus(
            {"type": "control", "router_consumed_sample": scope["audio_end_sample"]}
        )
        if (
            allow_resume
            and (not self.native_stopped)
            and (not self.muted)
            and (not self.response_id)
            and (not self.native_backend_queue)
        ):
            self.send_venus(
                {
                    "type": "control",
                    "native_resume": True,
                    "generation_epoch": self.generation_epoch,
                }
            )
        self.emit(
            "native.scene.finished", response_id=scope["response_id"], reason=reason
        )

    def cancel_native_scene(self, reason):
        scope = self.native_scene_pending
        if scope is None:
            return
        self.native_scene_pending = None
        if self.frame_job and (not self.frame_job.done()):
            self.frame_job.cancel()
        self.release_native_scene(
            scope, reason, allow_resume=reason == "user.speech.started"
        )

    def capture_changed(self, game=None):
        self.cancel_native_scene("capture_changed")
        result = super().capture_changed(game)
        self.send_venus(
            {
                "type": "control",
                "game": self.game,
                "generation_epoch": self.generation_epoch,
            }
        )
        self.emit("native.game_context", game_id=self.game, source="screen_capture")
        if self.config["native_routing"].get("proactive"):
            self.native_status_context()
        return result

    async def select_native_scene(self, payload):
        self.emit("native.route.marker", **payload)
        if payload.get("generation_epoch") != self.generation_epoch:
            return
        wire = payload["response_id"]
        if wire in self.native_scene_seen:
            self.emit("native.scene.duplicate", response_id=wire)
            return
        self.native_scene_seen[wire] = True
        while len(self.native_scene_seen) > 256:
            self.native_scene_seen.pop(next(iter(self.native_scene_seen)))
        scope = {
            key: payload[key]
            for key in ("response_id", "audio_end_sample", "generation_epoch")
        }
        if (
            not self.config["native_routing"].get("proactive", False)
            or payload.get("native_action_id") != 3
            or self.scene_admission != "native"
            or self.native_stopped
            or self.muted
            or self.user_speaking
            or self.response_id
            or self.route_pending
            or (self.native_scene_pending is not None)
            or self.capture.paused
        ):
            self.emit(
                "native.scene.rejected",
                response_id=wire,
                reason="disabled_or_user_control_has_priority",
            )
            self.release_native_scene(scope, "not_started")
            return
        media = payload.get("media_context", {})
        binding = self.binding()
        if (media.get("game_session_id"), media.get("scene_epoch")) != binding:
            self.emit(
                "native.scene.rejected",
                response_id=wire,
                reason="scene_binding_changed",
            )
            self.release_native_scene(scope, "not_started")
            return
        cutoff = media.get("audio_end_ts_ms")
        if not isinstance(cutoff, (int, float)) or cutoff > now_ms():
            self.emit(
                "native.scene.rejected",
                response_id=wire,
                reason="invalid_native_frame_clock",
            )
            self.release_native_scene(scope, "not_started")
            return
        settings = self.config["screen"].get("visual_window", {})
        count = settings.get("frames", 1)
        if type(count) is not int or not 1 <= count <= 16:
            raise ValueError("Invalid native scene frame limit")
        frames = self.capture.timeline.window(
            decision_at_ms=cutoff,
            duration_ms=settings.get("duration_ms", 3000),
            sources={"screen"},
        )[-count:]
        if not frames:
            self.emit(
                "native.scene.rejected",
                response_id=wire,
                reason="no_causal_screen_evidence",
            )
            self.release_native_scene(scope, "not_started")
            return
        scope.update(binding=binding, screen_epoch=self.screen_session_epoch)
        self.native_scene_pending = scope
        self.emit(
            "native.scene.selected",
            **scope,
            source_kind="native_screen_observation",
            asr_request_created=False,
            observation_ids=[f.observation.observation_id for f in frames],
            native_frame_clock_ms=cutoff,
            analysis_scope="passive_screen_analysis",
        )

        async def run():
            try:
                await self.analyze(frames, None, native_scene_binding=scope)
            finally:
                self.post("native.scene.analysis.finished", {"scope": scope})

        self.frame_job = self.spawn(run())

    async def handle(self, kind, payload):
        if kind == "venus.native.route" and payload.get("action_origin") == "scene":
            await self.select_native_scene(payload)
            return
        if kind in {"user.speech.started", "stop_speaking", "history.reset.ready"}:
            self.cancel_native_scene(kind)
            if (
                self.scene_admission == "jev"
                and self.frame_job
                and (not self.frame_job.done())
            ):
                self.frame_job.cancel()
        if kind in {"screen.analysis.ready", "screen.analysis.failed"} and payload.get(
            "native_scene_binding"
        ):
            scope = payload["native_scene_binding"]
            if (
                scope != self.native_scene_pending
                or scope["generation_epoch"] != self.generation_epoch
                or scope["binding"] != self.binding()
                or self.native_stopped
                or self.user_speaking
            ):
                self.emit(
                    "native.scene.discarded",
                    response_id=scope["response_id"],
                    reason="superseded_scene_analysis",
                )
                return
        if kind == "native.scene.analysis.finished":
            scope = payload["scope"]
            if scope == self.native_scene_pending:
                self.native_scene_pending = None
                self.release_native_scene(scope, "analysis_terminal")
            return
        if kind == "screen.analysis.ready" and payload.get("native_scene_binding"):
            before = self.response_id
            await super().handle(kind, payload)
            response = self.response_id if self.response_id != before else None
            self.emit(
                "native.scene.outcome",
                response_id=payload["native_scene_binding"]["response_id"],
                presentation_id=response,
                playback_queued=response is not None,
            )
            return
        await super().handle(kind, payload)
