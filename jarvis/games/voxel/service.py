from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import fcntl
from jarvis.paths import data_root, resource

ROOT = data_root()
BASE = ROOT / "local_games/voxel"
STATE = ROOT / ".runtime/voxel"
LOG = ROOT / "logs/voxel"
SYS = BASE / "sysroot"
USER = BASE / "user"


def environment():
    lib = SYS / "usr/lib/x86_64-linux-gnu"
    env = dict(os.environ)
    modules = next((SYS / "usr/lib").glob("pulse-*/modules"))
    env.update(
        LD_LIBRARY_PATH=f"{lib}:{SYS}/lib/x86_64-linux-gnu:{lib}/pulseaudio:{modules}",
        LUANTI_USER_PATH=str(USER),
        LUANTI_GAME_PATH=str(USER / "games"),
        DISPLAY=":91",
        LIBGL_DRIVERS_PATH=str(lib / "dri"),
        LIBGL_ALWAYS_SOFTWARE="1",
        __GLX_VENDOR_LIBRARY_NAME="mesa",
        LP_NUM_THREADS="8",
        PULSE_SERVER=f"unix:{STATE}/pulse.sock",
        PULSE_RUNTIME_PATH=str(STATE / "pulse"),
        PULSE_STATE_PATH=str(STATE / "pulse-state"),
        PULSE_CONFIG_PATH=str(STATE / "pulse-config"),
        ALSOFT_DRIVERS="pulse",
    )
    env["PYTHONPATH"] = (
        str(BASE / "python")
        + os.pathsep
        + str(ROOT)
        + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    )
    return env


