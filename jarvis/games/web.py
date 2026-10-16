from pathlib import Path
import asyncio
import contextlib
from fastapi import WebSocket, WebSocketDisconnect
from jarvis.games.protocol import MAX_MESSAGE_BYTES, decode
from jarvis.games.remote import BridgeError


def attach_bridge(app, registry):

    @app.websocket("/ws/game-bridge")
    async def game_bridge(ws: WebSocket):
        auth = ws.headers.get("authorization", "")
        adapter = registry.find(auth[7:]) if auth.startswith("Bearer ") else None
        if adapter is None or adapter.connected or adapter.transport_owner is not None:
            await ws.close(code=1008, reason="Pair an available voice session first")
            return
        owner = object()
        adapter.transport_owner = owner
        generation = adapter.active_game_epoch
        critical = asyncio.Queue(maxsize=32)

        async def send(message):
            try:
                critical.put_nowait(message)
            except asyncio.QueueFull:
                adapter.disconnect("critical_queue_full")
                raise BridgeError("Critical queue overflow")

        adapter.send = send

        async def writer():
            while True:
                await ws.send_json(await critical.get())

        async def lease():
            while True:
                await asyncio.sleep(0.5)
                if adapter.active_game_epoch != generation:
                    await ws.close(code=1000, reason="Active game changed")
                    return
                if adapter.connected and (not adapter.check_lease()):
                    await ws.close(code=1011, reason="Game heartbeat expired")
                    return

        jobs = [asyncio.create_task(writer()), asyncio.create_task(lease())]
        try:
            await ws.accept()
            hello = await asyncio.wait_for(ws.receive_text(), 5)
            envelope = decode(hello)
            if envelope.type != "hello":
                raise BridgeError("Expected hello")
            await adapter.receive(envelope.model_dump(mode="json"))
            while adapter.active_game_epoch == generation:
                raw = await ws.receive_text()
                if len(raw.encode()) > MAX_MESSAGE_BYTES:
                    raise BridgeError("Message too large")
                await adapter.receive(raw)
                for task in jobs:
                    if task.done():
                        task.result()
        except (WebSocketDisconnect, TimeoutError):
            pass
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close(code=1008, reason="Invalid bridge message")
        finally:
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            if adapter.transport_owner is owner:
                adapter.transport_owner = None
                if adapter.active_game_epoch == generation:
                    adapter.disconnect()


def attach_text(app, config, memory, busy, registry):

    @app.websocket("/games/text")
    async def text_session(ws: WebSocket):
        await ws.accept()
        if busy.locked():
            await ws.close(code=1013)
            return
        from jarvis.config import session_config
        from jarvis.core.schemas import new_id
        from jarvis.core.trace import Trace
        from jarvis.games.runtime import ExpansionRuntime

        async with busy:
            identifier = new_id()
            directory = (
                Path(config["root"])
                / "runs/game_expansion"
                / ("text_" + identifier[:8])
            )
            trace = Trace(directory, identifier)
            outgoing = asyncio.Queue()
            runtime = None
            jobs = []
            if config.get("voxel", {}).get("enabled"):
                from jarvis.games.voxel.mailbox import SnapshotQueue

                outgoing = SnapshotQueue()
            try:
                hello = await asyncio.wait_for(ws.receive_json(), 10)
                if hello.get("type") != "start":
                    raise BridgeError("Expected start")
                cfg = session_config(config)
                cfg["game_expansion"]["text_mode"] = True
                cfg["speech_check"]["enabled"] = False
                if cfg.get("voxel", {}).get("enabled"):
                    cfg["voxel"]["browser_view"] = True
                trace.manifest(
                    session_id=identifier,
                    mode="text_real_game_and_models",
                    audio_verified=False,
                )
                runtime = ExpansionRuntime(
                    cfg,
                    identifier,
                    str(hello.get("user_id", "development"))[:128],
                    trace,
                    outgoing.put_nowait,
                    lambda _: None,
                    memory,
                )
                runtime.registry = registry

                async def sender():
                    while True:
                        await ws.send_json(await outgoing.get())

                jobs = [
                    asyncio.create_task(sender()),
                    asyncio.create_task(runtime.run()),
                ]
                await ws.send_json(
                    {"type": "session", "session_id": identifier, "mode": "text"}
                )
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "end":
                        break
                    if message.get("type") in {
                        "game.select",
                        "game.bridge.receive",
                        "game.command",
                        "game.advice.seen",
                        "stop_speaking",
                        "present_result",
                        "remote.advice.seen",
                    }:
                        runtime.post(message["type"], message.get("payload", {}))
            except WebSocketDisconnect:
                pass
            except Exception as error:
                trace.emit("session.failed", error=type(error).__name__)
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                if runtime:
                    await runtime.close()
                trace.emit("session.closed", audio_verified=False)
                trace.close()
