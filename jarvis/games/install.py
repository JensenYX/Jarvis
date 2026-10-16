import hashlib
from pathlib import Path
import shutil
import tarfile
import tempfile
from urllib.request import urlopen

from jarvis.paths import data_root


def install_pvz():
    target = data_root() / "local_games/pvz_web"
    if (target / "game/index.html").is_file():
        print(f"PvZ: {target}")
        return
    url = "https://codeload.github.com/Plants-vs-Zombies-MODDED/game/tar.gz/277c1bf97813c355f59e974c5a3a7ffd46df5b1d"
    expected = "607d190a698152086f8c76b3e237d963c9307e2f0b1a76cecb66a4014deed004"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=target.parent, prefix="pvz-install-"
    ) as temporary:
        archive = Path(temporary) / "game.tar.gz"
        with urlopen(url, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                raise ValueError(
                    "The game download does not match the supported release"
                )
        staging = Path(temporary) / "game"
        staging.mkdir()
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                parts = Path(member.name).parts[1:]
                if not parts or parts[0] not in {"game", "LICENSE.md", "README.md"}:
                    continue
                if ".." in parts or member.issym() or member.islnk():
                    raise ValueError("Unexpected game archive entry")
                member.name = str(Path(*parts))
                source.extract(member, staging, filter="data")
        if not (staging / "game/index.html").is_file():
            raise ValueError("Game entry point is missing from the download")
        if target.exists():
            raise ValueError("The game directory already exists but is incomplete")
        staging.rename(target)
    print(f"PvZ: {target}")
