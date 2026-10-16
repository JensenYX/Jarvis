from __future__ import annotations
import time


class Episodes:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.identity = None
        self.active = {}
        self.pending = {}

    def reset(self):
        self.identity = None
        self.active.clear()
        self.pending.clear()

    def observe(self, current, previous):
        identity = tuple(
            (
                current.get(k)
                for k in ("game_instance_id", "player_life_id", "bridge_epoch")
            )
        )
        if identity != self.identity:
            self.reset()
            self.identity = identity
            return
        if not previous or current.get("phase") != "playing":
            self.pending.clear()
            return
        now = self.clock()
        p = current["view"].get("self", {})
        old = previous["view"].get("self", {})
        hp = p.get("hp")
        before = old.get("hp")
        maximum = p.get("hp_max") or 20
        hunger = p.get("hunger", {}).get("value")
        breath = p.get("breath")
        present = {}
        if hp is not None and 0 < hp <= maximum / 2:
            present["health_low"] = {"kind": "health_low", "hp": hp, "hp_max": maximum}
            if hp <= maximum / 4:
                present["health_critical"] = {
                    "kind": "health_critical",
                    "hp": hp,
                    "hp_max": maximum,
                }
        if (
            hunger is not None
            and p.get("hunger", {}).get("quality") == "game_api"
            and (hunger <= 8)
        ):
            present["hunger_low"] = {"kind": "hunger_low", "value": hunger}
            if hunger <= 3:
                present["hunger_critical"] = {
                    "kind": "hunger_critical",
                    "value": hunger,
                }
        if breath is not None and breath <= 3:
            present["breath_low"] = {
                "kind": "breath_low",
                "breath": breath,
                "breath_max": p.get("breath_max"),
            }
        if p.get("head_medium") == "drowning_medium":
            present["underwater"] = {"kind": "underwater", "breath": breath}
        enemies = current["view"].get("survival", {}).get("entities", [])
        if any(
            (
                e.get("relation") == "hostile_species"
                and e.get("distance", 100) > 0
                and (e.get("distance", 100) < 5)
                for e in enemies
            )
        ):
            present["nearby_threat"] = {
                "kind": "nearby_threat",
                "entities": [
                    e["kind"] for e in enemies if e.get("relation") == "hostile_species"
                ][:3],
            }
        phase = current["view"].get("environment", {}).get("day_phase")
        if phase in {"dusk", "night"}:
            present["night"] = {"kind": "night", "day_phase": phase}
        if hp is not None and before is not None and (hp < before):
            causes = [
                (e.get("payload") or {}).get("cause", "unknown")
                for e in current["view"].get("recent_events", [])
                if e.get("kind") == "hp_change"
                and (e.get("payload") or {}).get("change", 0) < 0
            ]
            cause = causes[-1] if causes else "unknown"
            key = "damage:" + cause
            present[key] = {
                "kind": "damage",
                "cause": cause,
                "hp_before": before,
                "hp": hp,
                "last_seen": now,
            }
        for key, event in self.active.items():
            resolved_drown = (
                key == "damage:drown" and breath is not None and (breath > 3)
            )
            if (
                key.startswith("damage:")
                and (not resolved_drown)
                and (now - event.get("last_seen", 0) < 30)
                and (hp is not None)
                and (hp < event["hp_before"])
            ):
                present.setdefault(key, event)
        for key in list(self.active):
            if key not in present:
                self.active.pop(key, None)
                self.pending.pop(key, None)
        for key, event in present.items():
            if key not in self.active:
                if key == "night" and previous["view"].get("environment", {}).get(
                    "day_phase"
                ) in {"night", "dusk"}:
                    continue
                self.pending[key] = event
            elif key in self.pending:
                self.pending[key] = event
            self.active[key] = event

    def take(self):
        values = list(self.pending.values())[:6]
        self.pending.clear()
        return values

    def still_relevant(self, evidence):
        kinds = {(e["kind"], e.get("cause")) for e in self.active.values()}
        return any(((e["kind"], e.get("cause")) in kinds for e in evidence))
