import copy
import hashlib
import json
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from jarvis.paths import RESOURCE_ROOT, data_root, model_path

ROOT = data_root()


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path=None, *, mode=None, frontend=None, game=None):
    load_dotenv(Path.cwd() / ".env", override=False)
    config = yaml.safe_load((RESOURCE_ROOT / "configs/defaults.yaml").read_text())
    custom = {}
    if path:
        custom = yaml.safe_load(Path(path).expanduser().resolve().read_text()) or {}
        if not isinstance(custom, dict):
            raise ValueError("Configuration must be a mapping")
    selected_mode = mode or custom.get("mode", config["mode"])
    selected_frontend = frontend or custom.get("frontend", config["frontend"])
    selected_game = game if game is not None else custom.get("game_name")
    if selected_mode not in {"audio", "omni"}:
        raise ValueError("Mode must be audio or omni")
    if selected_mode == "omni":
        selected_frontend = "venus-omni"
        config = merge(
            config, yaml.safe_load((RESOURCE_ROOT / "configs/omni.yaml").read_text())
        )
    elif selected_frontend not in {"venus", "minicpm", "voicechat"}:
        raise ValueError("Audio frontend must be venus, minicpm or voicechat")
    if selected_game and selected_game not in {"pvz", "voxel", "cs2", "lol"}:
        raise ValueError("Game must be pvz, voxel, cs2 or lol")
    if selected_game in {"cs2", "lol"} and selected_mode != "omni":
        raise ValueError("CS2 and LoL use --mode omni")
    if selected_game in {"pvz", "voxel"}:
        if selected_mode != "audio":
            raise ValueError("PvZ and VoxeLibre use --mode audio")
        config = merge(
            config, yaml.safe_load((RESOURCE_ROOT / "configs/games.yaml").read_text())
        )
        config["game_expansion"]["default_game"] = (
            "pvz_web" if selected_game == "pvz" else "voxel"
        )
        config["voxel"]["enabled"] = selected_game == "voxel"
        config["server"]["port"] = 8785 if selected_game == "pvz" else 8795
    config = merge(config, custom)
    config.update(
        mode=selected_mode, frontend=selected_frontend, game_name=selected_game
    )
    root = data_root()
    config.update(root=str(root), assets=str(RESOURCE_ROOT), policy_version="default")
    config["jev"]["backend"] = os.getenv(
        "JEV_BACKEND", config["jev"].get("backend", "typesafe")
    )
    config["jev"]["endpoint"] = os.getenv(
        "JEV_ENDPOINT", config["jev"].get("endpoint", "http://127.0.0.1:8810")
    )
    if config["jev"]["backend"] not in {"typesafe", "local"}:
        raise ValueError("JEV backend must be typesafe or local")
    config["endpoints"] = {
        "venus": os.getenv("FRONTEND_ENDPOINT", config["endpoints"]["venus"]),
        "asr": os.getenv("ASR_ENDPOINT", config["endpoints"]["asr"]),
    }
    worker = config["worker"]
    worker["base_url"] = os.getenv("WORKER_BASE_URL", worker["base_url"])
    worker["model"] = os.getenv("WORKER_MODEL", worker["model"])
    config["models"] = {
        "venus": str(model_path(selected_frontend)),
        "asr": str(model_path("asr")),
        "jev": os.getenv("JEV_MODEL", "jev-latest"),
        "deepseek": worker["model"],
        "gemini": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
    }
    config["asr"]["model_dir"] = str(model_path("asr"))
    config["native_routing"]["prompt_file"] = str(RESOURCE_ROOT / "prompts/audio.txt")
    if selected_frontend in {"minicpm", "voicechat"}:
        config["venus"]["verbatim_backend"] = False
    for section, field in [("runtime", "database"), ("tools", "artifact_directory")]:
        value = Path(config[section][field]).expanduser()
        config[section][field] = str(value if value.is_absolute() else root / value)
    if config["tools"].get("documents"):
        config["tools"]["documents"] = str(
            Path(config["tools"]["documents"]).expanduser().resolve()
        )
    if selected_mode == "omni":
        screen = config["screen"]
        screen["game"] = selected_game or screen.get("game", "cs2")
        screen["extractor_endpoint"] = os.getenv(
            "VISION_ENDPOINT", screen["extractor_endpoint"]
        )
        screen["decisions"]["endpoint"] = os.getenv(
            "JEV_OMNI_ENDPOINT", screen["decisions"]["endpoint"]
        )
        screen["audio_endpoints"] = dict(config["endpoints"])
        screen["media_directory"] = str(root / "screen/media")
        for section in ["native_fast", "visual_review"]:
            screen[section]["policy"] = str(
                RESOURCE_ROOT / "prompts" / Path(screen[section]["policy"]).name
            )
        screen["visual_review"].pop("route_policy", None)
        screen["tactical_reply"]["intent_endpoint"] = screen["extractor_endpoint"]
        readers = [
            ("hud", screen["scene_dialogue"].get("fact_reader", {})),
            ("life", screen.get("life_state", {})),
        ]
        for name, settings in readers:
            if settings.get("enabled"):
                settings["adapter_sha256"] = json.loads(
                    (RESOURCE_ROOT / "weights" / (name + ".json")).read_text()
                )["sha256"]
                if name == "hud":
                    protocol = RESOURCE_ROOT / "prompts/visual-facts.json"
                    settings["protocol"] = str(protocol)
                    settings["protocol_sha256"] = hashlib.sha256(
                        protocol.read_bytes()
                    ).hexdigest()
    if selected_game in {"pvz", "voxel"}:
        config["game_expansion"]["pvz_web_root"] = str(root / "local_games/pvz_web")
        config["game_expansion"]["pvz_web_license_confirmed"] = (
            root / "local_games/pvz_web/game/index.html"
        ).is_file()
        config["game_expansion"]["audio_endpoints"] = dict(config["endpoints"])
        config["voxel"]["secret_file"] = str(root / ".runtime/voxel/bridge.secret")
    if (
        config["runtime"]["max_active_workers"] < 1
        or config["runtime"]["max_queued_tasks"] < 0
    ):
        raise ValueError(
            "Worker counts must be positive, with a nonnegative queue size"
        )
    if not 0 < config["jev"]["control_accept"] <= 1:
        raise ValueError("JEV confidence threshold must be in (0, 1]")
    return config


def session_config(config, version=None):
    result = copy.deepcopy(config)
    if result.get("screen", {}).get("enabled"):
        result = merge(result, result["screen"].get("session_overrides", {}))
    return result


def asset_dir(version=None):
    return RESOURCE_ROOT
