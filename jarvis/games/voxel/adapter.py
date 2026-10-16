from __future__ import annotations
from jarvis.paths import resource
import asyncio
import base64
import copy
import json
import math
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from .display import Display

IDENTITY = ("world_id", "game_instance_id", "player_life_id", "bridge_epoch")
ANCHOR = IDENTITY[1:] + ("snapshot_seq", "control_epoch", "candidate_set_id")
MAX_BYTES = 128 * 1024
TRANSIENT_NATIVE_STOPS = {
    "input_lease_expired",
    "exchange_unavailable",
    "engine_stalled",
}


def attack_receipt(reply, action, observed, command):
    if (
        action.get("kind") != "ATTACK"
        or reply.get("kind") != "ATTACK"
        or reply.get("status") != "SUCCEEDED"
        or (reply.get("command_id") != command)
        or (not action.get("target_id"))
        or (reply.get("target_id") != action["target_id"])
    ):
        return reply
    branch = observed["view"].get("survival", {}).get("task") or {}
    candidate = next(
        (
            a
            for a in branch.get("actions", [])
            if a.get("action_id") == action.get("action_id")
            and a.get("kind") == "ATTACK"
            and (a.get("target_id") == action["target_id"])
        ),
        None,
    )
    held = observed["view"]["self"].get("wielded") or {}
    if candidate is None or not held.get("item_id"):
        return reply
    return {
        **reply,
        "weapon_item_id": held["item_id"],
        "weapon_evidence": {
            "kind": "native_validated_attack_snapshot",
            "command_id": command,
            "action_id": action["action_id"],
            "anchor": {k: observed[k] for k in ANCHOR},
        },
    }


def validate(data, depth=0):
    if depth > 12:
        raise ValueError("DTO too deep")
    if data is None or isinstance(data, bool):
        return
    if isinstance(data, (float, int)):
        if not math.isfinite(data) or abs(data) > 1e16:
            raise ValueError("Invalid number")
    elif isinstance(data, str):
        if len(data) > 2000:
            raise ValueError("String too long")
    elif isinstance(data, list):
        if len(data) > 80:
            raise ValueError("Too many items")
        for x in data:
            validate(x, depth + 1)
    elif isinstance(data, dict):
        if len(data) > 80:
            raise ValueError("Too many fields")
        for k, v in data.items():
            if not isinstance(k, str) or len(k) > 100:
                raise ValueError("Invalid field")
            validate(v, depth + 1)
    else:
        raise ValueError("JSON values required")


