import json
import asyncio
import websockets
from jarvis.audio.native_observer import ObserverRoutingMixin
from jarvis.core.runtime import SessionRuntime
from jarvis.audio.admission import NeutralController


class MiniConnection:
    def __init__(self, connection):
        self.connection = connection
        self.backend_responses = set()

    async def send(self, message):
        if isinstance(message, bytes):
            return await self.connection.send(message)
        value = json.loads(message)
        if value.get("type") == "start":
            value["system_prompt"] = "Streaming Omni Conversation. " + value[
                "system_prompt"
            ].removeprefix("Streaming Omni Conversation. ")
        elif value.get("type") == "control":
            if value.get("response_id") and value.get("start_response"):
                self.backend_responses.add(value["response_id"])
            approved = value.pop("approved_text", None)
            if approved and (not value.get("backend")):
                value["backend"] = approved
            value.pop("reset_context", None)
        await self.connection.send(json.dumps(value))

    async def recv(self):
        value = json.loads(await self.connection.recv())
        if value.get("type") == "step":
            value["native_foreground"] = (
                value.get("response_id") not in self.backend_responses
            )
            value["delegates"] = []
        return json.dumps(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except websockets.ConnectionClosedOK:
            raise StopAsyncIteration


class MiniCPMRuntime(ObserverRoutingMixin, SessionRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.neutral = NeutralController(self.worker, {"tools": self.tools.cards})
        self.admission_lock = asyncio.Lock()
        self.admission_observed = set()
        self.admission_dispatched = set()

    async def handle(self, kind, payload):
        if kind == "adapter.admission.result":
            uid, decision = payload["utterance_id"], payload["decision"]
            observed = self.observed_turns.get(uid, {}).get("final")
            if (
                decision["action"] == "NONE"
                or not observed
                or uid in self.observed_deleted
                or uid in self.admission_dispatched
            ):
                return
            self.admission_dispatched.add(uid)
            self.observed_consumed.add(uid)
            await self.dispatch_native(
                {
                    "text": observed.text,
                    "response_id": "admission-" + uid,
                    "audio_end_sample": observed.audio_end_sample,
                    "_bound": True,
                    "_observation_id": uid,
                }
            )
            return
        await super().handle(kind, payload)
        if (
            kind == "asr.final"
            and payload["utterance_id"] not in self.admission_observed
        ):
            self.admission_observed.add(payload["utterance_id"])

            async def decide():
                async with self.admission_lock:
                    decision = await self.neutral.decide(payload["text"])
                self.post(
                    "adapter.admission.result",
                    {"utterance_id": payload["utterance_id"], "decision": decision},
                )

            self.spawn(decide())
