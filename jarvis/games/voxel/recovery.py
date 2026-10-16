import asyncio
import json
import os
import time
from pathlib import Path


class ClientRecovery:
    def __init__(
        self, bridge, root, *, inspect=None, restart=None, clock=time.monotonic
    ):
        self.bridge = bridge
        self.root = Path(root)
        self.clock = clock
        self.inspect = inspect or self._inspect
        self.restart = restart or self._restart
        self.missing_since = None
        self.next_attempt = 0.0
        self.attempts = 0
        self.state = "ready"

    def _inspect(self):
        from jarvis.games.voxel.service import alive

        state = self.root / ".runtime/voxel"
        rows = {}
        for name in ("client", "server", "xvfb"):
            try:
                rows[name] = json.loads((state / (name + ".json")).read_text())
            except (OSError, ValueError):
                rows[name] = None
        disabled = any(
            (
                (state / (name + ".disabled")).exists()
                for name in ("client", "server", "app")
            )
        )
        client_alive = bool(rows["client"] and alive(rows["client"]))
        client_age = None
        if client_alive:
            client_age = float(Path("/proc/uptime").read_text().split()[0]) - float(
                rows["client"]["starttime"]
            ) / os.sysconf("SC_CLK_TCK")
        return {
            "enabled": not disabled and bool(rows["client"]),
            "dependencies": all(
                (rows[n] and alive(rows[n]) for n in ("server", "xvfb"))
            ),
            "client_alive": client_alive,
            "client_age_s": client_age,
        }

    async def _restart(self, replace):
        from jarvis.games.voxel.service import ensure_client

        await asyncio.to_thread(ensure_client, replace_disconnected=replace)

    async def check(self):
        native = await asyncio.to_thread(self.inspect)
        now = self.clock()
        s = self.bridge.snapshot
        disconnected = bool(s and s.get("phase") == "disconnected")
        missing = not native["client_alive"] or disconnected
        if not missing:
            self.missing_since = None
            self.state = "ready"
            return
        self.missing_since = self.missing_since or now
        if not native["enabled"] or not native["dependencies"]:
            self.state = "stopped"
            return
        if (
            native["client_alive"]
            and native.get("client_age_s") is not None
            and (native["client_age_s"] < 60)
        ):
            self.state = "starting"
            return
        if native["client_alive"] and (
            not self.bridge.input_owner or now - self.missing_since < 5
        ):
            return
        if now < self.next_attempt:
            return
        self.state = "recovering"
        self.attempts += 1
        self.next_attempt = now + min(30, 2 ** min(self.attempts, 5))
        self.bridge.revoke("client_recovery")
        self.bridge.emit("client.recovering", attempt=self.attempts)
        await self.bridge.display.release()
        try:
            await self.restart(native["client_alive"])
            self.next_attempt = max(self.next_attempt, self.clock() + 60)
            self.bridge.emit("client.restarted", attempt=self.attempts)
        except Exception as error:
            self.state = "retrying"
            self.bridge.emit("client.recovery_failed", reason=type(error).__name__)

    async def run(self):
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.bridge.emit("client.recovery_failed", reason=type(error).__name__)
            await asyncio.sleep(1)