def snapshot(data):
    validate(data)
    if data.get("protocol_version") != 1 or data.get("route") != "luanti_voxelibre":
        raise ValueError("Route mismatch")
    for k in IDENTITY + ("session_id", "candidate_set_id"):
        if not isinstance(data.get(k), str) or len(data[k]) > 100:
            raise ValueError("Missing identity")
    for k in ("snapshot_seq", "control_epoch", "sampled_at_game_mono_ms"):
        if not isinstance(data.get(k), (float, int)) or data[k] < 0:
            raise ValueError("Invalid sequence")
    if data.get("phase") not in {"playing", "dead", "disconnected"}:
        raise ValueError("Invalid phase")
    out = {
        k: copy.deepcopy(data[k])
        for k in IDENTITY
        + (
            "session_id",
            "candidate_set_id",
            "snapshot_seq",
            "control_epoch",
            "sampled_at_game_mono_ms",
            "phase",
        )
    }
    out.update(
        protocol_version=1,
        route="luanti_voxelibre",
        game_build=data.get("game_build"),
        observation_mode="declared_spatial_sensor",
    )
    v = data.get("view", {})
    p = v.get("self", {})
    out["view"] = {
        "game": "voxel",
        "self": {
            k: p[k]
            for k in (
                "position",
                "eye_position",
                "hp",
                "hp_max",
                "breath",
                "breath_max",
                "head_medium",
                "hunger",
                "wielded",
                "inventory_summary",
                "wield_index",
                "yaw",
                "pitch",
            )
            if k in p
        },
        "environment": {
            k: v.get("environment", {}).get(k)
            for k in (
                "visible_nodes",
                "visible_objects",
                "radius_blocks",
                "quality",
                "observed_at_game_mono_ms",
                "time_of_day",
                "day_phase",
                "always_day",
                "clear_weather",
            )
        },
        "focus": v.get("focus"),
        "recent_events": v.get("recent_events") or [],
        "unknown_fields": v.get("unknown_fields") or [],
    }

    def pos(value):
        if not isinstance(value, dict) or any(
            (not isinstance(value.get(k), (int, float)) for k in ("x", "y", "z"))
        ):
            raise ValueError("Invalid position")
        return {k: value[k] for k in ("x", "y", "z")}

    def item(value):
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("item_id"), str)
            or (not isinstance(value.get("count"), int))
        ):
            raise ValueError("Invalid item")
        if not 0 <= value["count"] <= 65535:
            raise ValueError("Invalid item count")
        return {
            "item_id": value["item_id"][:100],
            "name": str(value.get("name", ""))[:100],
            "count": value["count"],
        }

    public = out["view"]["self"]
    if public:
        public["position"] = pos(public["position"])
        if public.get("eye_position"):
            public["eye_position"] = pos(public["eye_position"])
        public["wielded"] = item(public["wielded"])
        public["inventory_summary"] = [
            item(x) for x in public.get("inventory_summary") or []
        ]
        public["hunger"] = {
            k: public.get("hunger", {}).get(k) for k in ("quality", "value", "max")
        }
    for key in ("inventory_summary",):
        if key in out["view"]["self"]:
            out["view"]["self"][key] = out["view"]["self"][key] or []
    env = out["view"]["environment"]
    for key in ("visible_nodes", "visible_objects"):
        env[key] = env[key] or []
    env["visible_nodes"] = [
        {
            "item_id": str(x.get("item_id", ""))[:100],
            "name": str(x.get("name", ""))[:100],
            "position": pos(x["position"]),
        }
        for x in env["visible_nodes"][:9]
    ]
    env["visible_objects"] = [
        {"kind": str(x.get("kind", ""))[:100], "position": pos(x["position"])}
        for x in env["visible_objects"][:12]
    ]
    if out["view"]["focus"]:
        f = out["view"]["focus"]
        out["view"]["focus"] = {
            "kind": "block_face",
            "item_id": str(f.get("item_id", ""))[:100],
            "name": str(f.get("name", ""))[:100],
            "position": pos(f["position"]),
            "place_position": pos(f["place_position"]),
        }
    out["view"]["recent_events"] = [
        {
            **{
                k: e[k]
                for k in (
                    "event_id",
                    "event_seq",
                    "actor",
                    "evidence",
                    "kind",
                    "player_life_id",
                    "at_game_mono_ms",
                )
                if k in e
            },
            "payload": {
                k: value
                for k, value in (e.get("payload") or {}).items()
                if k
                in {
                    "position",
                    "item_id",
                    "command_id",
                    "count_before",
                    "count_after",
                    "change",
                    "cause",
                    "action",
                    "reason",
                    "grant_id",
                    "source_event_id",
                }
            },
        }
        for e in out["view"]["recent_events"][-16:]
    ]
    out["action_candidates"] = data.get("action_candidates") or []
    if (
        not isinstance(out["action_candidates"], list)
        or len(out["action_candidates"]) > 1
    ):
        raise ValueError("One placement candidate maximum")
    for action in out["action_candidates"]:
        if (
            action.get("kind") != "PLACE_HELD_ON_FOCUS_ONCE"
            or action.get("item_id") != "mcl_core:dirt"
        ):
            raise ValueError("Unsupported action")
    out["capabilities"] = {
        k: bool(data.get("capabilities", {}).get(k, False))
        for k in ("observe", "guarded_control", "place_held_once")
    }
    out["capabilities"]["cooperative_build"] = (
        data.get("capabilities", {}).get("cooperative_build") is True
    )
    out["capabilities"]["survival_tasks"] = (
        data.get("capabilities", {}).get("survival_tasks") is True
    )
    construction = v.get("construction")
    if isinstance(construction, dict):
        materials = [
            {
                "item_id": str(x.get("item_id", ""))[:100],
                "name": str(x.get("name", ""))[:100],
                "sources": [
                    {k: r[k] for k in ("ingredients", "count", "grid") if k in r}
                    for r in (x.get("sources") or [])[:4]
                ],
            }
            for x in (construction.get("materials") or [])[:16]
        ]
        raw_project = construction.get("project")
        project = None
        if isinstance(raw_project, dict):
            project = {
                k: raw_project[k]
                for k in (
                    "project_id",
                    "grant_id",
                    "revision",
                    "status",
                    "expires_in_ms",
                    "completed",
                    "total",
                    "remaining_count",
                    "blocked_reason",
                )
                if k in raw_project
            }
            actions = []
            for a in (raw_project.get("actions") or [])[:40]:
                if a.get("kind") not in {
                    "PROJECT_PLACE",
                    "PROJECT_JUMP_PLACE",
                    "PROJECT_SELECT",
                    "PROJECT_EQUIP",
                    "PROJECT_MOVE",
                } or not isinstance(a.get("action_id"), str):
                    raise ValueError("Invalid project action")
                row = {
                    k: a[k]
                    for k in (
                        "action_id",
                        "kind",
                        "cell",
                        "slot",
                        "item_id",
                        "closest_remaining",
                        "useful_placements",
                        "label",
                        "jump",
                    )
                    if k in a
                }
                if "target" in a:
                    row["target"] = pos(a["target"])
                actions.append(row)
            project["actions"] = actions
            project["remaining_sample"] = [
                {"position": pos(r["position"]), "item_id": str(r["item_id"])[:100]}
                for r in (raw_project.get("remaining_sample") or [])[:16]
            ]
        out["view"]["construction"] = {
            "materials": materials,
            "project": project,
            "terrain": [
                {
                    "position": pos(r["position"]),
                    "clearance": int(r.get("clearance", 0)),
                }
                for r in (construction.get("terrain") or [])[:80]
            ],
        }
        out["view"]["construction"]["survey"] = [
            {
                "position": pos(r["position"]),
                "item_id": str(r.get("item_id", ""))[:100],
                "clearance": int(r.get("clearance", 0)),
                "obstacle": {
                    "position": pos(r["obstacle"]["position"]),
                    "item_id": str(r["obstacle"]["item_id"])[:100],
                }
                if r.get("obstacle")
                else None,
            }
            for r in (construction.get("survey") or [])[:80]
        ]
    survival = v.get("survival")
    if isinstance(survival, dict):
        pub = {
            "nodes": [],
            "entities": [],
            "tools": [],
            "workbench": pos(survival["workbench"])
            if survival.get("workbench")
            else None,
            "task": None,
            "in_water": survival.get("in_water") is True,
            "travel": [],
            "grounded": survival.get("grounded") is True,
            "dry_land": survival.get("dry_land") is True,
            "shore": [
                {"position": pos(r["position"])}
                for r in (survival.get("shore") or [])[:12]
            ],
        }
        pub["recipes"] = [
            {
                k: r[k]
                for k in (
                    "recipe_id",
                    "item_id",
                    "name",
                    "count",
                    "ingredients",
                    "grid",
                )
                if k in r
            }
            for r in (survival.get("recipes") or [])[:40]
        ]
        pub["drops"] = [
            {
                "item_id": str(r["item_id"])[:100],
                "count": int(r["count"]),
                "position": pos(r["position"]),
            }
            for r in (survival.get("drops") or [])[:12]
        ]
        pub["item_help"] = [
            {
                k: str(r.get(k, ""))[:1400]
                for k in ("item_id", "name", "description", "usage")
            }
            for r in (survival.get("item_help") or [])[:16]
        ]
        pub["navigation"] = []
        for r in (survival.get("navigation") or [])[:8]:
            pub["navigation"].append(
                {
                    **{
                        k: r[k]
                        for k in (
                            "id",
                            "rise",
                            "dx",
                            "dz",
                            "unknown_clearance",
                            "unknown_support",
                            "protected",
                            "supported",
                        )
                        if k in r
                    },
                    "target": pos(r["target"]),
                    "place_step": pos(r["place_step"]) if r.get("place_step") else None,
                    "blockers": [
                        {
                            "position": pos(b["position"]),
                            "item_id": str(b["item_id"])[:100],
                        }
                        for b in (r.get("blockers") or [])[:8]
                    ],
                }
            )
        for a in (survival.get("travel") or [])[:8]:
            pub["travel"].append(
                {
                    **{
                        k: a[k]
                        for k in (
                            "kind",
                            "dx",
                            "dz",
                            "rise",
                            "duration_ms",
                            "reaches_solid_land",
                        )
                        if k in a
                    },
                    "target": pos(a["target"]),
                }
            )
        for row in (survival.get("nodes") or [])[:48]:
            pub["nodes"].append(
                {
                    **{
                        k: row[k]
                        for k in (
                            "target_id",
                            "item_id",
                            "name",
                            "diggable",
                            "reason",
                            "dig_ms",
                            "harvest",
                            "focused",
                        )
                        if k in row
                    },
                    "position": pos(row["position"]),
                }
            )
        for row in (survival.get("entities") or [])[:12]:
            pub["entities"].append(
                {
                    **{
                        k: row[k]
                        for k in (
                            "target_id",
                            "kind",
                            "relation",
                            "distance",
                            "possible_drops",
                        )
                        if k in row
                    },
                    "position": pos(row["position"]),
                }
            )
        pub["tools"] = [
            {
                k: row[k]
                for k in (
                    "slot",
                    "item_id",
                    "name",
                    "count",
                    "wear",
                    "damage",
                    "interval",
                    "tool",
                    "placeable_support",
                )
                if k in row
            }
            for row in (survival.get("tools") or [])[:36]
        ]
        pub["item_facts"] = [
            {
                k: r[k]
                for k in (
                    "item_id",
                    "name",
                    "name_zh",
                    "groups",
                    "possible_drops",
                    "plant",
                    "description",
                    "description_zh",
                    "usage",
                    "usage_zh",
                    "has_on_place",
                    "has_on_use",
                    "edible",
                    "food_points",
                    "food_risk",
                )
                if k in r
            }
            for r in (survival.get("item_facts") or [])[:64]
        ]
        for row in pub["item_facts"]:
            if not isinstance(row.get("groups"), dict):
                row["groups"] = {}
        task = survival.get("task")
        if isinstance(task, dict):
            pub["task"] = {
                k: task[k]
                for k in (
                    "task_id",
                    "grant_id",
                    "status",
                    "expires_in_ms",
                    "blocked_reason",
                    "missing",
                    "defeated",
                )
                if k in task
            }
            pub["task"]["craft_requirements"] = [
                {
                    k: r[k]
                    for k in (
                        "recipe_id",
                        "item_id",
                        "grid",
                        "missing",
                        "workbench_required",
                        "inventory_blocker",
                    )
                    if k in r
                }
                for r in (task.get("craft_requirements") or [])[:24]
            ]
            if isinstance(task.get("path"), dict):
                pub["task"]["path"] = {
                    k: task["path"][k]
                    for k in (
                        "clear_length",
                        "limit",
                        "unknown_ahead",
                        "floor_gap",
                        "complete",
                        "cleared",
                        "exit_depth",
                        "furthest_dug",
                    )
                    if k in task["path"]
                }
            pub["task"]["target_states"] = [
                {
                    k: r[k]
                    for k in ("target_index", "position", "item_id", "reason", "dig_ms")
                    if k in r
                }
                for r in (task.get("target_states") or [])[:48]
            ]
            actions = []
            for a in (task.get("actions") or [])[:80]:
                if a.get("kind") not in {
                    "DIG",
                    "DIG_DOWN",
                    "PLACE",
                    "JUMP_PLACE",
                    "CRAFT",
                    "ATTACK",
                    "SELECT",
                    "EQUIP",
                    "USE",
                    "MOVE",
                    "JUMP_MOVE",
                    "SWIM",
                    "SWIM_UP",
                    "JUMP",
                    "LOOK",
                }:
                    raise ValueError("Invalid survival action")
                row = {
                    k: a[k]
                    for k in (
                        "action_id",
                        "kind",
                        "item_id",
                        "target_id",
                        "target_index",
                        "recipe_id",
                        "entity_kind",
                        "dig_ms",
                        "harvest",
                        "count",
                        "distance",
                        "distance_to_nearest_enemy",
                        "cooldown_ms",
                        "slot",
                        "damage",
                        "tool",
                        "placeable_support",
                        "passage_depth",
                        "distance_from_passage",
                        "dx",
                        "dz",
                        "rise",
                        "duration_ms",
                        "yaw",
                        "pitch",
                        "sector",
                        "reaches_solid_land",
                        "safe_walk_margin",
                        "dependency_for",
                        "route_id",
                        "step_support",
                        "site_support",
                        "interaction",
                        "target_item_id",
                    )
                    if k in a
                }
                if isinstance(a.get("dig_comparison"), dict):
                    row["dig_comparison"] = {
                        str(k)[:100]: {
                            f: v[f] for f in ("dig_ms", "wear_after") if f in v
                        }
                        for k, v in list(a["dig_comparison"].items())[:8]
                        if isinstance(v, dict)
                    }
                if "wear" in a:
                    row["wear"] = int(a["wear"])
                if a.get("target"):
                    row["target"] = pos(a["target"])
                if a.get("above"):
                    row["above"] = pos(a["above"])
                actions.append(row)
            pub["task"]["actions"] = actions
        out["view"]["survival"] = pub
    c = data.get("control", {})
    out["control"] = {
        k: c.get(k)
        for k in (
            "mode",
            "uncertain",
            "input_mode",
            "input_seq",
            "blocked_reason",
            "client_menu",
            "client_focused",
            "frame_ms",
        )
    }
    if isinstance(c.get("last_revocation"), dict):
        out["control"]["last_revocation"] = {
            k: c["last_revocation"].get(k)
            for k in ("reason", "control_epoch", "input_seq", "at")
        }
    return out


