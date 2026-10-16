import argparse
import asyncio
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="jarvis", description="Full-duplex audio and omni agents"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["serve", "doctor"]:
        p = commands.add_parser(name)
        p.add_argument("--config")
        p.add_argument("--mode", choices=["audio", "omni"])
        p.add_argument("--frontend", choices=["venus", "minicpm", "voicechat"])
        p.add_argument("--game", choices=["pvz", "voxel", "cs2", "lol"])
        if name == "serve":
            p.add_argument("--host")
            p.add_argument("--port", type=int)
    setup = commands.add_parser("setup")
    setup.add_argument(
        "component",
        choices=["venus", "asr", "minicpm", "voicechat", "omni", "pvz", "voxel"],
    )
    setup.add_argument("--python")
    setup.add_argument("--jobs", type=int, default=8)
    models = commands.add_parser("models").add_subparsers(dest="action", required=True)
    download = models.add_parser("download")
    download.add_argument("names", nargs="+")
    service = models.add_parser("serve")
    service.add_argument(
        "component",
        choices=[
            "venus",
            "venus-omni",
            "asr",
            "minicpm",
            "voicechat",
            "jev-omni",
            "vision",
            "hud",
            "life",
        ],
    )
    service.add_argument("--gpu")
    service.add_argument("--model-dir")
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int)
    games = commands.add_parser("games")
    games.add_argument("action", choices=["start", "stop", "status"])
    games.add_argument("game", choices=["voxel"])
    args = parser.parse_args()
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        if args.command == "setup":
            if args.component == "pvz":
                from jarvis.games.install import install_pvz

                install_pvz()
            elif args.component == "voxel":
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "jarvis.games.voxel.setup",
                        "--jobs",
                        str(args.jobs),
                    ],
                    check=True,
                )
            else:
                from jarvis.setup import install

                install(args.component, args.python)
        elif args.command == "models":
            if args.action == "download":
                from jarvis.models import catalog, download

                for name in args.names:
                    if name not in catalog():
                        parser.error(
                            f"Unknown model: {name}; choose from {', '.join(catalog())}"
                        )
                    download(name)
            else:
                from jarvis.setup import serve

                serve(
                    args.component,
                    gpu=args.gpu,
                    model_dir=args.model_dir,
                    port=args.port,
                    host=args.host,
                )
        elif args.command == "games":
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "jarvis.games.voxel.service",
                    args.action,
                    "all",
                ],
                check=True,
            )
        else:
            from jarvis.config import load_config

            config = load_config(
                args.config, mode=args.mode, frontend=args.frontend, game=args.game
            )
            if args.command == "doctor":
                from jarvis.doctor import doctor

                raise SystemExit(asyncio.run(doctor(config)))
            from jarvis.server import create_app
            import uvicorn

            uvicorn.run(
                create_app(config),
                host=args.host or config["server"]["host"],
                port=args.port or config["server"]["port"],
                ws_per_message_deflate=False,
            )
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