def alive(record):
    try:
        if (
            record.get("boot_id")
            and record["boot_id"]
            != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        ):
            return False
        fields = (
            Path(f"/proc/{record['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        )
        return fields[0] != "Z" and fields[19] == str(record["starttime"])
    except (OSError, KeyError):
        return False


def start(name, command, *, _locked=False):
    STATE.mkdir(parents=True, exist_ok=True)
    if name == "client" and (not _locked):
        with (STATE / "client-recovery.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return start(name, command, _locked=True)
    LOG.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{name}.json"
    (STATE / (name + ".disabled")).unlink(missing_ok=True)
    if path.exists() and alive(json.loads(path.read_text())):
        print(name, "already running")
        return
    with (LOG / f"{name}-console.log").open("ab") as output:
        process = subprocess.Popen(
            command,
            env=environment(),
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    record = {
        "pid": process.pid,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "starttime": Path(f"/proc/{process.pid}/stat")
        .read_text()
        .rsplit(")", 1)[1]
        .split()[19],
        "command": [str(s) for s in command],
    }
    path.write_text(json.dumps(record, indent=2))
    time.sleep(0.4)
    if process.poll() is not None:
        raise RuntimeError(f"{name} exited; see {LOG / (name + '-console.log')}")
    print(name, "started", process.pid)


def wait_ready(url, seconds=30):
    from urllib.request import urlopen

    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError("Service readiness timed out: " + url)


def wait_game_ready(seconds=60):
    until = time.monotonic() + seconds
    record = json.loads((STATE / "server.json").read_text())
    while time.monotonic() < until:
        if not alive(record):
            raise RuntimeError("Native server exited; inspect server.log")
        try:
            entries = Path("/proc/net/udp").read_text().splitlines()[1:]
            if any((row.split()[1].upper() == "0100007F:790F" for row in entries)):
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Native server did not finish loading within 60 seconds")


def stop(name, *, _locked=False):
    STATE.mkdir(parents=True, exist_ok=True)
    if name == "client" and (not _locked):
        with (STATE / "client-recovery.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return stop(name, _locked=True)
    (STATE / (name + ".disabled")).touch()
    path = STATE / f"{name}.json"
    if not path.exists():
        return
    record = json.loads(path.read_text())
    if alive(record):
        os.kill(record["pid"], signal.SIGTERM)
        for _ in range(100):
            if not alive(record):
                break
            time.sleep(0.1)
        if alive(record):
            raise RuntimeError(f"{name} is still saving/exiting; not killing it")
        print(name, "stopped")


def ensure_client(*, replace_disconnected=False):
    with (STATE / "client-recovery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if any(
            (
                (STATE / (name + ".disabled")).exists()
                for name in ("client", "server", "app")
            )
        ):
            return
        try:
            record = json.loads((STATE / "client.json").read_text())
        except (OSError, ValueError):
            return
        expected = str(BASE / "sources/luanti/bin/luanti")
        if not record.get("command") or record["command"][0] != expected:
            raise ValueError("Not the dedicated client")
        for name in ("server", "xvfb"):
            try:
                dependency = json.loads((STATE / (name + ".json")).read_text())
            except (OSError, ValueError):
                return
            if not alive(dependency):
                return
        if alive(record):
            if not replace_disconnected:
                return
            os.kill(record["pid"], signal.SIGTERM)
            until = time.monotonic() + 10
            while alive(record) and time.monotonic() < until:
                time.sleep(0.1)
            if alive(record):
                raise RuntimeError("Client has not finished exiting")
        if any(
            (
                (STATE / (name + ".disabled")).exists()
                for name in ("client", "server", "app")
            )
        ):
            return
        start("client", record["command"], _locked=True)


def prepare():
    STATE.mkdir(parents=True, exist_ok=True)
    LOG.mkdir(parents=True, exist_ok=True)
    tool_link = Path("/tmp/vx1")
    if not tool_link.exists() and (not tool_link.is_symlink()):
        tool_link.symlink_to(SYS / "launch-bin")
    for folder in ("pulse", "pulse-state", "pulse-config"):
        (STATE / folder).mkdir(mode=448, exist_ok=True)
    for filename in ("player.password", "bridge.secret"):
        p = STATE / filename
        if not p.exists():
            p.write_text(secrets.token_urlsafe(32) + "\n")
        p.chmod(384)
    (USER / "games").mkdir(parents=True, exist_ok=True)
    game = USER / "games/voxelibre"
    if game.is_symlink():
        game.unlink()
    if not game.exists():
        game.symlink_to("../../sources/voxelibre", target_is_directory=True)
    world = USER / "worlds/survival"
    world.mkdir(parents=True, exist_ok=True)
    if not (world / "world.mt").exists():
        (world / "world.mt").write_text(
            "gameid = voxelibre\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\nmod_storage_backend = sqlite3\ncreative_mode = false\nenable_damage = true\n"
        )
    if not (world / ".companion-world-id").exists():
        (world / ".companion-world-id").write_text(secrets.token_hex(16))
    settings = {
        "bind_address": "127.0.0.1",
        "port": "30991",
        "max_users": "1",
        "server_announce": "false",
        "creative_mode": "false",
        "enable_damage": "true",
        "jev_clear_weather": "false",
        "jev_always_day": "false",
        "time_speed": "72",
        "secure.enable_security": "true",
        "secure.http_mods": "jev_bridge",
        "enable_mod_channels": "true",
        "default_privs": "interact,shout",
        "disallow_empty_password": "true",
        "default_password": (STATE / "player.password").read_text().strip(),
        "num_emerge_threads": "2",
        "max_block_send_distance": "5",
        "active_block_range": "2",
        "max_objects_per_block": "1024",
        "jev_bridge_url": "http://127.0.0.1:8795/internal/voxel/exchange",
        "jev_bridge_secret": (STATE / "bridge.secret").read_text().strip(),
        "jev_bridge_player": "CompanionPlayer",
        "jev_bridge_world": (world / ".companion-world-id").read_text().strip(),
    }
    settings["jev_bridge_place_verified"] = "true"
    settings["jev_cooperative_build"] = "true"
    settings["jev_survival_tasks"] = "true"
    path = USER / "server.conf"
    path.write_text("".join((f"{k} = {v}\n" for k, v in settings.items())))
    path.chmod(384)
    if not (USER / "client.conf").exists():
        (USER / "client.conf").write_text(
            "screen_w = 1024\nscreen_h = 640\nfullscreen = false\nfps_max = 30\nfps_max_unfocused = 20\nviewing_range = 45\nenable_shaders = false\nenable_dynamic_shadows = false\nenable_clouds = false\nsound_volume = 0.2\nmouse_sensitivity = 0.15\nvideo_driver = opengl3\nkeymap_camera_mode = \n"
        )
    client_conf = USER / "client.conf"
    if "jev_bridge_telemetry" not in client_conf.read_text():
        with client_conf.open("a") as output:
            output.write("\njev_bridge_telemetry = true\n")
    mod = world / "worldmods/jev_bridge"
    if (resource("games/mods/jev_bridge/init.lua")).exists() and (not mod.exists()):
        mod.parent.mkdir(exist_ok=True)
        mod.symlink_to(
            os.path.relpath(resource("games/mods/jev_bridge"), mod.parent),
            target_is_directory=True,
        )
        with (world / "world.mt").open("a") as output:
            output.write("\nload_mod_jev_bridge = true\n")


def commands():
    loader = [
        "/lib64/ld-linux-x86-64.so.2",
        "--library-path",
        environment()["LD_LIBRARY_PATH"],
    ]
    modules = next((SYS / "usr/lib").glob("pulse-*/modules"))
    pulse_config = STATE / "pulse.pa"
    pulse_config.write_text(
        f"load-module module-native-protocol-unix socket={STATE}/pulse.sock auth-anonymous=1\nload-module module-null-sink sink_name=game sink_properties=device.description=VoxeLibre rate=24000 channels=2\nset-default-sink game\n"
    )
    pulse_config.chmod(384)
    return {
        "xvfb": loader
        + [
            str(SYS / "usr/bin/Xvfb"),
            ":91",
            "-screen",
            "0",
            "1024x640x24",
            "-ac",
            "-nolisten",
            "tcp",
            "-noreset",
            "-xkbdir",
            str(SYS / "usr/share/X11/xkb")
            if (SYS / "usr/share/X11/xkb").exists()
            else "/usr/share/X11/xkb",
        ],
        "pulse": loader
        + [
            str(SYS / "usr/bin/pulseaudio"),
            "--daemonize=no",
            "--exit-idle-time=-1",
            "--disable-shm",
            "--use-pid-file=no",
            "--log-level=warning",
            "--dl-search-path=" + str(modules),
            "-n",
            "-F",
            str(pulse_config),
        ],
        "server": [
            str(BASE / "sources/luanti/bin/luantiserver"),
            "--world",
            str(USER / "worlds/survival"),
            "--config",
            str(USER / "server.conf"),
            "--logfile",
            str(LOG / "server.log"),
        ],
        "client": [
            str(BASE / "sources/luanti/bin/luanti"),
            "--address",
            "127.0.0.1",
            "--port",
            "30991",
            "--name",
            "CompanionPlayer",
            "--password-file",
            str(STATE / "player.password"),
            "--config",
            str(USER / "client.conf"),
            "--go",
            "--logfile",
            str(LOG / "client.log"),
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument(
        "names", nargs="*", choices=["xvfb", "pulse", "server", "client", "app", "all"]
    )
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    names = args.names or ["all"]
    if "all" in names:
        names = ["xvfb", "pulse", "app", "server", "client"]
    if args.action == "start":
        prepare()
        available = commands()
        available["app"] = [args.python, "-m", "jarvis", "serve", "--game", "voxel"]
        for name in names:
            start(name, available[name])
            if name == "app":
                wait_ready("http://127.0.0.1:8795/health")
            if name == "server":
                wait_game_ready()
    elif args.action == "stop":
        for name in ["client", "server", "app", "pulse", "xvfb"]:
            if name in names:
                stop(name)
    else:
        for name in names:
            p = STATE / f"{name}.json"
            print(
                name,
                "running"
                if p.exists() and alive(json.loads(p.read_text()))
                else "stopped",
            )


if __name__ == "__main__":
    main()