class VoxelBridge:
    def __init__(self, config):
        self.config = config
        self.owner = None
        self.token = None
        self.snapshot = None
        self.received = 0.0
        self.session_id = ""
        self.sequence = 0
        self.generation = 0
        self.input_seq = 0
        self.input_mode = "released"
        self.input_seen = 0.0
        self.input_owner = None
        self.input_send = None
        self.takeover_ack = None
        self.revocations = deque(maxlen=32)
        self.outbox = OrderedDict()
        self.pending = {}
        self.inflight = {}
        self.ledger = OrderedDict()
        self.sources = set()
        self.dispatched = set()
        self.uncertain_commands = set()
        self.project_grant = None
        self.project_job = None
        self.task_grant = None
        self.recipe_catalog = {}
        self.catalog_total = 0
        self.catalog_loading = None
        self.afloat_job = None
        self.step_interrupt = 0
        self.history = deque(maxlen=32)
        self.last_event = 0
        self.last_binding = None
        self.retired = set()
        self.uncertain = False
        self.action_lock = asyncio.Lock()
        self.display = Display(config.get("display", ":91"))
        self.telemetry = deque(maxlen=1000)
        self.cached_catalog = None
        cache = resource("games/crafting_catalog.json")
        if cache.exists():
            try:
                self.cached_catalog = json.loads(cache.read_text())
            except (ValueError, OSError):
                pass

    def attach(self, owner):
        if self.owner and self.owner is not owner:
            raise RuntimeError("World already in use")
        self.owner = owner
        self.session_id = owner.session_id
        self.token = secrets.token_urlsafe(32)
        self.generation += 1
        self.input_mode = "released"
        self.sources.clear()
        return self.token

    def detach(self, owner):
        if self.owner is not owner:
            return
        self.revoke("session_closed")
        self.owner = None
        self.token = None
        self.session_id = ""
        self.input_mode = "released"
        self.input_seen = 0.0

    def authenticated(self, token):
        return bool(
            self.token
            and isinstance(token, str)
            and secrets.compare_digest(self.token, token)
        )

    def fresh(self):
        return bool(
            self.snapshot
            and time.monotonic() - self.received < 1.2
            and (self.snapshot["session_id"] == self.session_id)
        )

    def screen_status(self):
        s = self.snapshot
        if not s:
            return "waiting"
        if s["phase"] == "disconnected":
            return "recovering"
        if time.monotonic() - self.received > 1.2:
            return "syncing"
        return "ready"

    def continuity(self, grant):
        if (
            not grant
            or grant["generation"] != self.generation
            or time.monotonic() >= grant["deadline"]
        ):
            return "stop"
        if self.uncertain:
            return "stop"
        if (
            self.input_mode != "watch"
            or not self.input_owner
            or time.monotonic() - self.input_seen >= 1.2
            or (self.input_owner is not grant.get("stream_owner"))
            or (self.input_seq != grant.get("input_seq"))
        ):
            return "stop"
        if not self.fresh():
            return "wait"
        s = self.snapshot
        if (
            tuple((s[k] for k in IDENTITY)) != grant["identity"]
            or s["phase"] != "playing"
        ):
            return "stop"
        if s["control_epoch"] == grant["control_epoch"]:
            return "active"
        last = s.get("control", {}).get("last_revocation") or {}
        if (
            grant.get("native_stop") not in TRANSIENT_NATIVE_STOPS
            or last.get("reason") not in TRANSIENT_NATIVE_STOPS
        ):
            return "stop"
        control = s.get("control", {})
        if (
            control.get("input_seq") != self.input_seq
            or control.get("input_mode") != "watch"
        ):
            return "wait"
        if control.get("client_menu") or not control.get("client_focused"):
            return "stop"
        return "rearm"

    def origin(self, source_id, client=None):
        if not self.fresh():
            return None
        s = self.snapshot
        if client is not None:
            if not isinstance(client, dict):
                return None
            s = next(
                (
                    row
                    for row in reversed(self.history)
                    if all((client.get(k) == row[k] for k in ANCHOR))
                ),
                None,
            )
            if s is None:
                return None
        return {
            "anchor": {k: s[k] for k in ANCHOR},
            "snapshot": copy.deepcopy(s),
            "generation": self.generation,
            "session_id": self.session_id,
            "source_event_id": source_id,
            "received": self.received,
            "input_seq": self.input_seq,
            "pending_input_sync": s.get("control", {}).get("input_seq", self.input_seq)
            != self.input_seq,
        }

    def synced_origin(self, origin):
        if self.current(origin):
            return origin
        if (
            origin
            and self.fresh()
            and (not self.uncertain)
            and (self.input_mode == "watch")
            and (origin["session_id"] == self.session_id)
            and all((self.snapshot[k] == origin["snapshot"][k] for k in IDENTITY))
        ):
            changes = [
                r for r in self.revocations if r["generation"] > origin["generation"]
            ]
            if (
                changes
                and [r["generation"] for r in changes]
                == list(range(origin["generation"] + 1, self.generation + 1))
                and all(
                    (
                        r["reason"] in {"task_completed", "project_completed"}
                        for r in changes
                    )
                )
                and (
                    self.snapshot.get("control", {}).get("input_seq") == self.input_seq
                )
            ):
                return self.origin(origin["source_event_id"])
        if (
            not origin
            or not origin.get("pending_input_sync")
            or (not self.fresh())
            or self.uncertain
        ):
            return None
        if (
            origin["session_id"] != self.session_id
            or origin["generation"] != self.generation
            or origin.get("input_seq") != self.input_seq
        ):
            return None
        s = self.snapshot
        old = origin["snapshot"]
        if (
            any((s[k] != old[k] for k in IDENTITY))
            or s.get("control", {}).get("input_seq") != self.input_seq
        ):
            return None
        before = old["view"]["self"]
        after = s["view"]["self"]
        if any(
            (
                before.get(k) != after.get(k)
                for k in ("position", "yaw", "pitch", "wielded", "inventory_summary")
            )
        ):
            return None
        return self.origin(origin["source_event_id"])

    def current(self, origin):
        s = self.snapshot
        return bool(
            origin
            and self.fresh()
            and (not self.uncertain)
            and (origin["session_id"] == self.session_id)
            and (origin["generation"] == self.generation)
            and all(
                (s[k] == origin["anchor"][k] for k in ANCHOR[:3] + ("control_epoch",))
            )
        )

    def same_owner(self, origin):
        return bool(
            origin
            and self.snapshot
            and (not self.uncertain)
            and (origin["session_id"] == self.session_id)
            and (origin["generation"] == self.generation)
            and (origin.get("input_seq") == self.input_seq)
            and all((self.snapshot[k] == origin["snapshot"][k] for k in IDENTITY))
            and self.input_owner
            and (self.input_mode == "watch")
            and (time.monotonic() - self.input_seen < 1.2)
        )

    async def takeover(self, origin):
        if self.input_mode != "look":
            return origin if self.current(origin) else None
        if (
            not origin
            or not self.fresh()
            or self.uncertain
            or (not self.input_send)
            or (not self.input_owner)
            or (self.input_seq != origin.get("input_seq"))
            or (self.generation != origin.get("generation"))
            or (origin["session_id"] != self.session_id)
            or any((self.snapshot[k] != origin["snapshot"][k] for k in IDENTITY))
        ):
            return None
        source = origin["source_event_id"]
        owner = self.input_owner
        self.input_mode = "watch"
        self.revoke("semantic_takeover")
        generation = self.generation
        sequence = self.input_seq
        token = secrets.token_hex(12)
        self.takeover_ack = {"id": token, "accepted": False}
        try:
            await self.display.release()
            await self.input_send({"type": "actor_takeover", "id": token})
            until = time.monotonic() + 2.5
            while time.monotonic() < until:
                if (
                    self.input_owner is not owner
                    or self.generation != generation
                    or self.input_mode != "watch"
                    or self.uncertain
                ):
                    return None
                s = self.snapshot
                if (
                    self.fresh()
                    and self.takeover_ack["accepted"]
                    and (s.get("control", {}).get("input_seq") == sequence)
                ):
                    own = s["view"]["self"]
                    first = s["snapshot_seq"]
                    await asyncio.sleep(0.12)
                    if (
                        self.snapshot["snapshot_seq"] > first
                        and self.generation == generation
                        and (self.input_mode == "watch")
                    ):
                        after = self.snapshot["view"]["self"]
                        if after["position"] == own["position"]:
                            self.emit("takeover", source=source, mode="watch")
                            return self.origin(source)
                await asyncio.sleep(0.04)
        finally:
            self.takeover_ack = None
        return None

    def emit(self, kind, **payload):
        if self.owner:
            self.owner.post("voxel.event", {"kind": kind, **payload})

    def revoke(self, reason):
        self.generation += 1
        self.input_seq += 1
        self.revocations.append({"generation": self.generation, "reason": reason})
        self.project_grant = None
        self.task_grant = None
        for key, message in list(self.outbox.items()):
            if message["kind"] in {
                "ARM",
                "COMMIT",
                "SUBMIT",
                "PROJECT_ARM",
                "PROJECT_AIM",
                "PROJECT_STEP",
                "TASK_ARM",
                "TASK_AIM",
                "TASK_STEP",
            }:
                self.outbox.pop(key)
                if key in self.dispatched and message["kind"] in {
                    "SUBMIT",
                    "COMMIT",
                    "PROJECT_STEP",
                    "TASK_STEP",
                }:
                    continue
                future = self.pending.pop(key, None)
                if future and (not future.done()):
                    future.set_result({"status": "CANCELLED", "reason": reason})
        self._queue("REVOKE", {"reason": reason}, wait=False)
        self.emit("control", mode="MANUAL", reason=reason)
        if reason in {
            "task_completed",
            "project_completed",
            "task_deadline",
            "project_deadline",
            "semantic_reclaim",
            "semantic_task_reclaim",
            "button_reclaim",
            "button_project_pause",
            "task_cancelled_by_user",
            "project_cancelled_by_user",
        }:
            if self.input_mode == "watch" and self.input_owner and self.input_send:
                self.input_mode = "look"
                owner = self.input_owner

                async def returned():
                    if (
                        self.input_mode == "look"
                        and self.input_owner is owner
                        and self.input_send
                    ):
                        try:
                            await self.input_send(
                                {"type": "actor_returned", "reason": reason}
                            )
                        except (WebSocketDisconnect, TimeoutError, RuntimeError):
                            pass

                asyncio.create_task(returned())

    def _queue(self, kind, values, wait=True):
        if kind == "REVOKE":
            for key, old in list(self.outbox.items()):
                if old["kind"] == "REVOKE" and key not in self.pending:
                    self.outbox.pop(key)
        if len(self.outbox) >= 24:
            self.uncertain = True
            raise RuntimeError("Control outbox full")
        self.sequence += 1
        key = secrets.token_hex(16)
        message = {
            "kind": kind,
            "request_id": key,
            "seq": self.sequence,
            "session_id": self.session_id,
            **values,
        }
        self.outbox[key] = message
        if wait:
            future = asyncio.get_running_loop().create_future()
            self.pending[key] = future
            self.inflight[key] = message
            return (key, future)
        return (key, None)

    async def request(self, kind, values, timeout=2.0):
        key, future = self._queue(kind, values)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(key, None)
            self.inflight.pop(key, None)
            self.outbox.pop(key, None)
            self.dispatched.discard(key)

    async def execute(self, origin, action_id):
        async with self.action_lock:
            if not self.current(origin) or origin["source_event_id"] in self.sources:
                return {"status": "REJECTED", "reason": "stale_source"}
            if len(self.sources) >= 512:
                return {"status": "REJECTED", "reason": "session_source_limit"}
            if not any(
                (
                    a["action_id"] == action_id
                    for a in origin["snapshot"]["action_candidates"]
                )
            ):
                return {"status": "REJECTED", "reason": "invalid_candidate"}
            self.sources.add(origin["source_event_id"])
            args = {k: origin[k] for k in ("anchor", "source_event_id")}
            args.update(grant_id=secrets.token_hex(16))
            self.emit("control", mode="ARMING")
            committed = False
            command = secrets.token_hex(16)
            try:
                committed = True
                result = await self.request(
                    "SUBMIT", {**args, "action_id": action_id, "command_id": command}
                )
                if result["status"] == "UNKNOWN":
                    self.uncertain = True
                    self.uncertain_commands.add(command)
                    result = await self.reconcile(command)
                return result
            except asyncio.CancelledError:
                if committed:
                    self.uncertain = True
                    self.uncertain_commands.add(command)
                    self.emit(
                        "unknown", command_id=command, reason="inflight_task_cancelled"
                    )
                raise
            except (TimeoutError, ConnectionError):
                if committed:
                    self.uncertain = True
                    self.uncertain_commands.add(command)
                    self.emit("unknown", command_id=command)
                    return await self.reconcile(command)
                self.revoke("arm_timeout")
                return {"status": "REJECTED", "reason": "arm_timeout"}
            finally:
                self.emit("control", mode="MANUAL", uncertain=self.uncertain)

    async def reconcile(self, command):
        try:
            answer = await self.request("QUERY", {"command_id": command}, timeout=5)
            if answer["status"] in {"SUCCEEDED", "REJECTED", "CANCELLED"}:
                self.uncertain_commands.discard(command)
                self.uncertain = bool(self.uncertain_commands)
                self.emit("reconciled", result=answer)
                return answer
        except (TimeoutError, ConnectionError):
            pass
        return {"status": "UNKNOWN", "command_id": command}

    async def arm_project(self, project, plan, origin, duration_ms=900000):
        if not self.current(origin):
            return {"status": "REJECTED", "reason": "source_changed"}
        grant = secrets.token_hex(16)
        generation = self.generation
        started = time.monotonic()
        reply = await self.request(
            "PROJECT_ARM",
            {
                "project_id": project.project_id,
                "revision": project.revision,
                "grant_id": grant,
                "source_event_id": origin["source_event_id"],
                "anchor": origin["anchor"],
                "cells": plan.absolute(),
                "origin": plan.origin,
                "duration_ms": duration_ms,
                "receipts": [
                    r["command_id"]
                    for r in project.completed
                    if r.get("command_id") and (not r.get("removed"))
                ][-256:],
            },
            timeout=5,
        )
        if reply.get("status") == "ARMED":
            if generation != self.generation or (
                not self.current(origin) and (not self.same_owner(origin))
            ):
                self.revoke("late_project_arm")
                return {"status": "CANCELLED", "reason": "source_changed"}
            self.project_grant = {
                "id": grant,
                "project_id": project.project_id,
                "generation": generation,
                "deadline": started + duration_ms / 1000,
                "identity": tuple((origin["snapshot"][k] for k in IDENTITY)),
                "control_epoch": origin["anchor"]["control_epoch"],
                "input_seq": self.input_seq,
                "stream_owner": self.input_owner,
            }
        return reply

    def project_current(self):
        g = self.project_grant
        return bool(
            g
            and self.fresh()
            and (not self.uncertain)
            and (g["generation"] == self.generation)
            and (time.monotonic() < g["deadline"])
            and (tuple((self.snapshot[k] for k in IDENTITY)) == g["identity"])
            and (self.snapshot["control_epoch"] == g["control_epoch"])
        )

    def facing(self, target, movement=False):
        p = self.snapshot["view"]["self"]
        eye = p.get("eye_position") or {
            **p["position"],
            "y": p["position"]["y"] + 1.625,
        }
        dx, dz = (target["x"] - eye["x"], target["z"] - eye["z"])
        horizontal = math.hypot(dx, dz)
        yaw = math.atan2(-dx, dz)
        pitch = (
            math.radians(8)
            if movement
            else -math.atan2(target["y"] - eye["y"], horizontal)
        )
        delta = abs((yaw - p.get("yaw", 0) + math.pi) % (2 * math.pi) - math.pi)
        return (horizontal < 0.15 or delta < 0.12) and abs(
            pitch - p.get("pitch", 0)
        ) < 0.16

    async def aim_action(self, action, snapshot, project=False):
        kinds = (
            {"PROJECT_PLACE", "PROJECT_JUMP_PLACE"}
            if project
            else {"DIG", "DIG_DOWN", "PLACE", "JUMP_PLACE", "USE", "ATTACK"}
        )
        if action["kind"] not in kinds or action.get("interaction") == "self":
            return (action, snapshot)
        target = action.get("target")
        if not target and action.get("target_id"):
            target = next(
                (
                    e["position"]
                    for e in snapshot["view"].get("survival", {}).get("entities", [])
                    if e.get("target_id") == action["target_id"]
                ),
                None,
            )
        if not target or self.facing(target):
            return (action, snapshot)
        grant = self.project_grant if project else self.task_grant
        current = self.project_current if project else self.task_current
        if not grant:
            return (None, snapshot)
        key = "project_id" if project else "task_id"
        interrupt = self.step_interrupt
        self.emit("execution", state="aiming", action=action["kind"], target=target)
        try:
            reply = await self.request(
                "PROJECT_AIM" if project else "TASK_AIM",
                {
                    key: grant[key],
                    "grant_id": grant["id"],
                    "action_id": action["action_id"],
                    "anchor": {k: snapshot[k] for k in ANCHOR},
                },
                timeout=2,
            )
        except (TimeoutError, ConnectionError):
            return (None, self.snapshot)
        if reply.get("status") != "AIMING":
            return (None, self.snapshot)
        target = reply["target"]
        until = min(grant["deadline"], time.monotonic() + 1.8)
        aligned_seq = None
        while (
            current()
            and interrupt == self.step_interrupt
            and (time.monotonic() < until)
        ):
            if self.facing(target):
                if aligned_seq is None:
                    aligned_seq = self.snapshot["snapshot_seq"]
                elif self.snapshot["snapshot_seq"] > aligned_seq:
                    s = self.snapshot
                    branch = (
                        s["view"]
                        .get("construction" if project else "survival", {})
                        .get("project" if project else "task")
                        or {}
                    )
                    from .operations import Operations

                    same = (
                        (
                            lambda a: all(
                                (
                                    a.get(k) == action.get(k)
                                    for k in (
                                        "kind",
                                        "cell",
                                        "item_id",
                                        "target",
                                        "slot",
                                    )
                                )
                            )
                        )
                        if project
                        else lambda a: Operations.same_action(action, a)
                    )
                    selected = next(
                        (a for a in branch.get("actions", []) if same(a)), None
                    )
                    if selected:
                        self.emit(
                            "camera.aligned",
                            action_kind=action["kind"],
                            target=target,
                            yaw=s["view"]["self"].get("yaw"),
                            pitch=s["view"]["self"].get("pitch"),
                        )
                        return (selected, s)
            else:
                aligned_seq = None
            await asyncio.sleep(0.035)
        self.emit("camera.wait_failed", action_kind=action["kind"], target=target)
        return (None, self.snapshot)

    async def project_step(self, action, snapshot=None):
        async with self.action_lock:
            if not self.project_current():
                return {"status": "CANCELLED", "reason": "project_not_active"}
            action, snapshot = await self.aim_action(
                action, snapshot or self.snapshot, project=True
            )
            if action is None:
                return {"status": "REJECTED", "reason": "camera_alignment_pending"}
            g = dict(self.project_grant)
            s = snapshot or self.snapshot
            command = secrets.token_hex(16)
            interrupt = self.step_interrupt
            self.emit(
                "execution",
                state="executing",
                action=action["kind"],
                target=action.get("target"),
            )
            args = {
                "project_id": g["project_id"],
                "grant_id": g["id"],
                "command_id": command,
                "action_id": action["action_id"],
                "anchor": {k: s[k] for k in ANCHOR},
            }
            try:
                reply = await self.request("PROJECT_STEP", args, timeout=5)
                if reply.get("status") == "INPUT_ARMED":
                    await self._project_input(reply, g, interrupt)
                    until = min(g["deadline"], time.monotonic() + 3)
                    while (
                        self.project_grant
                        and self.project_grant["id"] == g["id"]
                        and (not self.fresh())
                        and (time.monotonic() < until)
                    ):
                        await asyncio.sleep(0.04)
                    if not self.project_current():
                        if (
                            self.continuity(self.project_grant) == "stop"
                            and self.generation == g["generation"]
                        ):
                            self.revoke("bounded_input_interrupted")
                        known = self.ledger.get(command)
                        if known and known.get("status") in {
                            "SUCCEEDED",
                            "REJECTED",
                            "CANCELLED",
                            "UNKNOWN",
                        }:
                            return known
                        return await self.reconcile(command)
                    reply = await self.request(
                        "PROJECT_INPUT_DONE",
                        {k: args[k] for k in ("project_id", "grant_id", "command_id")},
                        timeout=5,
                    )
                if reply.get("status") == "UNKNOWN":
                    self.uncertain = True
                    self.uncertain_commands.add(command)
                return reply
            except asyncio.CancelledError:
                self.uncertain = True
                self.uncertain_commands.add(command)
                self.revoke("project_task_cancelled")
                raise
            except (TimeoutError, ConnectionError):
                self.uncertain = True
                self.uncertain_commands.add(command)
                return await self.reconcile(command)

    async def _project_input(self, operation, grant, interrupt=None):
        if operation["kind"] == "PROJECT_MOVE" and operation.get("jump"):
            operation = {**operation, "kind": "JUMP_MOVE"}
        was_afloat = bool(self.afloat_job)
        await self._stop_afloat()
        await self.display.release()
        try:
            active = self.task_current if grant.get("task_id") else self.project_current
            if interrupt is None:
                interrupt = self.step_interrupt

            def current():
                return self.step_interrupt == interrupt and active()

            if not current():
                return
            if operation["kind"] == "PROJECT_SELECT":
                code = "Digit" + str(operation["slot"])
                await self.display.input({"kind": "key", "code": code, "down": True})
                await asyncio.sleep(0.1)
                await self.display.input({"kind": "key", "code": code, "down": False})
                await asyncio.sleep(0.22)
            elif operation["kind"] in {"PROJECT_JUMP_PLACE", "JUMP_PLACE"}:
                await self.display.input({"kind": "key", "code": "Space", "down": True})
                await asyncio.sleep(0.22)
                await self.display.input(
                    {"kind": "key", "code": "Space", "down": False}
                )
                await asyncio.sleep(0.65)
            elif operation["kind"] == "LOOK":
                for _ in range(8):
                    if not current():
                        return
                    player = self.snapshot["view"]["self"]
                    delta = (operation["yaw"] - player["yaw"] + math.pi) % (
                        2 * math.pi
                    ) - math.pi
                    vertical = operation.get("pitch", 0.1) - player["pitch"]
                    if abs(delta) < 0.08 and abs(vertical) < 0.08:
                        break
                    await self.display.input(
                        {
                            "kind": "motion",
                            "x": max(-300, min(300, -delta / 0.00262)),
                            "y": max(-300, min(300, vertical / 0.00262)),
                        }
                    )
                    await asyncio.sleep(0.18)
            elif operation["kind"] == "USE" and operation.get("interaction") == "self":
                for _ in range(8):
                    if not current():
                        return
                    vertical = -1.45 - self.snapshot["view"]["self"]["pitch"]
                    if abs(vertical) < 0.1:
                        break
                    await self.display.input(
                        {
                            "kind": "motion",
                            "x": 0,
                            "y": max(-300, min(300, vertical / 0.00262)),
                        }
                    )
                    await asyncio.sleep(0.16)
                before = self.snapshot["view"]["self"].get("wielded", {}).copy()
                await self.display.input({"kind": "button", "button": 2, "down": True})
                until = min(grant["deadline"], time.monotonic() + 3.6)
                while current() and time.monotonic() < until:
                    await asyncio.sleep(0.04)
                    after = self.snapshot["view"]["self"].get("wielded", {})
                    if after.get("item_id") != before.get("item_id") or after.get(
                        "count", 0
                    ) < before.get("count", 0):
                        break
                await self.display.input({"kind": "button", "button": 2, "down": False})
            elif operation["kind"] in {"SWIM_UP", "JUMP"}:
                await self.display.input({"kind": "key", "code": "Space", "down": True})
                until = time.monotonic() + operation.get("duration_ms", 800) / 1000
                while current() and time.monotonic() < until:
                    await asyncio.sleep(0.05)
                    if (
                        operation["kind"] == "SWIM_UP"
                        and self.snapshot["view"]["self"].get("breath", 0) >= 9
                        and (
                            self.snapshot["view"]["self"].get("head_medium")
                            == "breathable"
                        )
                    ):
                        await asyncio.sleep(0.35)
                        break
                if (
                    operation["kind"] != "SWIM_UP"
                    or not current()
                    or self.snapshot["view"]["self"].get("head_medium") != "breathable"
                ):
                    await self.display.input(
                        {"kind": "key", "code": "Space", "down": False}
                    )
            elif operation["kind"] in {"PROJECT_MOVE", "JUMP_MOVE", "SWIM"}:
                target = operation["target"]
                started = time.monotonic()
                brisk = (
                    operation["kind"] == "PROJECT_MOVE"
                    and operation.get("safe_walk_margin") is True
                )
                swimming = operation["kind"] == "SWIM" or (
                    operation["kind"] == "JUMP_MOVE"
                    and bool(
                        was_afloat
                        or self.snapshot["view"].get("survival", {}).get("in_water")
                    )
                )
                if swimming:
                    await self.display.input(
                        {"kind": "key", "code": "Space", "down": True}
                    )
                if not swimming:
                    await self.display.input(
                        {"kind": "key", "code": "ShiftLeft", "down": True}
                    )
                    until = time.monotonic() + 1.8
                    while current() and time.monotonic() < until:
                        if self.facing(target, movement=True):
                            break
                        await asyncio.sleep(0.05)
                    if not current() or not self.facing(target, movement=True):
                        return
                    started = time.monotonic()
                descending = (
                    not swimming
                    and target["y"]
                    < self.snapshot["view"]["self"]["position"]["y"] - 0.4
                )
                if descending or brisk:
                    await self.display.input(
                        {"kind": "key", "code": "ShiftLeft", "down": False}
                    )
                while current() and time.monotonic() - started < 3.2:
                    task = getattr(
                        getattr(getattr(self.owner, "voxel", None), "operations", None),
                        "task",
                        None,
                    )
                    if (
                        task
                        and task.get("urgent_goal")
                        and (operation["kind"] == "PROJECT_MOVE")
                        and (not operation.get("emergency"))
                    ):
                        break
                    p = self.snapshot["view"]["self"]
                    pos = p["position"]
                    dx = target["x"] - pos["x"]
                    dz = target["z"] - pos["z"]
                    if not swimming and pos["y"] < target["y"] - (
                        1.35 if operation["kind"] == "JUMP_MOVE" else 0.7
                    ):
                        break
                    distance = math.hypot(dx, dz)
                    if distance < (0.4 if brisk else 0.1 if descending else 0.16):
                        if (
                            operation["kind"] == "SWIM"
                            or abs(pos["y"] - target["y"]) < 0.45
                        ):
                            break
                        if descending:
                            await self.display.input(
                                {"kind": "key", "code": "ShiftLeft", "down": False}
                            )
                        await asyncio.sleep(0.05)
                        continue
                    yaw = p["yaw"]
                    forward = -dx * math.sin(yaw) + dz * math.cos(yaw)
                    right = dx * math.cos(yaw) + dz * math.sin(yaw)
                    keys = set()
                    if abs(forward) > (0.055 if descending else 0.1):
                        keys.add("KeyW" if forward > 0 else "KeyS")
                    if abs(right) > (0.055 if descending else 0.1):
                        keys.add("KeyD" if right > 0 else "KeyA")
                    if operation["kind"] == "JUMP_MOVE" and (not swimming):
                        keys.add("Space")
                    if not swimming and (not descending) and (not brisk):
                        keys.add("ShiftLeft")
                    seq = self.snapshot["snapshot_seq"]
                    for key in keys:
                        if not current():
                            return
                        await self.display.input(
                            {"kind": "key", "code": key, "down": True}
                        )
                    speed = 4.3 if brisk or swimming or descending else 2.0
                    pulse_limit = (
                        0.05
                        if descending
                        else 0.1
                        if brisk
                        else 0.14
                        if swimming
                        else 0.16
                    )
                    await asyncio.sleep(
                        min(pulse_limit, max(0.025, (distance - 0.12) / speed))
                    )
                    for key in keys - {"ShiftLeft"}:
                        await self.display.input(
                            {"kind": "key", "code": key, "down": False}
                        )
                    if brisk:
                        await self.display.input(
                            {"kind": "key", "code": "ShiftLeft", "down": True}
                        )
                    await asyncio.sleep(0.08 if brisk else 0.12)
                    until = time.monotonic() + 0.45
                    while (
                        current()
                        and self.snapshot["snapshot_seq"] <= seq
                        and (time.monotonic() < until)
                    ):
                        await asyncio.sleep(0.03)
                    if (descending or brisk) and current():
                        await self.display.input(
                            {"kind": "key", "code": "ShiftLeft", "down": False}
                        )
        finally:
            water = self.snapshot["view"].get("survival", {})
            keep_afloat = (
                operation["kind"] in {"SWIM_UP", "SWIM", "JUMP_MOVE"}
                and current()
                and (not water.get("dry_land", False))
                and (
                    water.get("in_water", False)
                    or operation["kind"] in {"SWIM_UP", "SWIM"}
                )
            )
            if keep_afloat:
                await self.display.input({"kind": "key", "code": "Space", "down": True})
                self.afloat_job = asyncio.create_task(self._hold_afloat(grant))
            else:
                await self.display.release()
            await asyncio.sleep(0.16 if operation.get("safe_walk_margin") else 0.35)

    async def _stop_afloat(self):
        job = self.afloat_job
        self.afloat_job = None
        if job and job is not asyncio.current_task():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def interrupt_step(self, reason):
        self.step_interrupt += 1
        grant = self.task_grant or self.project_grant
        if not grant:
            return
        await self._stop_afloat()
        await self.display.release()
        if grant is not self.task_grant and grant is not self.project_grant:
            return
        key = "task_id" if grant.get("task_id") else "project_id"
        try:
            await self.request(
                "TASK_CANCEL_STEP" if key == "task_id" else "PROJECT_CANCEL_STEP",
                {key: grant[key], "grant_id": grant["id"], "reason": reason},
                timeout=2,
            )
        except (TimeoutError, ConnectionError):
            self.emit("step_interrupt_pending", reason=reason)

    async def _hold_afloat(self, grant):
        try:
            while self.task_current() and self.task_grant["id"] == grant["id"]:
                if self.snapshot["view"].get("survival", {}).get("dry_land"):
                    break
                await asyncio.sleep(0.05)
        finally:
            await self.display.input({"kind": "key", "code": "Space", "down": False})
            if self.afloat_job is asyncio.current_task():
                self.afloat_job = None

    async def load_recipes(self):
        if (
            self.cached_catalog
            and self.snapshot
            and (
                self.cached_catalog.get("game_build") == self.snapshot.get("game_build")
            )
        ):
            self.recipe_catalog = {
                r["recipe_id"]: r for r in self.cached_catalog["recipes"]
            }
            self.catalog_total = self.cached_catalog["total"]
            if len(self.recipe_catalog) == self.catalog_total:
                return self.recipe_catalog
        if self.catalog_loading and self.catalog_loading is not asyncio.current_task():
            return await self.catalog_loading
        self.catalog_loading = asyncio.current_task()
        try:
            for _ in range(180):
                reply = await self.request("TASK_CATALOG", {})
                page = reply.get("catalog") or {}
                self.catalog_total = page.get("total", 0)
                for row in page.get("recipes") or []:
                    self.recipe_catalog[row["recipe_id"]] = {
                        k: row[k]
                        for k in (
                            "recipe_id",
                            "item_id",
                            "name",
                            "count",
                            "width",
                            "grid",
                            "ingredients",
                        )
                    }
                if (
                    self.catalog_total
                    and len(self.recipe_catalog) >= self.catalog_total
                ):
                    return self.recipe_catalog
            raise RuntimeError("Recipe catalog incomplete")
        finally:
            self.catalog_loading = None

    def task_current(self):
        g = self.task_grant
        return bool(
            g
            and self.fresh()
            and (not self.uncertain)
            and (g["generation"] == self.generation)
            and (time.monotonic() < g["deadline"])
            and (tuple((self.snapshot[k] for k in IDENTITY)) == g["identity"])
            and (self.snapshot["control_epoch"] == g["control_epoch"])
        )

    async def arm_task(self, task, origin):
        if not self.current(origin):
            return {"status": "REJECTED", "reason": "source_changed"}
        from .operations import Operations

        if not Operations.scope_ready(task):
            return {"status": "REJECTED", "reason": "invalid_scope"}
        generation = self.generation
        grant = secrets.token_hex(16)
        started = time.monotonic()
        duration = task["duration_ms"]
        reply = await self.request(
            "TASK_ARM",
            {
                "task_id": task["id"],
                "grant_id": grant,
                "source_event_id": origin["source_event_id"],
                "anchor": origin["anchor"],
                "duration_ms": duration,
                "scope": task["scope"],
                "max_effects": max(0, task["quantity"] - task.get("completed", 0))
                if task.get("quantity")
                and task["scope"]["kind"] not in {"AGENT", "TRAVEL"}
                else 2048,
            },
            timeout=5,
        )
        if reply.get("status") == "ARMED":
            if generation != self.generation or (
                not self.current(origin) and (not self.same_owner(origin))
            ):
                self.revoke("late_task_arm")
                return {"status": "CANCELLED"}
            self.task_grant = {
                "id": grant,
                "task_id": task["id"],
                "generation": generation,
                "deadline": started + duration / 1000,
                "identity": tuple((origin["snapshot"][k] for k in IDENTITY)),
                "control_epoch": origin["anchor"]["control_epoch"],
                "input_seq": self.input_seq,
                "stream_owner": self.input_owner,
            }
        return reply

    async def task_step(self, action, snapshot):
        async with self.action_lock:
            if not self.task_current():
                return {"status": "CANCELLED", "reason": "task_not_active"}
            action, snapshot = await self.aim_action(action, snapshot)
            if action is None:
                return {"status": "REJECTED", "reason": "camera_alignment_pending"}
            g = dict(self.task_grant)
            command = secrets.token_hex(16)
            interrupt = self.step_interrupt
            self.emit(
                "execution",
                state="executing",
                action=action["kind"],
                target=action.get("target"),
            )
            args = {
                "task_id": g["task_id"],
                "grant_id": g["id"],
                "command_id": command,
                "action_id": action["action_id"],
                "anchor": {k: snapshot[k] for k in ANCHOR},
            }
            observed = copy.deepcopy(snapshot)
            try:
                reply = await self.request("TASK_STEP", args, timeout=5)
                if reply.get("status") == "INPUT_ARMED":
                    if action.get("emergency"):
                        reply = {**reply, "emergency": True}
                    await self._project_input(reply, g, interrupt)
                    until = min(g["deadline"], time.monotonic() + 3)
                    while (
                        self.task_grant
                        and self.task_grant["id"] == g["id"]
                        and (not self.fresh())
                        and (time.monotonic() < until)
                    ):
                        await asyncio.sleep(0.04)
                    if self.task_current():
                        reply = await self.request(
                            "TASK_INPUT_DONE",
                            {k: args[k] for k in ("task_id", "grant_id", "command_id")},
                            timeout=5,
                        )
                    else:
                        if (
                            self.continuity(self.task_grant) == "stop"
                            and self.generation == g["generation"]
                        ):
                            self.revoke("bounded_task_input_interrupted")
                        known = self.ledger.get(command)
                        reply = (
                            known
                            if known
                            and known.get("status")
                            in {"SUCCEEDED", "REJECTED", "CANCELLED"}
                            else await self.reconcile(command)
                        )
                if reply.get("status") == "RUNNING":
                    until = time.monotonic() + min(
                        25, reply.get("wait_ms", 1000) / 1000 + 8
                    )
                    while time.monotonic() < until:
                        await asyncio.sleep(0.25)
                        reply = await self.request(
                            "QUERY", {"command_id": command}, timeout=5
                        )
                        if reply.get("status") != "RUNNING":
                            break
                if reply.get("status") in {"RUNNING", "UNKNOWN"}:
                    self.uncertain = True
                    self.uncertain_commands.add(command)
                    reply = await self.reconcile(command)
                return attack_receipt(reply, action, observed, command)
            except asyncio.CancelledError:
                self.revoke("task_cancelled")
                self.uncertain = True
                self.uncertain_commands.add(command)
                raise
            except (TimeoutError, ConnectionError):
                self.uncertain = True
                self.uncertain_commands.add(command)
                return attack_receipt(
                    await self.reconcile(command), action, observed, command
                )

    def exchange(self, data):
        s = snapshot(data["snapshot"])
        binding = tuple((s[k] for k in IDENTITY))
        if binding in self.retired:
            raise ValueError("Retired game binding")
        if self.last_binding and binding != self.last_binding:
            self.retired.add(self.last_binding)
            if len(self.retired) > 256:
                self.retired = set(list(self.retired)[-128:])
            self.generation += 1
            self.history.clear()
            self.last_event = 0
            for key, f in self.pending.items():
                if not f.done():
                    msg = self.inflight.get(key, {})
                    unknown = key in self.dispatched and msg.get("kind") in {
                        "SUBMIT",
                        "COMMIT",
                        "PROJECT_STEP",
                        "TASK_STEP",
                    }
                    if unknown:
                        self.uncertain = True
                        self.uncertain_commands.add(msg.get("command_id"))
                    f.set_result(
                        {
                            "status": "UNKNOWN" if unknown else "CANCELLED",
                            "reason": "binding_changed",
                            "command_id": msg.get("command_id"),
                        }
                    )
            self.pending.clear()
            self.outbox.clear()
            self.dispatched.clear()
            self.project_grant = None
            self.task_grant = None
            self.recipe_catalog = {}
            self.catalog_total = 0
        self.last_binding = binding
        if (
            self.snapshot
            and binding == tuple((self.snapshot[k] for k in IDENTITY))
            and (s["snapshot_seq"] <= self.snapshot["snapshot_seq"])
        ):
            raise ValueError("Snapshot out of order")
        now = time.monotonic()
        if self.received:
            self.telemetry.append({"exchange_gap_ms": (now - self.received) * 1000})
        self.snapshot = s
        self.received = now
        self.history.append(s)
        self.emit("snapshot", snapshot=s)
        for e in data.get("events") or []:
            if e["event_seq"] > self.last_event:
                info = e.get("payload") or {}
                for g, kind, key in (
                    (self.task_grant, "task_stopped", "task_id"),
                    (self.project_grant, "project_stopped", "project_id"),
                ):
                    if g and e.get("kind") == kind and (info.get(key) == g.get(key)):
                        g.setdefault("native_stop", info.get("reason"))
                self.last_event = e["event_seq"]
                self.emit("native", event=e)
        acknowledged = []
        for row in data.get("results") or []:
            key = row.get("request_id")
            acknowledged.append(key)
            self.outbox.pop(key, None)
            future = self.pending.pop(key, None)
            if future and (not future.done()):
                future.set_result(row)
            if row.get("command_id"):
                self.ledger[row["command_id"]] = row
                if row["command_id"] in self.uncertain_commands and row.get(
                    "status"
                ) in {"SUCCEEDED", "REJECTED", "CANCELLED"}:
                    self.uncertain_commands.discard(row["command_id"])
                    self.uncertain = bool(self.uncertain_commands)
                while len(self.ledger) > 256:
                    self.ledger.popitem(last=False)
            self.emit("result", result=row)
        input_alive = bool(self.input_owner and now - self.input_seen < 1.2)
        commands = (
            list(self.outbox.values())[:8] if s["session_id"] == self.session_id else []
        )
        self.dispatched.update((m["request_id"] for m in commands))
        return {
            "game_instance_id": s["game_instance_id"],
            "bridge_epoch": s["bridge_epoch"],
            "session_id": self.session_id,
            "ack_events": self.last_event,
            "ack_results": acknowledged,
            "input": {
                "seq": self.input_seq,
                "mode": self.input_mode,
                "alive": input_alive,
            },
            "commands": commands,
        }


