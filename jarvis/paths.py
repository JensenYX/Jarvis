import os
from importlib.resources import files
from pathlib import Path

RESOURCE_ROOT = Path(str(files("jarvis").joinpath("resources")))


def data_root():
    return (
        Path(os.environ.get("JARVIS_DATA_DIR", "~/.local/share/jarvis"))
        .expanduser()
        .resolve()
    )


def model_root():
    return (
        Path(os.environ.get("JARVIS_MODEL_DIR", str(data_root() / "models")))
        .expanduser()
        .resolve()
    )


def resource(*parts):
    return RESOURCE_ROOT.joinpath(*parts)


def weight(name):
    return resource("weights", name + ".safetensors")


def model_path(name):
    directory = model_root() / name
    subdirectory = {
        "venus": "Realtime-Venus-Audio",
        "venus-omni": "Realtime-Venus-Omni",
    }.get(name)
    return directory / subdirectory if subdirectory else directory
