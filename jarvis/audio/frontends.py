from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

import websockets


@dataclass(frozen=True)
class FrontendCapabilities:
    admission: str
    backend_channel: str
    native_hold: bool
    native_cancel: bool
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000


CAPABILITIES = {
    "venus": FrontendCapabilities("native_gate", "backend_text", True, True),
    "minicpm": FrontendCapabilities("causal_asr", "text_prefill", True, False),
    "voicechat": FrontendCapabilities(
        "native_function", "function_result", False, False
    ),
    "venus-omni": FrontendCapabilities("native_gate", "backend_text", True, True),
}


class FrontendConnection(Protocol):
    async def send(self, message: bytes | str) -> None: ...
    async def recv(self) -> str: ...


@asynccontextmanager
async def connect(frontend, endpoint):
    async with websockets.connect(
        endpoint, max_size=8_000_000, proxy=None
    ) as connection:
        if frontend == "minicpm":
            from jarvis.audio.minicpm import MiniConnection

            yield MiniConnection(connection)
        elif frontend == "voicechat":
            from jarvis.audio.voicechat import VoiceConnection

            yield VoiceConnection(connection)
        else:
            yield connection


def runtime_class(frontend, game=False):
    from jarvis.audio.native_observer import runtime_factory

    if frontend == "venus":
        return runtime_factory(game)
    if frontend == "minicpm":
        from jarvis.audio.minicpm import MiniCPMRuntime

        base = MiniCPMRuntime
    elif frontend == "voicechat":
        from jarvis.audio.voicechat import VoiceChatRuntime

        base = VoiceChatRuntime
    else:
        raise ValueError("Unknown audio frontend")
    if game:
        from jarvis.games.runtime import ExpansionRuntime

        class GameRuntime(base, ExpansionRuntime):
            pass

        return GameRuntime
    return base
