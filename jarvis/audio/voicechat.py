import json
from contextlib import contextmanager
import websockets
from jarvis.audio.native_observer import ObserverRoutingMixin
from jarvis.audio.native_routing import NativeRoutingMixin
from jarvis.core.runtime import SessionRuntime
from jarvis.audio.voicechat_contract import (
    ADMISSION,
    DELEGATE,
    RESULT_POLICY,
    native_tool_goal,
)

CALL_PREFIX = "voicechat-call:"


class VoiceChatRuntime(ObserverRoutingMixin, SessionRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_function_sources = {}
        self.returned_function_calls = set()
        self.task_function_calls = {}
        self.decision_function_call = None
        self.function_delivery_foreground = None
        self.delivery_call = None
        sender = self.send_venus

        def send(message):
            if (
                isinstance(message, dict)
                and message.get("start_response")
                and self.delivery_call
            ):
                message = {**message, "native_call_id": self.delivery_call}
            sender(message)

        self.send_venus = send
        self.emit(
            "adapter.declared",
            model="NVIDIA-NemotronLabs-VoiceChat-11B",
            admission=ADMISSION,
            output="native_async_function_result",
            observer="Qwen3-ASR for frozen memory and freshness only",
            frontend="voicechat",
        )

    @contextmanager
    def function_delivery_window(self):
        current = self.response_id
        pending = (
            set(self.native_function_sources.values()) - self.returned_function_calls
        )
        permit = pending and self.presentations.get(current, {}).get("native_direct")
        previous = self.function_delivery_foreground
        if permit:
            self.function_delivery_foreground = current
            self.response_id = None
        try:
            yield
        finally:
            if permit and self.response_id is None:
                self.response_id = current
            self.function_delivery_foreground = previous

    async def try_deliver(self):
        with self.function_delivery_window():
            await super().try_deliver()

    async def apply_delivery(self, *args, **kwargs):
        with self.function_delivery_window():
            await super().apply_delivery(*args, **kwargs)

    async def apply_decision(self, decision):
        previous = self.decision_function_call
        self.decision_function_call = self.native_function_sources.get(
            decision.utterance_id
        )
        try:
            await super().apply_decision(decision)
        finally:
            self.decision_function_call = previous

    def start_worker(self, update, *args, **kwargs):
        task = super().start_worker(update, *args, **kwargs)
        call = self.native_function_sources.get(update.utterance_id)
        if task is not None and call is not None:
            self.task_function_calls[task.task_id, task.version] = call
        return task

    def result_eligible(self, key):
        task = self.tasks.get(key[0])
        call = self.task_function_calls.get(tuple(key))
        if call is None and task is not None:
            call = self.native_function_sources.get(task.source_utterance_id)
        if call in self.returned_function_calls and (not self.decision_function_call):
            return False
        return super().result_eligible(key)

    async def dispatch_native(self, payload):
        wire = payload["response_id"]
        if not wire.startswith(CALL_PREFIX):
            self.emit("adapter.delegate.rejected", reason="not_a_native_function_call")
            return
        previous = set(self.native_sources)
        await NativeRoutingMixin.dispatch_native(self, payload)
        for source in self.native_sources - previous:
            self.native_function_sources[source] = wire[len(CALL_PREFIX) :]
            uid = self.aligned_turn(payload["audio_end_sample"])
            if uid is not None:
                self.native_source_observations[source] = uid
                self.observed_consumed.add(uid)
            self.emit(
                "adapter.delegate.bound",
                source_utterance_id=source,
                call_id=self.native_function_sources[source],
                goal_rewritten=False,
                native_audio_end_sample=payload["audio_end_sample"],
            )

    def speak(
        self,
        backend=None,
        purpose="fast",
        task=None,
        source_utterance=None,
        user_text=None,
    ):
        source = source_utterance
        if task and task[0] in self.tasks:
            source = self.tasks[task[0]].source_utterance_id
        call = (
            self.decision_function_call
            or (self.task_function_calls.get(tuple(task)) if task else None)
            or self.native_function_sources.get(source)
        )
        if not call or call in self.returned_function_calls:
            self.emit(
                "adapter.unsupported_output",
                purpose=purpose,
                reason="Native VoiceChat accepts backend content only as a real pending function result",
            )
            return None
        if purpose in {"ack", "progress"}:
            self.emit(
                "adapter.function_result.deferred",
                purpose=purpose,
                reason="Pending native function needs substantive harness completion",
            )
            return None
        if (
            source_utterance
            and source_utterance != self.current_utterance
            or self.user_speaking
            or self.muted
        ):
            return None
        rid = self.function_delivery_foreground or self.response_id
        presentation = self.presentations.get(rid, {})
        if presentation.get("native_direct") and (
            not presentation.get("generated_done")
        ):
            self.response_id = rid
            self.send_venus(
                {
                    "type": "control",
                    "native_function_result": True,
                    "native_call_id": call,
                    "backend": backend or "",
                    "generation_epoch": self.generation_epoch,
                    "response_id": rid,
                }
            )
            self.returned_function_calls.add(call)
            presentation.update(
                purpose=purpose,
                task=task,
                approved_text=backend,
                authoritative_backend=bool(backend),
                source_utterance_id=source,
            )
            self.emit(
                "adapter.function_result.delivered",
                call_id=call,
                response_id=rid,
                native_turn_continues=True,
                generation_epoch=self.generation_epoch,
            )
            return rid
        previous, self.delivery_call = (self.delivery_call, call)
        try:
            response = super().speak(
                backend,
                purpose,
                task=task,
                source_utterance=source_utterance,
                user_text=user_text,
            )
            if response:
                self.returned_function_calls.add(call)
            return response
        finally:
            self.delivery_call = previous


class VoiceConnection:
    def __init__(self, connection):
        self.connection = connection
        self.input_sample = 0
        self.epoch = 0
        self.turn = 0
        self.idle = True
        self.drop_until_idle = False
        self.native_session = None
        self.pending_calls = set()
        self.backend_response = None
        self.next_backend_response = None
        self.pending_pcm = None
        self.tool_names = set()

    def begin_turn(self, native_bos=False):
        if self.idle or native_bos:
            self.turn += 1
            self.idle = False
            self.drop_until_idle = False
            self.backend_response = self.next_backend_response
            self.next_backend_response = None

    def step(self, **extra):
        response = (
            self.backend_response or f"voicechat:{self.native_session}:{self.turn}"
        )
        return {
            "type": "step",
            "response_id": response,
            "generation_epoch": self.epoch,
            "native_foreground": self.backend_response is None,
            "native_autonomy": True,
            "audio_end_sample": self.input_sample,
            "delegates": [],
            "text": "",
            "is_listen": self.idle,
            "end_of_turn": False,
            "sample_rate": 24000,
            "dropped_stale_epoch": self.drop_until_idle,
            **extra,
        }

    async def send(self, message):
        if isinstance(message, bytes):
            return await self.connection.send(message)
        value = json.loads(message)
        if value.get("type") == "start":
            tools = [*value.get("tools", []), DELEGATE]
            self.tool_names = {tool["function"]["name"] for tool in tools}
            await self.connection.send(
                json.dumps(
                    {
                        "type": "start",
                        "policy": value["system_prompt"] + "\n" + RESULT_POLICY,
                        "tools": tools,
                        "provider": "jarvis",
                        "seed": value.get("seed", 42),
                    }
                )
            )
        elif value.get("type") == "control":
            epoch = value.get("generation_epoch", self.epoch)
            if epoch > self.epoch:
                self.drop_until_idle = not self.idle
                self.epoch = epoch
            if value.get("start_response") or value.get("native_function_result"):
                call = value.get("native_call_id")
                if call not in self.pending_calls:
                    raise RuntimeError(
                        "Backend delivery has no matching pending native function call"
                    )
                self.pending_calls.remove(call)
                self.next_backend_response = value["response_id"]
                await self.connection.send(
                    json.dumps(
                        {
                            "type": "tool.result",
                            "call_id": call,
                            "result": {"content": value.get("backend", "")},
                        }
                    )
                )
        elif value.get("type") == "end":
            await self.connection.send(json.dumps(value))

    async def recv(self):
        while True:
            value = json.loads(await self.connection.recv())
            kind = value.get("type")
            if kind == "session":
                self.native_session = value["session_id"]
                return json.dumps(
                    {
                        "type": "ready",
                        "patches": {
                            "model": "NVIDIA-NemotronLabs-VoiceChat-11B",
                            "native_function_calls": True,
                            "decoder_math_modified_by_harness": False,
                            "external_hold_or_cancel_supported": False,
                            "native_session": value,
                        },
                    }
                )
            if kind == "audio":
                if self.pending_pcm is not None:
                    raise RuntimeError(
                        "Multiple native audio frames without a step boundary"
                    )
                self.pending_pcm = value["pcm"]
                continue
            if kind == "tool.call":
                if value.get("name") not in self.tool_names or not isinstance(
                    value.get("arguments"), dict
                ):
                    await self.connection.send(
                        json.dumps(
                            {
                                "type": "tool.result",
                                "call_id": value["call_id"],
                                "result": {
                                    "error": "Invalid native public function intent"
                                },
                            }
                        )
                    )
                    continue
                goal = (
                    value["arguments"].get("goal")
                    if value["name"] == "delegate"
                    else native_tool_goal(value["name"], value["arguments"])
                )
                if not isinstance(goal, str) or not goal.strip():
                    continue
                self.pending_calls.add(value["call_id"])
                return json.dumps(
                    self.step(
                        response_id=CALL_PREFIX + value["call_id"],
                        native_foreground=True,
                        delegates=[goal],
                        is_listen=True,
                    )
                )
            if kind == "event" and value.get("event", {}).get("type") == "native.step":
                event = value["event"]
                self.input_sample = event["audio_end_sample"]
                if "agent_idle" not in event or "agent_turn_ended" not in event:
                    raise RuntimeError(
                        "Actor must expose real upstream BOS/EOS/abort state boundaries"
                    )
                native_bos = event.get("text") == "<s>"
                if not event["agent_idle"]:
                    self.begin_turn(native_bos=native_bos)
                pcm, self.pending_pcm = (self.pending_pcm, None)
                raw_text = event.get("text", "")
                text = "" if raw_text in {"<s>", "</s>"} else raw_text
                result = self.step(
                    text=text,
                    native_text_delta=raw_text,
                    end_of_turn=event["agent_turn_ended"],
                    is_listen=event["agent_idle"]
                    and (not event["agent_turn_ended"])
                    and (not event.get("text"))
                    and (not pcm),
                    **{"pcm": pcm} if pcm is not None else {},
                    native_idle=event["agent_idle"],
                    native_turn_started=native_bos,
                    native_asr=event.get("asr", ""),
                    turn_boundary_source=event["turn_boundary_source"],
                    input_backlog_samples=event.get("input_backlog_samples", 0),
                    step_started_ns=event["step_started_ns"],
                    step_finished_ns=event["step_finished_ns"],
                )
                if event["agent_idle"]:
                    self.idle = True
                    self.drop_until_idle = False
                    self.backend_response = None
                return json.dumps(result)
            if kind == "error":
                return json.dumps(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except websockets.ConnectionClosedOK:
            raise StopAsyncIteration
