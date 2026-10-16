from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.error import URLError
from urllib.request import urlopen
from jarvis.paths import data_root, resource

ROOT = data_root()
BASE = ROOT / "local_games/voxel"
SYS = BASE / "sysroot"
CACHE = BASE / "downloads"


def download(url, path, sha):
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha:
        return
    temporary = path.with_suffix(path.suffix + ".part")
    for attempt in range(4):
        try:
            with urlopen(url, timeout=30) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            break
        except (URLError, TimeoutError, ConnectionError):
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    if hashlib.sha256(temporary.read_bytes()).hexdigest() != sha:
        temporary.unlink()
        raise ValueError("Download checksum mismatch: " + path.name)
    temporary.replace(path)


def sources():
    lock = json.loads((resource("games/versions.lock.json")).read_text())
    for name, project in [
        ("luanti", "luanti-org/luanti"),
        ("voxelibre", "VoxeLibre/VoxeLibre"),
    ]:
        data = lock[name]
        destination = BASE / "sources" / name
        if destination.exists():
            continue
        archive = CACHE / (name + "-" + data["commit"] + ".tar.gz")
        download(
            f"https://codeload.github.com/{project}/tar.gz/{data['commit']}",
            archive,
            data["archive_sha256"],
        )
        destination.mkdir(parents=True)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                parts = Path(member.name).parts[1:]
                if not parts:
                    continue
                if ".." in parts:
                    raise ValueError("Unsafe archive member")
                if member.issym():
                    target = (
                        destination / Path(*parts).parent / member.linkname
                    ).resolve()
                    if not target.is_relative_to(destination.resolve()):
                        raise ValueError("Unsafe source symlink")
                if member.islnk():
                    raise ValueError("Unexpected source hard link")
                member.name = str(Path(*parts))
                tar.extract(member, destination)


def dependencies():
    packages = json.loads((resource("games/packages.lock.json")).read_text())
    SYS.mkdir(parents=True, exist_ok=True)

    def fetch(row):
        target = CACHE / row["filename"]
        download(row["url"], target, row["sha256"])
        return target

    with ThreadPoolExecutor(max_workers=8) as pool:
        files = list(pool.map(fetch, packages))
    for archive in files:
        subprocess.run(["dpkg-deb", "-x", str(archive), str(SYS)], check=True)
    for path in SYS.rglob("*"):
        if path.is_symlink() and os.readlink(path).startswith("/"):
            target = SYS / os.readlink(path).lstrip("/")
            if target.exists():
                path.unlink()
                path.symlink_to(os.path.relpath(target, path.parent))


def patch_local(install_xkb_link=True):
    config = SYS / "usr/lib/x86_64-linux-gnu/cmake/SDL2/sdl2-config.cmake"
    if config.exists():
        config.write_text(
            config.read_text().replace(
                'set(prefix "/usr")',
                'get_filename_component(prefix "${CMAKE_CURRENT_LIST_DIR}/../../../.." ABSOLUTE)',
            )
        )
    x = SYS / "usr/bin/Xvfb"
    raw = x.read_bytes()
    if b"/usr/bin\x00" in raw:
        if raw.count(b"/usr/bin\x00") != 1:
            raise ValueError("Xvfb patch source mismatch")
        x.write_bytes(raw.replace(b"/usr/bin\x00", b"/tmp/vx1\x00"))
    elif b"/tmp/vx1\x00" not in raw:
        raise ValueError("Xvfb dependency changed")
    launch = SYS / "launch-bin"
    launch.mkdir(exist_ok=True)
    wrapper = launch / "xkbcomp"
    wrapper.write_text(
        '#!/bin/sh\nVOXEL_TOOL="$(readlink -f -- "$0")"\nVOXEL_ROOT="$(CDPATH= cd -- "$(dirname -- "$VOXEL_TOOL")/.." && pwd)"\nexec /lib64/ld-linux-x86-64.so.2 --library-path "$VOXEL_ROOT/usr/lib/x86_64-linux-gnu:$VOXEL_ROOT/lib/x86_64-linux-gnu" "$VOXEL_ROOT/usr/bin/xkbcomp" "$@"\n'
    )
    wrapper.chmod(493)
    if install_xkb_link:
        link = Path("/tmp/vx1")
        if link.is_symlink():
            if link.resolve() != launch.resolve():
                raise RuntimeError("Another voxel display owns /tmp/vx1")
        elif not link.exists():
            link.symlink_to(launch)
        else:
            raise RuntimeError("Reserved game launcher path /tmp/vx1 is occupied")
    source = BASE / "sources/luanti"
    if "companion_timer" not in (source / "src/client/game.cpp").read_text():
        subprocess.run(
            [
                "patch",
                "-p1",
                "-i",
                str(resource("games/patches/voxel/luanti-client-telemetry.patch")),
            ],
            cwd=source,
            check=True,
        )
    if "companion_presentation" not in (source / "src/client/client.h").read_text():
        subprocess.run(
            [
                "patch",
                "-p1",
                "-i",
                str(
                    resource("games/patches/voxel/luanti-companion-presentation.patch")
                ),
            ],
            cwd=source,
            check=True,
        )
    if (
        "presentation.target - camera->getPosition()"
        not in (source / "src/client/game.cpp").read_text()
    ):
        subprocess.run(
            [
                "patch",
                "-p1",
                "-i",
                str(
                    resource("games/patches/voxel/luanti-companion-camera-world.patch")
                ),
            ],
            cwd=source,
            check=True,
        )
    game_source = BASE / "sources/voxelibre"
    if (
        'if not pos or not core.compare_block_status(pos, "active") then return end'
        not in (game_source / "mods/ENTITIES/mcl_minecarts/carts.lua").read_text()
    ):
        subprocess.run(
            [
                "patch",
                "-p1",
                "-i",
                str(resource("games/patches/voxel/voxelibre-cart-active-block.patch")),
            ],
            cwd=game_source,
            check=True,
        )


