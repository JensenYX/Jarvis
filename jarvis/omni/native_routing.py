import time
from collections import deque
from jarvis.core.schemas import TranscriptUpdate, new_id


class NativeRoutingMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_sources = set()
        self.native_requests = set()
        self.native_direct = set()
        self.native_delegate_queue = deque()
        self.native_last_input = 0
        self.native_wire = None
        self.native_wire_map = {}
        self.native_backend_queue = deque(maxlen=12)
        self.native_stopped = False
        self.native_last_backend = None

    async def select_game(self, game_id):
        await super().select_game(game_id)
        self.emit("native.game_context", game_id=game_id, source="ui_selection")

    async def handle(self, kind, payload):
        if kind.startswith("asr.") or kind in {
            "user.speech.started",
            "user.speech.ended",
        }:
            self.emit("native.observer_ignored", observed_type=kind)
            return
        if kind == "native.delegate":
            await self.dispatch_native(payload)
            return
        if kind == "native.next_delegate":
            if self.native_delegate_queue and (not self.route_pending):
                await self.dispatch_native(self.native_delegate_queue.popleft())
            return
        if kind == "native.idle":
            if self.native_stopped:
                return
            if not self.response_id and self.native_backend_queue:
                args, kwargs = self.native_backend_queue.popleft()
                self.speak(*args, **kwargs)
            if self.native_delegate_queue and (not self.route_pending):
                self.post("native.next_delegate")
            return
        if kind == "stop_speaking":
            self.native_stopped = True
            self.native_stop_cutoff = self.native_last_input
        await super().handle(kind, payload)
        if kind == "stop_speaking":
            self.send_venus(
                {
                    "type": "control",
                    "native_suspend": True,
                    "generation_epoch": self.generation_epoch,
                }
            )

    async def dispatch_native(self, payload):
        query = payload["text"].strip()
        key = (payload["response_id"], query)
        if key in self.native_requests:
            self.emit("native.delegate_duplicate", response_id=key[0])
            return
        if not query or len(query) > 2000 or "<" in query or (">" in query):
            self.emit("native.delegate_rejected", reason="invalid_completed_payload")
            return
        if self.route_pending:
            self.native_delegate_queue.append(payload)
            return
        self.native_requests.add(key)
        source = "venus-" + new_id()
        self.native_sources.add(source)
        self.native_stopped = False
        self.muted = False
        self.emit(
            "native.delegate.accepted",
            source=source,
            request=query,
            audio_end_sample=payload["audio_end_sample"],
            response_id=payload["response_id"],
            source_kind="observed_asr_after_native_decision"
            if payload.get("_bound")
            else "model_generated_delegate",
            asr_required=bool(payload.get("_bound")),
            jev_admission=False,
        )
        update = TranscriptUpdate(
            utterance_id=source,
            revision=1,
            text=query,
            is_final=True,
            audio_end_sample=max(1, payload["audio_end_sample"]),
            language=payload.get("language", ""),
        )
        await super().transcript(update)

    def clear_playback(self, reason):
        super().clear_playback(reason)
        if not self.native_stopped:
            self.send_venus(
                {
                    "type": "control",
                    "generation_epoch": self.generation_epoch,
                    "hold": False,
                    "native_resume": True,
                }
            )

    def speak(self, *args, **kwargs):
        current = self.presentations.get(self.response_id, {})
        if current.get("native_direct") and (not current.get("generated_done")):
            self.native_backend_queue.append((args, kwargs))
            return None
        return super().speak(*args, **kwargs)

    def venus_step(self, message):
        if not message.get("native_foreground"):
            return super().venus_step(message)
        if (
            message.get("dropped_stale_epoch")
            or message["generation_epoch"] != self.generation_epoch
        ):
            return
        self.native_last_input = max(
            self.native_last_input, message.get("audio_end_sample", 0)
        )
        if message.get("delegates"):
            self.emit(
                "native.speech_delegate_suppressed",
                response_id=message["response_id"],
                reason="only_scored_native_route_admits_harness",
            )
        if message.get("native_interrupt") and self.response_id:
            rid = self.response_id
            self.presentations[rid]["interrupted"] = True
            self.send_client(
                {
                    "type": "playback.clear",
                    "generation_epoch": self.generation_epoch,
                    "reason": "native_listen_interrupt",
                }
            )
            self.finish_presentation(rid)
            self.emit(
                "native.interrupted",
                response_id=rid,
                audio_end_sample=self.native_last_input,
            )
        if message.get("is_listen"):
            self.emit("venus.step", **{k: v for k, v in message.items() if k != "pcm"})
            self.emit(
                "native.listen",
                audio_end_sample=message.get("audio_end_sample"),
                response_id=message["response_id"],
            )
            self.post("native.idle")
            return
        if self.native_stopped:
            return
        wire = message["response_id"]
        rid = self.native_wire_map.get(wire)
        useful = bool(message.get("text", "").strip() or message.get("pcm"))
        if rid is None and useful:
            rid = new_id()
            self.native_wire_map[wire] = rid
            self.native_direct.add(rid)
            if self.response_id and self.response_id != rid:
                old = self.presentations[self.response_id]
                old["interrupted"] = True
                self.send_client(
                    {
                        "type": "playback.clear",
                        "generation_epoch": self.generation_epoch,
                        "reason": "native_new_turn",
                    }
                )
                self.finish_presentation(self.response_id)
            self.response_id = rid
            self.native_wire = wire
            self.presentations[rid] = {
                "purpose": "native_fast",
                "task": None,
                "chunks": [],
                "language": self.response_language,
                "authoritative_backend": False,
                "approved_text": None,
                "source_utterance_id": None,
                "text": [],
                "generated_done": False,
                "interrupted": False,
                "generated_samples": 0,
                "epoch": self.generation_epoch,
                "history_recorded": False,
                "deadline": time.monotonic() + 60,
                "native_direct": True,
            }
            self.emit(
                "native.fast.started",
                response_id=rid,
                wire_response_id=wire,
                audio_end_sample=self.native_last_input,
                asr_required=False,
                jev_admission=False,
            )
        if rid is not None and rid == self.response_id:
            mapped = {**message, "response_id": rid, "delegates": []}
            super().venus_step(mapped)
        else:
            self.emit("venus.step", **{k: v for k, v in message.items() if k != "pcm"})
        if message.get("end_of_turn"):
            self.post("native.idle")

    def finish_generation(self, presentation):
        if presentation.get("native_direct"):
            presentation["generated_done"] = True
            self.finish_presentation(self.response_id)
            return
        super().finish_generation(presentation)
        if not self.native_stopped:
            self.send_venus(
                {
                    "type": "control",
                    "generation_epoch": self.generation_epoch,
                    "hold": False,
                    "native_resume": True,
                }
            )

    def finish_presentation(self, response_id):
        super().finish_presentation(response_id)
        if not self.response_id:
            self.post("native.idle")

    def playback_ack(self, ack):
        chunk = self.chunks.get(ack.get("chunk_id"), {})
        rid = chunk.get("response_id")
        owned = bool(
            rid
            and rid == self.response_id
            and (ack.get("generation_epoch") == chunk.get("epoch"))
        )
        super().playback_ack(ack)
        presentation = self.presentations.get(rid, {})
        if (
            owned
            and ack.get("interrupted")
            and (not self.response_id)
            and (not self.native_stopped)
            and presentation.get("history_recorded")
            and (not presentation.get("native_direct"))
        ):
            self.send_venus(
                {
                    "type": "control",
                    "generation_epoch": self.generation_epoch,
                    "end_response": True,
                    "hold": False,
                }
            )
            self.emit(
                "native.backend.released",
                response_id=rid,
                reason="interrupted_playback_receipt",
            )
