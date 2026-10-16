import argparse
from pathlib import Path
from types import SimpleNamespace

from jarvis.paths import model_path, data_root, weight


PORTS = {
    "venus": 8766,
    "venus-omni": 8766,
    "minicpm": 8766,
    "voicechat": 8766,
    "asr": 8767,
    "jev-omni": 8810,
    "vision": 8811,
    "hud": 8812,
    "life": 8813,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=PORTS)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    name = args.component
    directory = args.model_dir or model_path(
        "jev-omni" if name in {"hud", "life"} else name
    )
    if not directory.is_dir():
        parser.error(f"Model directory is missing: {directory}")
    if name == "venus":
        from jarvis.adapters.venus_audio import create_app

        app = create_app(str(directory))
    elif name == "venus-omni":
        from jarvis.adapters.omni.venus_audio import create_app
        from jarvis.adapters.omni.venus_omni_native import NativeVenusOmni

        app = create_app(
            str(directory),
            model_factory=lambda path: NativeVenusOmni(
                path, weight("omni"), engagement_policy=True
            ),
            model_name="Realtime-Venus-Omni",
            owner_protocol="omni_native_route_v1",
            transport_limits={"incoming": 200, "controls": 128, "outgoing": 128},
            aligned_keep_latest=True,
        )
    elif name == "minicpm":
        from jarvis.adapters.minicpm import create_app

        app = create_app(SimpleNamespace(model_dir=str(directory)))
    elif name == "voicechat":
        from aiohttp import web
        from jarvis.adapters.voicechat import create_app

        options = SimpleNamespace(
            model_dir=str(directory),
            upstream=str(data_root() / "sources/voicechat"),
            engine_type="native",
            torch_threads=4,
            quiet_steps=True,
            converted_llm=None,
            converted_eartts=None,
            max_model_len=None,
        )
        web.run_app(create_app(options), host=args.host, port=args.port or PORTS[name])
        return
    elif name == "asr":
        from jarvis.adapters.asr import create_app

        app = create_app(str(directory), silence_ms=1100, threshold=0.3)
    elif name == "jev-omni":
        from jarvis.adapters.jev_omni import create_app
        from jarvis.adapters.jev_omni_fast import Owner

        app = create_app(directory, owner_factory=Owner)
    elif name == "vision":
        from jarvis.adapters.screen_gemma import create_app

        app = create_app(directory)
    else:
        from jarvis.adapters.jev_omni import create_app

        if name == "hud":
            from jarvis.adapters.jev_game_facts import factory
        else:
            from jarvis.adapters.jev_life_state import factory
        app = create_app(directory, owner_factory=factory(weight(name)))
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port or PORTS[name])


if __name__ == "__main__":
    main()
