from __future__ import annotations
import asyncio
import contextlib
import json
import time
from pathlib import Path
import numpy as np
import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jarvis.config import session_config
from jarvis.paths import RESOURCE_ROOT
from jarvis.core.secrets import redact
from jarvis.core.memory import MemoryStore
from jarvis.core.schemas import new_id
from jarvis.core.trace import Trace


def create_app(config):
    ROOT = Path(config["root"])

    @contextlib.asynccontextmanager
    async def lifespan(app):
        queue = worker = None
        if config.get("screen", {}).get("enabled") and config["memory"].get("enabled"):
            from jarvis.omni.personal_memory import MemoryExtractionQueue
            from jarvis.core.worker import DeepSeekWorker

            worker = DeepSeekWorker(
                session_config(config), None, lambda *args, **kwargs: None
            )
            queue = memory.personal_extractions = MemoryExtractionQueue(
                memory, worker, config["screen"].get("personal_memory_queue", {})
            )
            queue.start()
        try:
            yield
        finally:
            if queue:
                await queue.stop()
                del memory.personal_extractions
            if worker:
                await worker.close()

    app = FastAPI(title="Jarvis", lifespan=lifespan)
    app.mount(
        "/static", StaticFiles(directory=RESOURCE_ROOT / "frontend"), name="static"
    )
    memory = MemoryStore(ROOT / config["runtime"]["database"])
    app.state.memory = memory
    app.state.config = config
    busy = asyncio.Lock()
    screen_recordings = None
    if config.get("screen", {}).get("enabled"):
        from jarvis.omni.web import Recordings

        screen_recordings = Recordings()
    game_pairs = None
    if config.get("game_expansion", {}).get("enabled"):
        from jarvis.games.remote import PairingRegistry
        from jarvis.games.web import attach_bridge, attach_text

        game_pairs = PairingRegistry()
        attach_bridge(app, game_pairs)
        attach_text(app, config, memory, busy, game_pairs)
        if config.get("voxel", {}).get("enabled"):
            from jarvis.games.voxel.adapter import attach as attach_voxel

            attach_voxel(app, game_pairs, config)

    @app.get("/")
    async def index():
        if config.get("screen", {}).get("enabled"):
            return FileResponse(RESOURCE_ROOT / "frontend/screen/index.html")
        if game_pairs:
            if config.get("voxel", {}).get("enabled"):
                from fastapi.responses import HTMLResponse

                html = (RESOURCE_ROOT / "frontend/expansion.html").read_text()
                html = html.replace(
                    "</head>",
                    '<link rel="stylesheet" href="/static/games/voxel/panel.css"></head>',
                )
                html = html.replace(
                    '<option value="pvz_web">',
                    '<option value="voxel">VoxeLibre 开源生存沙盒</option><option value="pvz_web">',
                )
                return HTMLResponse(html)
            return FileResponse(RESOURCE_ROOT / "frontend/expansion.html")
        return FileResponse(RESOURCE_ROOT / "frontend/index.html")

    if game_pairs:
        from fastapi.responses import HTMLResponse

        pvz_root = ROOT / config["game_expansion"].get(
            "pvz_web_root", "local_games/pvz_web"
        )
        if (
            config["game_expansion"].get("pvz_web_license_confirmed")
            and (pvz_root / "game/index.html").is_file()
        ):
            from fastapi.responses import Response
            from jarvis.games.pvz_assets import (
                cfunction,
                tutorial_level,
                custom_audio,
                ALIASES,
            )

            @app.get("/games/pvz_web/game/js/Cfunction.js")
            async def pvz_functions():
                return Response(
                    cfunction((pvz_root / "game/js/Cfunction.js").read_text()),
                    media_type="text/javascript",
                )

            @app.get("/games/pvz_web/game/level/1.js")
            async def pvz_tutorial():
                return Response(
                    tutorial_level((pvz_root / "game/level/1.js").read_text()),
                    media_type="text/javascript",
                )

            def asset_route(target):

                async def asset():
                    return FileResponse(pvz_root / target)

                return asset

            for alias, target in ALIASES.items():
                if (pvz_root / target).is_file():
                    app.get("/games/pvz_web/" + alias)(asset_route(target))

            @app.get("/games/pvz_web/game/index.html")
            async def pvz_page():
                html = (pvz_root / "game/index.html").read_text()
                html = html.replace('<script src="js/Cheatcodes.js"></script>', "")
                html = html.replace(
                    '<script async defer src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script>',
                    "",
                )
                html = html.replace(
                    "</body>",
                    '<script type="module" src="/static/games/pvz/adapter.mjs"></script></body>',
                )
                return HTMLResponse(
                    html,
                    headers={
                        "Content-Security-Policy": "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' data: blob:; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'",
                        "Referrer-Policy": "no-referrer",
                        "Permissions-Policy": "microphone=(), camera=(), geolocation=()",
                    },
                )

            @app.get("/games/pvz_web/game/js/Custom.js")
            async def pvz_custom_audio():
                return Response(
                    custom_audio((pvz_root / "game/js/Custom.js").read_text()),
                    media_type="application/javascript",
                )

            app.mount(
                "/games/pvz_web", StaticFiles(directory=pvz_root), name="pvz_assets"
            )

    @app.get("/health")
    async def health():
        return {
            "ready": True,
            "busy": busy.locked(),
            "frontend": config["frontend"],
            "mode": config["mode"],
            "policy_version": session_config(
                config, config["runtime"].get("assets_version")
            )["policy_version"],
        }

    @app.get("/artifacts/{token}/{filename}")
    async def artifact(token: str, filename: str, preview: bool = False):
        from jarvis.core.artifacts import artifact_path

        root = ROOT / config["tools"].get("artifact_directory", "data/artifacts")
        try:
            path = artifact_path(root, token, filename)
        except ValueError:
            raise HTTPException(404, "Artifact not found")
        if not path.is_file():
            raise HTTPException(404, "Artifact not found")
        if preview and path.suffix.lower() == ".html":
            return FileResponse(
                path,
                media_type="text/html",
                headers={
                    "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )
        return FileResponse(
            path,
            filename=filename,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.websocket("/ws")
    async def session(ws: WebSocket):
        if config.get("screen", {}).get("enabled"):
            from jarvis.omni.web import session as screen_session

            return await screen_session(ws, config, memory, busy, screen_recordings)
        await ws.accept()
        if busy.locked():
            await ws.close(code=1013, reason="A single-user session is already active")
            return
        async with busy:
            session_id = new_id()
            directory = (
                ROOT / "runs" / (time.strftime("%Y%m%d_%H%M%S") + "_" + session_id[:8])
            )
            trace = Trace(directory, session_id)
            cfg = session_config(config)
            outgoing, venus_out, asr_out = (
                asyncio.Queue(),
                asyncio.PriorityQueue(),
                asyncio.Queue(),
            )
            if cfg.get("voxel", {}).get("enabled"):
                from jarvis.games.voxel.mailbox import SnapshotQueue

                outgoing = SnapshotQueue()
            outbound_seq = 0
            session_started = time.monotonic()
            runtime = None
            tasks = []

            def send_client(message):
                outgoing.put_nowait(message)

            def send_venus(message):
                nonlocal outbound_seq
                outbound_seq += 1
                venus_out.put_nowait((0, outbound_seq, message))

            async def client_sender():
                while True:
                    await ws.send_json(await outgoing.get())

            try:
                hello = await asyncio.wait_for(ws.receive_json(), 20)
                if hello.get("type") != "start":
                    raise ValueError("Expected session start")
                user_id = str(hello.get("user_id", "local_user"))[:128]
                language = hello.get("language", "auto")
                if language not in {"auto", "en", "zh"}:
                    raise ValueError("Unsupported conversation language")
                cfg["speech_language"] = language
                native = cfg.get("native_routing", {}).get("enabled", False)
                asr_endpoint = cfg["endpoints"]["asr"]
                if language != "auto":
                    asr_endpoint += (
                        ("&" if "?" in asr_endpoint else "?") + "language=" + language
                    )
                asr_context = (
                    cfg.get("game_expansion", {}).get("asr_context")
                    if game_pairs
                    else None
                )
                if asr_context:
                    from urllib.parse import urlencode

                    asr_endpoint += ("&" if "?" in asr_endpoint else "?") + urlencode(
                        {"context": asr_context}
                    )
                trace.manifest(
                    session_id=session_id,
                    user_id=user_id,
                    mode="live",
                    policy_version=cfg["policy_version"],
                    models=cfg["models"],
                    audio_input="microphone_or_realtime_replay",
                    model_weights_frozen=True,
                )
                async with contextlib.AsyncExitStack() as stack:
                    from jarvis.audio.frontends import connect

                    venus = await stack.enter_async_context(
                        connect(cfg["frontend"], cfg["endpoints"]["venus"])
                    )
                    asr = None
                    if not native or cfg.get("native_routing", {}).get("asr_observer"):
                        asr = await stack.enter_async_context(
                            websockets.connect(asr_endpoint, max_size=2000000)
                        )
                        ready = json.loads(await asyncio.wait_for(asr.recv(), 20))
                        if ready.get("type") != "ready":
                            raise RuntimeError("ASR session unavailable")
                    system_prompt = (
                        Path(cfg["assets"]) / "prompts/venus_system.md"
                    ).read_text()
                    if native:
                        system_prompt = (
                            ROOT / cfg["native_routing"]["prompt_file"]
                        ).read_text()
                        if cfg["native_routing"].get("delegated_continuity"):
                            from jarvis.core.conversation_policy import (
                                NATIVE_CONVERSATION,
                            )

                            system_prompt += "\n" + NATIVE_CONVERSATION
                        if game_pairs:
                            game_name = cfg.get("game_expansion", {}).get(
                                "default_game", "pvz_web"
                            )
                            system_prompt += (
                                "\nYou are helping the user play "
                                + game_name
                                + ". You cannot see or control the game yourself. For ANY question about current game state, strategy, inventory or action, including stopping/reclaiming controls, delegate the user request to the game harness. Never merely promise to act."
                            )
                    profile = (
                        await memory.profile(user_id)
                        if cfg["memory"].get("profile_context")
                        else []
                    )
                    if cfg["memory"].get("profile_context") and (profile or not native):
                        system_prompt += (
                            "\nSaved preferences from earlier conversations (apply when relevant): "
                            + json.dumps(
                                [
                                    {
                                        "id": m["id"],
                                        "text": m.get("english_text") or m["text"],
                                    }
                                    for m in profile
                                ],
                                ensure_ascii=False,
                            )
                            if native
                            else "\n" + memory.profile_prompt(profile)
                        )
                    if language != "auto":
                        system_prompt += "\nReply in " + (
                            "English." if language == "en" else "Chinese."
                        )
                    from jarvis.core.tools import ToolRegistry

                    await venus.send(
                        json.dumps(
                            {
                                "type": "start",
                                "hold": not native,
                                "native_autonomy": native,
                                "seed": 42,
                                "tools": ToolRegistry(cfg["tools"]).schemas(
                                    ["read_only", "artifact_write"]
                                ),
                                "generation_options": {
                                    **cfg.get("native_routing", {}).get(
                                        "generation_options", {}
                                    ),
                                    "game_identity": {
                                        "pvz_web": "pvz",
                                        "voxel": "voxel",
                                    }.get(
                                        cfg.get("game_expansion", {}).get(
                                            "default_game"
                                        ),
                                        "none",
                                    )
                                    if game_pairs
                                    else "none",
                                },
                                "batch_system_prefill": cfg["venus"].get(
                                    "batch_system_prefill", False
                                ),
                                "incremental_tts": cfg["venus"].get(
                                    "incremental_tts", True
                                ),
                                "tts_drain_units": cfg["venus"].get(
                                    "tts_drain_units", False
                                ),
                                "system_prompt": system_prompt,
                            }
                        )
                    )
                    ready = json.loads(await asyncio.wait_for(venus.recv(), 90))
                    if ready.get("type") != "ready":
                        raise RuntimeError(
                            "Venus prepare failed: "
                            + str(ready.get("error", ready.get("type")))
                        )
                    prepared = trace.emit(
                        "venus.prepared",
                        patches=ready.get("patches", {}),
                        incremental_tts=cfg["venus"].get("incremental_tts", True),
                    )
                    send_client(
                        {"type": "event", "event": prepared.model_dump(mode="json")}
                    )
                    from jarvis.audio.frontends import runtime_class

                    factory = runtime_class(cfg["frontend"], bool(game_pairs))
                    runtime = factory(
                        cfg, session_id, user_id, trace, send_client, send_venus, memory
                    )
                    if game_pairs:
                        runtime.registry = game_pairs
                    runtime.profile = profile

                    async def erase_recordings():
                        trace.erase()
                        return 1

                    runtime.erase_recordings = erase_recordings
                    if cfg["memory"].get("profile_context"):
                        runtime.emit(
                            "memory.profile.loaded",
                            memory_ids=[m["id"] for m in profile],
                            phase="session_start",
                        )

                    async def venus_sender():
                        while True:
                            _, _, message = await venus_out.get()
                            await venus.send(
                                message
                                if isinstance(message, bytes)
                                else json.dumps(message, ensure_ascii=False)
                            )

                    async def asr_sender():
                        while True:
                            await asr.send(await asr_out.get())

                    async def venus_receiver():
                        async for raw in venus:
                            msg = json.loads(raw)
                            type = msg.pop("type")
                            if type == "step":
                                runtime.post("venus.step", msg)
                            elif type == "error":
                                raise RuntimeError(
                                    "Venus owner: " + msg.get("error", "unknown error")
                                )
                            elif type == "context.reset":
                                runtime.post("history.reset.ready", msg)
                            else:
                                runtime.emit("venus." + type, **msg)

                    async def asr_receiver():
                        async for raw in asr:
                            msg = json.loads(raw)
                            runtime.post(msg["type"], msg.get("payload", {}))

                    channels = [
                        client_sender,
                        venus_sender,
                        venus_receiver,
                        runtime.run,
                    ]
                    if asr is not None:
                        channels.extend([asr_sender, asr_receiver])
                    tasks = [asyncio.create_task(fn()) for fn in channels]
                    runtime.emit(
                        "native.routing.config",
                        enabled=native,
                        asr_connected=asr is not None,
                    )
                    session_started = time.monotonic()
                    session_message = {
                        "type": "session",
                        "session_id": session_id,
                        "generation_epoch": 0,
                        "input_sample_rate": 16000,
                        "output_sample_rate": 24000,
                        "frontend": cfg["frontend"],
                    }
                    send_client(session_message)
                    while True:
                        incoming = await ws.receive()
                        for task in tasks:
                            if task.done():
                                task.result()
                                raise RuntimeError("A session channel ended")
                        if incoming["type"] == "websocket.disconnect":
                            break
                        if incoming.get("bytes") is not None:
                            data = incoming["bytes"]
                            pcm = np.frombuffer(data, dtype="<f4").copy()
                            if len(pcm) > 16000 or not np.isfinite(pcm).all():
                                raise ValueError("Invalid audio frame")
                            outbound_seq += 1
                            venus_out.put_nowait((1, outbound_seq, data))
                            if asr is not None:
                                asr_out.put_nowait(data)
                        elif incoming.get("text"):
                            message = json.loads(incoming["text"])
                            message_type = message.get("type")
                            if message_type == "playback.ack":
                                runtime.post("playback.ack", message["payload"])
                            elif message_type in {
                                "stop_speaking",
                                "feedback",
                                "present_result",
                            }:
                                runtime.post(message_type, message.get("payload", {}))
                            elif game_pairs and message_type in {
                                "game.select",
                                "game.bridge.receive",
                                "game.command",
                                "game.advice.seen",
                                "remote.advice.seen",
                            }:
                                runtime.post(message_type, message.get("payload", {}))
                            elif message_type == "playback.event":
                                if message.get("payload", {}).get("type") in {
                                    "assistant.playback.started",
                                    "assistant.playback.ended",
                                    "assistant.playback.cleared",
                                }:
                                    runtime.post(message_type, message["payload"])
                            elif message_type == "end":
                                break
                            else:
                                runtime.emit(
                                    "client.message_rejected",
                                    message_type=str(message_type),
                                )
            except WebSocketDisconnect:
                pass
            except Exception as error:
                trace.emit(
                    "session.failed",
                    error=type(error).__name__,
                    message=redact(str(error))[:400],
                )
                with contextlib.suppress(Exception):
                    await ws.send_json(
                        {"type": "error", "message": redact(str(error))[:300]}
                    )
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if runtime:
                    await runtime.close()
                trace.emit("session.closed")
                trace.close()

    return app
