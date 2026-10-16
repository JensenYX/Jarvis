import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from jarvis.paths import data_root, resource


def environment(name):
    return data_root() / "envs" / name


def environment_python(name):
    return environment(name) / "bin/python"


def install(component, python=None):
    target = environment(component)
    interpreter = python or sys.executable
    if component == "voicechat" and python is None:
        interpreter = shutil.which("python3.12") or interpreter
    version = subprocess.check_output(
        [
            interpreter,
            "-c",
            'import sys; print(".".join(map(str, sys.version_info[:2])))',
        ],
        text=True,
    ).strip()
    if component == "voicechat" and version != "3.12":
        raise ValueError(
            "VoiceChat requires Python 3.12; use --python /path/to/python3.12"
        )
    if component in {"venus", "minicpm"} and version not in {"3.10", "3.11", "3.12"}:
        raise ValueError("Use Python 3.11 for the Venus and MiniCPM environments")
    if not environment_python(component).is_file():
        subprocess.run([interpreter, "-m", "venv", str(target)], check=True)
    pip = [str(environment_python(component)), "-m", "pip"]
    subprocess.run(
        [*pip, "install", "--upgrade", "pip", "setuptools<81", "wheel"], check=True
    )
    subprocess.run(
        [*pip, "install", "-r", str(resource("requirements", component + ".txt"))],
        check=True,
    )
    if component == "voicechat":
        source = data_root() / "sources/voicechat"
        entry = json.loads(resource("upstreams.json").read_text())["voicechat"]
        if not (source / ".git").is_dir():
            source.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--filter=blob:none", entry["repo"], str(source)],
                check=True,
            )
        subprocess.run(
            ["git", "-C", str(source), "checkout", "--detach", entry["revision"]],
            check=True,
        )
        subprocess.run(
            [
                *pip,
                "install",
                "-e",
                str(source) + "[all]",
                "-c",
                str(resource("requirements/voicechat.txt")),
            ],
            check=True,
        )
        subprocess.run(
            [*pip, "install", "-r", str(resource("requirements/voicechat.txt"))],
            check=True,
        )
        subprocess.run(
            [
                *pip,
                "install",
                "--no-build-isolation",
                "--no-deps",
                "causal-conv1d==1.6.2.post1",
                "mamba-ssm==2.3.2.post1",
            ],
            check=True,
        )
    install_runtime(component)
    print(f"{component}: {target}")


def install_runtime(component):
    destination = environment(component)
    source = Path(__file__).resolve().parent
    runtime = destination / "jarvis-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="jarvis-update-", dir=destination
    ) as temporary:
        staging = Path(temporary) / "jarvis"
        shutil.copytree(
            source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        previous = runtime / "jarvis"
        backup = Path(temporary) / "previous"
        if previous.exists():
            previous.rename(backup)
        try:
            staging.rename(previous)
        except OSError:
            if backup.exists():
                backup.rename(previous)
            raise
    site = subprocess.check_output(
        [
            str(environment_python(component)),
            "-c",
            'import sysconfig; print(sysconfig.get_paths()["purelib"])',
        ],
        text=True,
    ).strip()
    Path(site, "jarvis-runtime.pth").write_text(str(runtime) + "\n")


def serve(component, *, gpu=None, model_dir=None, port=None, host="127.0.0.1"):
    python = environment_python(
        component
        if component not in {"venus-omni", "hud", "life"}
        else "venus"
        if component == "venus-omni"
        else "omni"
    )
    if component in {"jev-omni", "vision"}:
        python = environment_python("omni")
    if not python.is_file():
        raise ValueError(
            f"Model environment is missing. Run jarvis setup {'omni' if component in {'jev-omni', 'vision', 'hud', 'life'} else 'venus' if component == 'venus-omni' else component}"
        )
    env = dict(os.environ)
    if gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)
    install_runtime(
        component
        if component in {"venus", "minicpm", "voicechat", "asr"}
        else "venus"
        if component == "venus-omni"
        else "omni"
    )
    env.pop("PYTHONPATH", None)
    command = [str(python), "-I", "-m", "jarvis.services", component, "--host", host]
    if model_dir:
        command += ["--model-dir", str(model_dir)]
    if port:
        command += ["--port", str(port)]
    subprocess.run(command, env=env, check=True)