def build(jobs):
    source = BASE / "sources/luanti"
    build_dir = BASE / "build"
    lib = SYS / "usr/lib/x86_64-linux-gnu"
    env = dict(os.environ)
    env["PKG_CONFIG_PATH"] = str(lib / "pkgconfig")
    env["PKG_CONFIG_SYSROOT_DIR"] = str(SYS)
    flags = " ".join(
        (
            "-Wl,-rpath-link," + str(p)
            for p in [lib, SYS / "lib/x86_64-linux-gnu", lib / "pulseaudio"]
        )
    )
    configure = [
        "cmake",
        "-S",
        str(source),
        "-B",
        str(build_dir),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DRUN_IN_PLACE=TRUE",
        "-DBUILD_CLIENT=TRUE",
        "-DBUILD_SERVER=TRUE",
        "-DBUILD_UNITTESTS=FALSE",
        "-DBUILD_DOCUMENTATION=FALSE",
        "-DENABLE_LTO=FALSE",
        "-DENABLE_GETTEXT=FALSE",
        "-DENABLE_POSTGRESQL=FALSE",
        "-DENABLE_REDIS=FALSE",
        "-DENABLE_SPATIAL=FALSE",
        "-DENABLE_LEVELDB=FALSE",
        "-DENABLE_CURSES=FALSE",
        "-DENABLE_SOUND=TRUE",
        "-DENABLE_CURL=TRUE",
        "-DCMAKE_PREFIX_PATH=" + str(SYS / "usr"),
        "-DCMAKE_CXX_FLAGS=-I"
        + str(SYS / "usr/include")
        + " -I"
        + str(SYS / "usr/include/x86_64-linux-gnu"),
        "-DFREETYPE_LIBRARY=" + str(lib / "libfreetype.so.6"),
        "-DCMAKE_EXE_LINKER_FLAGS=-Wl,--disable-new-dtags " + flags,
        "-DCMAKE_BUILD_RPATH=$ORIGIN/../../../sysroot/usr/lib/x86_64-linux-gnu;$ORIGIN/../../../sysroot/lib/x86_64-linux-gnu;$ORIGIN/../../../sysroot/usr/lib/x86_64-linux-gnu/pulseaudio",
    ]
    subprocess.run(configure, env=env, check=True)
    subprocess.run(
        ["cmake", "--build", str(build_dir), "-j", str(jobs)], env=env, check=True
    )


def main():
    global BASE, SYS, CACHE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-downloads", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--directory", type=Path, default=BASE)
    parser.add_argument("--cache-directory", type=Path)
    parser.add_argument(
        "--no-display-link",
        action="store_true",
        help="Install dependencies without creating the Xvfb keyboard helper link",
    )
    args = parser.parse_args()
    BASE = args.directory.resolve()
    SYS = BASE / "sysroot"
    CACHE = (
        args.cache_directory.resolve() if args.cache_directory else BASE / "downloads"
    )
    CACHE.mkdir(parents=True, exist_ok=True)
    if not args.skip_downloads:
        sources()
        dependencies()
    patch_local(not args.no_display_link)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(BASE / "python"),
            "Pillow==11.3.0",
            "mss==10.1.0",
            "python-xlib==0.33",
            "six==1.17.0",
        ],
        check=True,
    )
    if not args.skip_build:
        build(max(1, min(args.jobs, 16)))
    print("VoxeLibre is installed. Run jarvis games start voxel.")


if __name__ == "__main__":
    main()
