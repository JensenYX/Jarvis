from __future__ import annotations
from collections import deque, Counter
import copy
import time


class Activity:
    def __init__(self):
        self.events = deque(maxlen=48)
        self.blocks = {}
        self.version = 0
        self.last_judged = 0
        self.last_request = 0.0
        self.last_spoken = 0.0
        self.last_feedback = None
        self.seen = deque(maxlen=128)
        self.last_change = 0.0

    def observe(self, event):
        if event.get("event_id") in self.seen:
            return
        if event.get("kind") not in {
            "placed",
            "dug",
            "inventory_changed",
            "hp_change",
            "respawned",
        }:
            return
        if event.get("actor") == "jev":
            return
        self.seen.append(event.get("event_id"))
        self.events.append(copy.deepcopy(event))
        self.version += 1
        self.last_change = time.monotonic()
        pos = (event.get("payload") or {}).get("position")
        if pos:
            key = tuple((pos.get(k) for k in ("x", "y", "z")))
            if event["kind"] == "dug":
                self.blocks.pop(key, None)
            elif event["kind"] == "placed":
                self.blocks[key] = {
                    "position": copy.deepcopy(pos),
                    "item_id": event["payload"]["item_id"],
                    "event_id": event["event_id"],
                }
        while len(self.blocks) > 512:
            self.blocks.pop(next(iter(self.blocks)))

    def context(self):
        rows = list(self.blocks.values())
        bounds = None
        if rows:
            bounds = {
                k: [
                    min((r["position"][k] for r in rows)),
                    max((r["position"][k] for r in rows)),
                ]
                for k in ("x", "y", "z")
            }
        return {
            "bounds": bounds,
            "count": len(rows),
            "materials": dict(Counter((r["item_id"] for r in rows))),
            "recent_blocks": [
                [r["position"][k] for k in ("x", "y", "z")] + [r["item_id"]]
                for r in rows[-16:]
            ],
            "events": [
                {
                    k: e[k]
                    for k in ("event_id", "actor", "evidence", "kind", "payload")
                    if k in e
                }
                for e in list(self.events)[-6:]
            ],
            "last_feedback": self.last_feedback,
            "caveat": "Observed human changes, possibly historical. Not the whole building. Never assert unseen completion.",
        }

    def due(self):
        return (
            self.version != self.last_judged
            and time.monotonic() - self.last_request >= 2
            and (time.monotonic() - self.last_change >= 1.2)
        )


async def judge_activity(controller, snapshot, activity, project, history, memories):
    from .coach import dialogue
    from .construction import brief

    player = snapshot.get("view", {}).get("self", {})
    state = {
        "player": {k: player.get(k) for k in ("position", "hp", "hunger")},
        "activity": activity.context(),
        "project": brief(project),
        "history": dialogue(history),
        "memories": [
            {k: m.get(k) for k in ("text", "source", "observed_at")}
            for m in memories[-3:]
        ],
    }
    return await controller.judge_typed(
        "voxel_activity",
        state,
        {
            "meaning": {
                "type": "choice",
                "instructions": "What do the actual human events mean? Distinguish a recognizable construction milestone from routine placement. A few blocks alone do not prove a house.",
                "criteria": {
                    "BUILD_PROGRESS": "Recognizable wall/shape/roof progress",
                    "BUILD_COMPLETE": "Strong evidence of completing the stated structure/subgoal",
                    "REVISION": "Reworking/removing a prior creation",
                    "RESOURCE": "Relevant resource change",
                    "DANGER": "Observed survival issue",
                    "ROUTINE": "Nothing meaningfully new",
                },
            },
            "speak": {
                "type": "choice",
                "instructions": "Choose a natural companion response to the NEW human work. A coherent developing shape or newly finished section with no similar last_feedback merits one specific appreciative comment or suggestion, even if modest. If the user invites comments, engage with the actual change. Stay silent for isolated clicks, unchanged state or repetition; never claim unseen work.",
                "criteria": {
                    "PRAISE": "Specific appreciation of a human-created milestone",
                    "SUGGEST": "One useful evidence-based suggestion",
                    "ASK": "One useful goal/style question",
                    "SILENT": "Observe silently",
                },
            },
            "importance": {
                "type": "score",
                "instructions": "How useful is this as lasting world/project memory?",
                "criteria": [
                    "Routine transient repetition",
                    "Small immediate progress",
                    "Meaningful goal-related milestone",
                    "Distinct creation or durable landmark",
                ],
            },
            "grounded": {
                "type": "noul",
                "instructions": "Do these native observed changes provide concrete facts to refer to (material, shape/extent or modification), even if the whole building is unfinished? A complete house is NOT required. Do not assume unseen details; feedback can explicitly be about this small observed section.",
            },
        },
        deadline_ms=1500,
    )
