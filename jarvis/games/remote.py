from __future__ import annotations
import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Callable
from jarvis.games.protocol import (
    ActionResult,
    Anchor,
    Arm,
    ArmAutoplay,
    StartAutoplay,
    Capabilities,
    Envelope,
    MAX_MESSAGE_BYTES,
    Submit,
    decode,
)


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Binding:
    session_id: str
    game_id: str
    game_instance_id: str
    bridge_epoch: str
    active_game_epoch: int


@dataclass(frozen=True)
class Origin:
    binding: Binding
    anchor: Anchor
    source_event_id: str
    authority_generation: int = 0


class RemoteGameAdapter:
    def __init__(
        self,
        session_id,
        emit: Callable,
        *,
        clock=time.monotonic,
        request_timeout=3.0,
        heartbeat_timeout=3.0,
        max_origin_age=3.0,
    ):
        self.session_id, self.emit, self.clock = (session_id, emit, clock)
        self.request_timeout, self.heartbeat_timeout, self.max_origin_age = (
            request_timeout,
            heartbeat_timeout,
            max_origin_age,
        )
        self.active_game_epoch = 0
        self.game_id = None
        self.binding = None
        self.capabilities = Capabilities()
        self.snapshot = None
        self.snapshot_received = 0.0
        self.send = None
        self.last_seen = 0.0
        self.last_event_seq = -1
        self.last_snapshot_seq = -1
        self.pending = {}
        self.source_ids = set()
        self.command_results = {}
        self.grants = {}
        self.retired_bindings = set()
        self.contexts = {}
        self.commands = {}
        self.confirmations = {}
        self.transport_owner = None
        self.fresh_nonce = None
        self.fresh_started = None
        self.probes = {}
        self.uncertain = False
        self.uncertain_since = None
        self.submit_lock = asyncio.Lock()
        self.connected = False
        self.authority_generation = 0
        self.revocation_floor = None
        self.autoplay = None

    async def activate(self, game_id, send):
        if self.connected:
            await self.revoke("game_switched", wait=False)
        self.disconnect("game_switched")
        self.active_game_epoch += 1
        self.game_id = game_id
        self.send = send
        self.retired_bindings.clear()
        self.emit(
            "bridge.selected", game_id=game_id, active_game_epoch=self.active_game_epoch
        )
        return self.active_game_epoch

    def origin(self, source_event_id, client=None):
        if not self.binding or not self.snapshot:
            return None
        if client is not None:
            if any(
                (
                    client.get(k) != getattr(self.binding, k)
                    for k in ("game_instance_id", "bridge_epoch", "active_game_epoch")
                )
            ):
                return None
            try:
                anchor = Anchor.model_validate(client.get("anchor"))
            except (ValueError, TypeError):
                return None
            context = self.contexts.get(anchor.snapshot_seq)
            if not context:
                return None
            snapshot, _ = context
            if any(
                (
                    getattr(snapshot, k) != getattr(anchor, k)
                    for k in ("decision_id", "candidate_set_id", "control_epoch")
                )
            ):
                return None
            return Origin(
                self.binding, anchor, source_event_id, self.authority_generation
            )
        return Origin(
            self.binding,
            Anchor(
                snapshot_seq=self.snapshot.snapshot_seq,
                decision_id=self.snapshot.decision_id,
                control_epoch=self.snapshot.control_epoch,
                candidate_set_id=self.snapshot.candidate_set_id,
            ),
            source_event_id,
            self.authority_generation,
        )

    def current(self, origin, *, require_window=False):
        s = self.snapshot
        return bool(
            origin
            and self.connected
            and (origin.binding == self.binding)
            and s
            and (origin.authority_generation == self.authority_generation)
            and (s.decision_id == origin.anchor.decision_id)
            and (s.control_epoch == origin.anchor.control_epoch)
            and (
                self.game_id == "pvz_web"
                or s.candidate_set_id == origin.anchor.candidate_set_id
            )
            and (
                not require_window
                or (
                    s.awaiting_action
                    and self.revocation_floor is None
                    and (not self.uncertain)
                )
            )
        )

    def fresh(self):
        return bool(
            self.connected
            and self.snapshot
            and (self.fresh_started is not None)
            and (self.clock() - self.fresh_started <= self.max_origin_age)
        )

    def _envelope(self, kind, payload, message_id=None):
        if not self.binding:
            raise BridgeError("Game has not completed hello")
        return Envelope(
            type=kind,
            message_id=message_id or secrets.token_hex(16),
            **self.binding.__dict__,
            payload=payload,
        ).model_dump(mode="json")

    async def _request(self, kind, payload, expected):
        if not self.connected or not self.send:
            raise BridgeError("Game disconnected")
        message_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = (expected, future, self.binding, payload)
        try:
            await self.send(self._envelope(kind, payload, message_id))
            return await asyncio.wait_for(future, self.request_timeout)
        finally:
            self.pending.pop(message_id, None)

    async def observe(self, focus=None):
        nonce = secrets.token_hex(16)
        started = self.clock()
        self.probes[nonce] = started
        generation = self.authority_generation
        binding = self.binding
        try:
            payload = {"nonce": nonce}
            if focus is not None:
                payload["focus"] = focus
            result = await self._request("observe", payload, "snapshot")
            if result.nonce != nonce or self.clock() - started > self.max_origin_age:
                raise BridgeError("Fresh observation deadline exceeded")
            self.fresh_nonce = nonce
            self.fresh_started = started
            if (
                self.uncertain
                and self.uncertain_since is not None
                and (started > self.uncertain_since)
                and (generation == self.authority_generation)
                and (binding == self.binding)
            ):
                self.uncertain = False
                self.uncertain_since = None
                self.authority_generation += 1
            return result
        finally:
            self.probes.pop(nonce, None)

    async def receive(self, raw):
        if not isinstance(raw, (str, bytes)):
            import json

            if len(json.dumps(raw).encode()) > MAX_MESSAGE_BYTES:
                raise BridgeError("Message too large")
        envelope = (
            decode(raw)
            if isinstance(raw, (str, bytes))
            else Envelope.model_validate(raw)
        )
        if (
            envelope.session_id != self.session_id
            or envelope.game_id != self.game_id
            or envelope.active_game_epoch != self.active_game_epoch
        ):
            raise BridgeError("Unpaired or inactive game")
        binding = Binding(
            self.session_id,
            self.game_id,
            envelope.game_instance_id,
            envelope.bridge_epoch,
            self.active_game_epoch,
        )
        if envelope.type == "hello":
            hello = envelope.body()
            if self.connected and binding == self.binding:
                raise BridgeError("Duplicate hello")
            if binding in self.retired_bindings:
                raise BridgeError("Retired binding cannot reconnect")
            if len(self.retired_bindings) >= 1024:
                raise BridgeError("Binding ledger full; select game again")
            self.disconnect("new_game_binding")
            self.binding = binding
            self.capabilities = hello.capabilities
            self.last_seen = self.clock()
            self.connected = True
            self.last_event_seq = self.last_snapshot_seq = -1
            self.emit(
                "bridge.connected",
                game_id=self.game_id,
                game_instance_id=binding.game_instance_id,
                bridge_epoch=binding.bridge_epoch,
                build=hello.build,
                adapter_revision=hello.adapter_revision,
                capabilities=self.capabilities.model_dump(mode="json"),
            )
            await self.send(
                self._envelope(
                    "hello_ack",
                    self.capabilities.model_dump(mode="json"),
                    envelope.message_id,
                )
            )
            return
        if not self.connected or binding != self.binding:
            raise BridgeError("Stale bridge binding")
        self.last_seen = self.clock()
        body = envelope.body()
        if envelope.type == "snapshot":
            if body.snapshot_seq <= self.last_snapshot_seq:
                return
            if self.snapshot and body.control_epoch < self.snapshot.control_epoch:
                return
            self.last_snapshot_seq = body.snapshot_seq
            self.snapshot = body
            self.snapshot_received = self.clock()
            if self.autoplay and (
                not body.autoplay
                or body.autoplay.autoplay_id != self.autoplay["id"]
                or body.control_epoch != self.autoplay["control_epoch"]
            ):
                self.autoplay = None
            if (
                self.revocation_floor is not None
                and body.control_epoch > self.revocation_floor
            ):
                self.revocation_floor = None
            self.contexts[body.snapshot_seq] = (body, self.clock())
            while len(self.contexts) > 32:
                self.contexts.pop(next(iter(self.contexts)))
            if body.nonce in self.probes:
                self.fresh_nonce = body.nonce
                self.fresh_started = self.probes[body.nonce]
            self.emit(
                "bridge.snapshot",
                game_id=self.game_id,
                game_instance_id=binding.game_instance_id,
                bridge_epoch=binding.bridge_epoch,
                active_game_epoch=self.active_game_epoch,
                **body.model_dump(mode="json"),
            )
        elif envelope.type == "event":
            if body.event_seq <= self.last_event_seq:
                return
            self.last_event_seq = body.event_seq
            if self.snapshot and body.control_epoch > self.snapshot.control_epoch:
                self.snapshot = self.snapshot.model_copy(
                    update={
                        "control_epoch": body.control_epoch,
                        "awaiting_action": False,
                    }
                )
                self.autoplay = None
            self.emit(
                "bridge.event", game_id=self.game_id, **body.model_dump(mode="json")
            )
        elif envelope.type == "heartbeat":
            await self.send(
                self._envelope("heartbeat", body.model_dump(), envelope.message_id)
            )
        elif envelope.type in {"action_result", "action_status"}:
            request = self.pending.get(envelope.message_id)
            command = self.commands.get(body.command_id)
            if (
                not command
                or command[0] != binding
                or body.action_id not in (None, command[1])
            ):
                raise BridgeError("Unrelated action reply")
            if request and (
                request[0] != envelope.type
                or request[3].get("command_id") != body.command_id
            ):
                raise BridgeError("Mismatched action reply")
            prior = self.command_results.get(body.command_id)
            if (
                prior
                and prior.status.startswith("CONFIRMED")
                and (not body.status.startswith("CONFIRMED"))
            ):
                return
            self.command_results[body.command_id] = body
            future = self.confirmations.get(body.command_id)
            if future and body.status != "ACCEPTED" and (not future.done()):
                future.set_result(body)
            self.emit("bridge.action", game_id=self.game_id, **body.model_dump())
        elif envelope.type == "revoke_ack":
            if self.snapshot and body.control_epoch < self.snapshot.control_epoch:
                return
            if self.snapshot and body.control_epoch >= self.snapshot.control_epoch:
                self.snapshot = self.snapshot.model_copy(
                    update={
                        "control_epoch": body.control_epoch,
                        "awaiting_action": False,
                    }
                )
            self.emit(
                "bridge.revoked", game_id=self.game_id, control_epoch=body.control_epoch
            )
        elif envelope.type not in {"grant_result", "autoplay_result", "error"}:
            raise BridgeError("Unexpected game-to-server message")
        request = self.pending.get(envelope.message_id)
        if request:
            expected, future, requested_binding, requested_payload = request
            if envelope.type in {
                "grant_result",
                "autoplay_result",
            } and body.source_event_id != requested_payload.get("source_event_id"):
                raise BridgeError("Mismatched grant reply")
            if (
                requested_binding == binding
                and expected == envelope.type
                and (not future.done())
            ):
                future.set_result(body)
            elif envelope.type == "error" and (not future.done()):
                future.set_exception(BridgeError(body.code))

    async def start_autoplay(self, origin, scope, duration_ms=None, allowed_kinds=None):
        if self.game_id != "pvz_web" or not self.capabilities.auto_continue:
            raise BridgeError("Autoplay unavailable")
        if not self.current(origin, require_window=True):
            raise BridgeError("Stale authorization")
        if origin.source_event_id in self.source_ids or len(self.source_ids) >= 2048:
            raise BridgeError("Source unavailable")
        self.source_ids.add(origin.source_event_id)
        await self.observe()
        if not self.current(origin, require_window=True):
            raise BridgeError("Authorization context changed")
        current = self.origin(origin.source_event_id)
        started = self.clock()
        result = await self._request(
            "start_autoplay",
            StartAutoplay(
                source_event_id=origin.source_event_id,
                anchor=current.anchor,
                scope=scope,
                duration_ms=duration_ms,
                allowed_kinds=allowed_kinds or ["PLANT", "COLLECT_SUN"],
            ).model_dump(mode="json"),
            "autoplay_result",
        )
        if result.status != "STARTED" or not result.autoplay_id:
            raise BridgeError(result.reason or "Autoplay rejected")
        if not self.current(current, require_window=True):
            raise BridgeError("Autoplay revoked while starting")
        self.autoplay = {
            "id": result.autoplay_id,
            "scope": scope,
            "binding": self.binding,
            "control_epoch": current.anchor.control_epoch,
            "authority_generation": self.authority_generation,
            "expires": started + duration_ms / 1000
            if duration_ms is not None
            else None,
        }
        return dict(self.autoplay)

    def autoplay_current(self, run_id):
        run = self.autoplay
        s = self.snapshot
        return bool(
            run
            and run["id"] == run_id
            and self.connected
            and (self.binding == run["binding"])
            and s
            and (run["authority_generation"] == self.authority_generation)
            and (s.control_epoch == run["control_epoch"])
            and (run["expires"] is None or self.clock() < run["expires"])
            and (not self.uncertain)
            and (self.revocation_floor is None)
        )

    async def discard_step(self, grant_id):
        issued = self.grants.pop(grant_id, None)
        if issued and issued[0].binding == self.binding and self.connected:
            await self.send(self._envelope("discard_step", {"grant_id": grant_id}))

    async def arm_once(self, origin, allowed_kinds, ttl_ms=3000, *, autoplay_id=None):
        if not self.current(origin, require_window=True) or not self.fresh():
            raise BridgeError("Stale action origin")
        if autoplay_id and (not self.autoplay_current(autoplay_id)):
            raise BridgeError("Autoplay ended")
        if not autoplay_id and self.autoplay:
            raise BridgeError("Autoplay active")
        if self.capabilities.mode != "guarded_control":
            raise BridgeError("Game control is observation-only")
        if origin.source_event_id in self.source_ids:
            raise BridgeError("Source already consumed")
        if not set(allowed_kinds) <= set(self.capabilities.actions):
            raise BridgeError("Unsupported action kind")
        if len(self.source_ids) >= 2048:
            raise BridgeError("Source ledger full; reconnect")
        self.source_ids.add(origin.source_event_id)
        payload = Arm(
            source_event_id=origin.source_event_id,
            anchor=origin.anchor,
            allowed_kinds=allowed_kinds,
            ttl_ms=ttl_ms,
        ).model_dump(mode="json")
        if autoplay_id:
            payload = ArmAutoplay(**payload, autoplay_id=autoplay_id).model_dump(
                mode="json"
            )
        result = await self._request(
            "arm_autoplay_step" if autoplay_id else "arm_once", payload, "grant_result"
        )
        if result.source_event_id != origin.source_event_id:
            raise BridgeError("Grant belongs to another source")
        if result.status != "ARMED" or not result.grant_id:
            raise BridgeError(result.reason or "Host rejected grant")
        if not self.current(origin, require_window=True) or (
            autoplay_id and (not self.autoplay_current(autoplay_id))
        ):
            if origin.binding == self.binding:
                await self.revoke("origin_changed", wait=False)
            raise BridgeError("Origin changed while arming")
        context = self.contexts.get(origin.anchor.snapshot_seq)
        if not context:
            raise BridgeError("Source snapshot expired")
        self.grants[result.grant_id] = (origin, tuple(allowed_kinds), context[0])
        return result

    async def commit_action(self, origin, grant_id, action_id):
        async with self.submit_lock:
            requested_binding = self.binding
            issued = self.grants.pop(grant_id, None)
            if not issued or issued[0] != origin:
                raise BridgeError("Unknown or consumed grant")
            if not self.current(origin, require_window=True) or not self.fresh():
                raise BridgeError("Stale submission")
            action = next(
                (a for a in issued[2].legal_actions if a.action_id == action_id), None
            )
            if (
                not action
                or action.kind not in self.capabilities.actions
                or action.kind not in issued[1]
            ):
                raise BridgeError("Illegal candidate")
            command_id = secrets.token_hex(16)
            if len(self.commands) >= 2048:
                raise BridgeError("Command ledger full; reconnect")
            self.commands[command_id] = (self.binding, action_id)
            confirmation = asyncio.get_running_loop().create_future()
            self.confirmations[command_id] = confirmation
            self.emit(
                "bridge.action.submitting",
                command_id=command_id,
                game_id=self.game_id,
                action_id=action_id,
            )
            try:
                result = await self._request(
                    "submit_once",
                    Submit(
                        command_id=command_id,
                        grant_id=grant_id,
                        action_id=action_id,
                        anchor=origin.anchor,
                    ).model_dump(mode="json"),
                    "action_result",
                )
                if result.status == "ACCEPTED":
                    result = await asyncio.wait_for(
                        asyncio.shield(confirmation), self.request_timeout
                    )
            except (TimeoutError, BridgeError):
                if self.binding != requested_binding:
                    result = ActionResult(
                        command_id=command_id,
                        status="UNKNOWN",
                        reason="binding_changed",
                    )
                else:
                    self.mark_uncertain()
                    try:
                        result = await self._request(
                            "get_action_status",
                            {"command_id": command_id},
                            "action_status",
                        )
                    except (TimeoutError, BridgeError):
                        result = ActionResult(
                            command_id=command_id, status="UNKNOWN", reason="ack_lost"
                        )
            finally:
                self.confirmations.pop(command_id, None)
                if not confirmation.done():
                    confirmation.cancel()
            if result.command_id != command_id:
                result = ActionResult(
                    command_id=command_id, status="UNKNOWN", reason="mismatched_reply"
                )
            if self.binding != requested_binding:
                return result
            self.command_results[command_id] = result
            if result.status in {"UNKNOWN", "ACCEPTED"}:
                self.mark_uncertain()
            else:
                self.uncertain = False
                self.uncertain_since = None
            return result

    def mark_uncertain(self):
        if not self.uncertain:
            self.authority_generation += 1
        self.uncertain = True
        self.uncertain_since = self.clock()
        self.grants.clear()
        self.autoplay = None

    def fence_control(self):
        self.authority_generation += 1
        self.grants.clear()
        self.autoplay = None
        if self.snapshot:
            self.revocation_floor = max(
                self.revocation_floor or 0, self.snapshot.control_epoch
            )
            self.snapshot = self.snapshot.model_copy(update={"awaiting_action": False})

    async def revoke(self, reason="user_reclaimed", *, wait=True):
        self.fence_control()
        if not self.connected:
            return
        self.emit("bridge.revoke_requested", game_id=self.game_id, reason=reason)
        if wait:
            return await self._request("revoke", {"reason": reason}, "revoke_ack")
        await self.send(self._envelope("revoke", {"reason": reason}))

    def disconnect(self, reason="disconnected"):
        was_connected = self.connected
        self.connected = False
        self.snapshot = None
        self.authority_generation += 1
        self.revocation_floor = None
        if self.binding:
            self.retired_bindings.add(self.binding)
        self.capabilities = Capabilities()
        self.fresh_started = None
        self.fresh_nonce = None
        self.uncertain = False
        self.uncertain_since = None
        self.source_ids.clear()
        self.probes.clear()
        self.grants.clear()
        self.autoplay = None
        self.contexts.clear()
        self.commands.clear()
        self.command_results.clear()
        for command_id, future in self.confirmations.items():
            if not future.done():
                future.set_result(
                    ActionResult(command_id=command_id, status="UNKNOWN", reason=reason)
                )
        self.confirmations.clear()
        for _, future, _, _ in self.pending.values():
            if not future.done():
                future.set_exception(BridgeError(reason))
        self.pending.clear()
        if was_connected:
            self.emit("bridge.disconnected", game_id=self.game_id, reason=reason)

    def check_lease(self):
        if self.connected and self.clock() - self.last_seen > self.heartbeat_timeout:
            self.disconnect("heartbeat_expired")
            return False
        return self.connected


class PairingRegistry:
    def __init__(self):
        self.entries = {}

    def issue(self, adapter):
        self.remove(adapter.session_id)
        token = secrets.token_urlsafe(32)
        self.entries[token] = (adapter.session_id, adapter)
        return token

    def find(self, token):
        for expected, (_, adapter) in self.entries.items():
            if secrets.compare_digest(token, expected):
                return adapter
        return None

    def remove(self, session_id):
        for token, (sid, _) in list(self.entries.items()):
            if sid == session_id:
                self.entries.pop(token, None)
