import asyncio
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit, urlunsplit

import httpx

from jarvis.paths import resource, weight


def health_url(endpoint):
    url = urlsplit(endpoint)
    return urlunsplit(
        (
            "https" if url.scheme in {"wss", "https"} else "http",
            url.netloc,
            "/health",
            "",
            "",
        )
    )


async def doctor(config):
    checks = []
    required = [config["worker"].get("api_key_env", "WORKER_API_KEY")]
    if config["jev"].get("backend") != "local":
        required.append("TYPESAFE_API_KEY")
    if config["mode"] == "omni":
        required += ["GEMINI_API_KEY", "GEMINI_BASE_URL", "GEMINI_MODEL"]
    for name in dict.fromkeys(required):
        checks.append(
            (name, bool(os.getenv(name)), "set" if os.getenv(name) else "missing")
        )
    for name in ["audio", "audio-game", "omni", "hud", "life"]:
        checks.append((f"weight/{name}", weight(name).is_file(), "available"))
    checks.append(
        ("browser files", resource("frontend/index.html").is_file(), "available")
    )
    endpoints = {
        "frontend": config["endpoints"]["venus"],
        "asr": config["endpoints"]["asr"],
    }
    if config["jev"].get("backend") == "local":
        endpoints["jev"] = config["jev"]["endpoint"]
    if config["mode"] == "omni":
        endpoints.update(
            vision=config["screen"]["extractor_endpoint"],
            jev_omni=config["screen"]["decisions"]["endpoint"],
        )
        checks.append(("ffmpeg", shutil.which("ffmpeg") is not None, "on PATH"))
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:

        async def inspect(name, endpoint):
            try:
                response = await client.get(health_url(endpoint))
                response.raise_for_status()
                ready = response.json().get("ready") is True
                return name, ready, "ready" if ready else "loading"
            except (httpx.HTTPError, ValueError):
                return name, False, "unavailable"

        checks += await asyncio.gather(
            *(inspect(name, endpoint) for name, endpoint in endpoints.items())
        )
    if config.get("game_name") == "pvz":
        checks.append(
            (
                "PvZ",
                Path(
                    config["game_expansion"]["pvz_web_root"], "game/index.html"
                ).is_file(),
                "installed",
            )
        )
    if config.get("game_name") == "voxel":
        checks.append(
            (
                "VoxeLibre bridge",
                Path(config["voxel"]["secret_file"]).is_file(),
                "initialized",
            )
        )
    for name, ready, detail in checks:
        print(f"{'OK' if ready else 'MISSING':7} {name}: {detail}")
    return 0 if all(ready for _, ready, _ in checks) else 1