def attach(app, registry, config):
    from .origin import VoxelOriginGuard

    app.add_middleware(VoxelOriginGuard)
    root = Path(config["root"])
    options = config["voxel"]
    bridge = VoxelBridge(options)
    registry.voxel = bridge
    app.state.voxel_bridge = bridge
    if options.get("recover_client"):
        from .recovery import ClientRecovery
        from contextlib import asynccontextmanager

        recovery = ClientRecovery(bridge, root)
        app.state.voxel_client_recovery = recovery
        previous_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def with_recovery(app):
            async with previous_lifespan(app):
                job = asyncio.create_task(recovery.run())
                try:
                    yield
                finally:
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)

        app.router.lifespan_context = with_recovery

    @app.get("/games/voxel/health")
    async def game_health():
        return {
            "screen": bridge.screen_status(),
            "phase": (bridge.snapshot or {}).get("phase"),
            "recovery": getattr(
                getattr(app.state, "voxel_client_recovery", None), "state", "disabled"
            ),
        }

    secret = (
        (root / options.get("secret_file", ".runtime/voxel/bridge.secret"))
        .read_text()
        .strip()
    )

    @app.post("/internal/voxel/exchange")
    async def exchange(request: Request):
        if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "Loopback required")
        if not secrets.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + secret
        ):
            raise HTTPException(403, "Bridge credential required")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_BYTES:
                raise HTTPException(413, "Bridge DTO too large")
        try:
            data = json.loads(raw)
            validate(data)
            return bridge.exchange(data)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Invalid bridge DTO")

    @app.websocket("/games/voxel/stream")
    async def stream(ws: WebSocket):
        origin = ws.headers.get("origin", "")
        if urlsplit(origin).netloc != ws.headers.get("host") or urlsplit(
            origin
        ).scheme not in {"http", "https"}:
            await ws.close(code=1008)
            return
        await ws.accept()
        jobs = []
        owner = object()
        receiver = None
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 5)
            if (
                not bridge.authenticated(hello.get("token"))
                or bridge.input_owner is not None
            ):
                await ws.close(code=1008)
                return
            bridge.input_owner = owner
            bridge.input_seen = time.monotonic()
            bridge.revoke("stream_connected")
            send_lock = asyncio.Lock()

            async def send_control(message):
                async with send_lock:
                    await asyncio.wait_for(ws.send_json(message), 1.0)

            bridge.input_send = send_control
            from .streaming import FrameWindow

            window_frames = FrameWindow(numbered=hello.get("frame_protocol") == 2)
            frame_ack = 0.0
            uses_ack = hello.get("frame_ack") is True

            async def frames():
                while bridge.authenticated(hello.get("token")):
                    if bridge.screen_status() not in {"ready", "syncing"}:
                        await asyncio.sleep(0.2)
                        continue
                    if uses_ack:
                        await window_frames.ready()
                    start = time.monotonic()
                    try:
                        frame = await asyncio.wait_for(
                            bridge.display.frame(
                                window_frames.quality, window_frames.width
                            ),
                            2.0,
                        )
                    except RuntimeError:
                        await asyncio.sleep(0.5)
                        continue
                    capture_ms = (time.monotonic() - start) * 1000
                    async with send_lock:
                        await asyncio.wait_for(
                            ws.send_bytes(window_frames.sent(frame, capture_ms)), 2.0
                        )
                    await asyncio.sleep(
                        max(0, window_frames.interval - (time.monotonic() - start))
                    )

            async def watchdog():
                last_status = None
                last_stats = 0.0
                while True:
                    await asyncio.sleep(0.4)
                    if not bridge.authenticated(hello.get("token")):
                        await ws.close(code=1000)
                        return
                    state = bridge.screen_status()
                    if state != last_status:
                        async with send_lock:
                            await asyncio.wait_for(
                                ws.send_json({"type": "game_status", "state": state}),
                                1.0,
                            )
                        if state in {"recovering", "waiting"}:
                            bridge.input_mode = "released"
                            bridge.revoke("game_screen_unavailable")
                            await bridge.display.release()
                        last_status = state
                    if time.monotonic() - last_stats >= 5:
                        last_stats = time.monotonic()
                        stats = window_frames.stats()
                        bridge.emit("stream.stats", **stats)
                        async with send_lock:
                            await asyncio.wait_for(
                                ws.send_json({"type": "stream_stats", **stats}), 1.0
                            )
                    if time.monotonic() - bridge.input_seen > 1.2:
                        if bridge.input_mode != "released":
                            await bridge.display.release()
                            bridge.input_mode = "released"
                            bridge.revoke("input_heartbeat_lost")

            from .audio import GameAudio

            game_audio = GameAudio("unix:" + str(root / ".runtime/voxel/pulse.sock"))

            async def sound():
                try:
                    while bridge.authenticated(hello.get("token")):
                        pcm = await game_audio.read()
                        if bridge.input_mode != "released":
                            import numpy as np

                            samples = np.frombuffer(pcm, dtype="<f4")
                            pcm = (
                                (np.clip(samples, -1, 1) * 32767)
                                .astype("<i2")
                                .tobytes()
                            )
                            async with send_lock:
                                await asyncio.wait_for(
                                    ws.send_json(
                                        {
                                            "type": "game_audio",
                                            "pcm": base64.b64encode(pcm).decode(),
                                            "sample_rate": 24000,
                                            "channels": 2,
                                            "format": "s16le",
                                            "sample_count": len(samples),
                                        }
                                    ),
                                    1.0,
                                )
                except (RuntimeError, OSError):
                    pass
                finally:
                    await game_audio.close()

            jobs = [
                asyncio.create_task(frames()),
                asyncio.create_task(watchdog()),
                asyncio.create_task(sound()),
            ]
            window = time.monotonic()
            count = 0
            last_seq = 0
            while bridge.authenticated(hello.get("token")):
                receiver = asyncio.create_task(ws.receive_text())
                done, _ = await asyncio.wait(
                    [receiver, *jobs[:2]], return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if task is not receiver:
                        task.result()
                        return
                raw = receiver.result()
                receiver = None
                if len(raw) > 1024:
                    raise ValueError("Input too large")
                event = json.loads(raw)
                validate(event)
                count += 1
                if time.monotonic() - window >= 1:
                    window = time.monotonic()
                    count = 1
                if count > 180:
                    raise ValueError("Input rate exceeded")
                if event.get("kind") == "heartbeat":
                    bridge.input_seen = time.monotonic()
                    continue
                if event.get("kind") == "frame_ack" and uses_ack:
                    frame_ack = time.monotonic()
                    window_frames.ack(event.get("frame_id"))
                    continue
                if event.get("kind") == "refresh_frame":
                    window_frames.refresh()
                    continue
                if event.get("kind") == "ping":
                    await send_control({"type": "pong", "id": event.get("id")})
                    continue
                if event.get("kind") == "takeover_ack":
                    if (
                        bridge.takeover_ack
                        and event.get("id") == bridge.takeover_ack["id"]
                    ):
                        bridge.takeover_ack["accepted"] = event.get("accepted") is True
                    continue
                seq = event.get("seq")
                if type(seq) is not int or seq <= last_seq:
                    raise ValueError("Input sequence required")
                last_seq = seq
                bridge.input_seen = time.monotonic()
                if event.get("kind") == "mode":
                    mode = (
                        event.get("mode")
                        if event.get("mode") in {"look", "menu", "watch"}
                        else "released"
                    )
                    if mode != bridge.input_mode:
                        bridge.input_mode = mode
                        bridge.revoke("input_mode_changed")
                        await bridge.display.release()
                    continue
                if event.get("kind") == "release":
                    bridge.input_mode = "released"
                elif bridge.input_mode not in {"look", "menu"}:
                    continue
                if event.get("kind") != "motion":
                    bridge.input_seq += 1
                    bridge.generation += 1
                try:
                    await bridge.display.input(event)
                except RuntimeError:
                    bridge.input_mode = "released"
                    bridge.revoke("game_window_unavailable")
                    await bridge.display.release()
                    async with send_lock:
                        await ws.send_json(
                            {"type": "game_status", "state": "recovering"}
                        )
                for task in jobs:
                    if task.done():
                        task.result()
        except (WebSocketDisconnect, TimeoutError, ValueError, RuntimeError):
            pass
        finally:
            if receiver:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            if bridge.input_owner is owner:
                bridge.input_owner = None
                bridge.input_send = None
                bridge.input_mode = "released"
                bridge.revoke("stream_closed")
                await bridge.display.release()
            try:
                await ws.close(code=1011)
            except (RuntimeError, WebSocketDisconnect):
                pass
