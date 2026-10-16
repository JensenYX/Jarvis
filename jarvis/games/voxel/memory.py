import json
import time


class WorldMemory:
    def __init__(self, store, user):
        self.store = store
        self.user = user
        self.world = None
        self.goal = None
        self.landmarks = []
        self.ids = {}
        self.preferences = {}
        self.cancelled = []
        self.discoveries = []

    async def load(self, world):
        self.world = world
        self.goal = None
        self.landmarks = []
        self.ids = {}
        self.preferences = {}
        self.cancelled = []
        self.discoveries = []
        records = await self.store.search(
            self.user, "voxel-world:" + world, limit=500, expand=False
        )
        for row in sorted(records, key=lambda x: x["updated_at"]):
            try:
                data = json.loads(row["text"])
            except (ValueError, TypeError):
                continue
            if (
                not isinstance(data, dict)
                or data.get("world_id") != world
                or data.get("game") != "voxel"
            ):
                continue
            if data.get("kind") == "goal":
                self.goal = data["text"]
                self.ids["goal"] = row["id"]
            elif data.get("kind") == "landmark":
                self.landmarks.append(data)
            elif data.get("kind", "").startswith("preference:"):
                self.preferences[data["kind"]] = data["text"]
                self.ids[data["kind"]] = row["id"]
            elif data.get("kind") == "cancelled_goal":
                self.cancelled.append(data["text"])
            elif data.get("kind") == "discovery":
                self.discoveries.append(data)
        self.landmarks = self.landmarks[-12:]
        self.discoveries = self.discoveries[-12:]

    async def save(
        self,
        kind,
        text,
        source,
        position=None,
        *,
        provenance="explicit_user",
        evidence_ids=None,
    ):
        if not self.world:
            return
        data = {
            "game": "voxel",
            "world_id": self.world,
            "kind": kind,
            "text": text[:600],
            "observed_at": time.time(),
            "source": provenance,
            "position": position,
            "source_event_ids": (evidence_ids or [source])[-12:],
        }
        previous = (
            self.ids.get(kind)
            if kind == "goal" or kind.startswith("preference:")
            else None
        )
        record = await self.store.write(
            self.user,
            {
                "kind": "task_summary",
                "action": "UPDATE" if previous else "ADD",
                "supersedes_id": previous,
                "text": json.dumps(data, ensure_ascii=False),
                "entities": ["voxel-world:" + self.world],
            },
            source,
        )
        if record and kind == "goal":
            self.ids["goal"] = record["id"]
            self.goal = text[:600]
        if record and kind == "landmark":
            self.landmarks = (self.landmarks + [data])[-12:]
        if record and kind.startswith("preference:"):
            self.preferences[kind] = text[:600]
            self.ids[kind] = record["id"]
        if record and kind == "cancelled_goal":
            self.cancelled = (self.cancelled + [text[:600]])[-6:]
        if record and kind == "discovery":
            self.discoveries = (self.discoveries + [data])[-12:]
        return record

    def context(self):
        return {
            "goal": self.goal,
            "preferences": self.preferences,
            "landmarks": self.landmarks[-4:],
            "cancelled_goals": self.cancelled[-4:],
            "recent_discoveries": [
                {"text": r["text"], "position": r.get("position")}
                for r in self.discoveries[-3:]
            ],
        }

    async def remember_request(self, controller, text, source):
        answer = await controller.judge_typed(
            "voxel_memory_intent",
            {"current_request": text, "saved": self.context()},
            {
                "kind": {
                    "type": "choice",
                    "instructions": "Is a lasting game preference explicitly stated by this user now? Store the exact source text only. 不要伤害动物/以后别挖脚下 are constraints; 喜欢木屋 is style; 先说计划/多提醒我 is interaction. Ordinary commands, acknowledgments, insults and transient state questions are NONE. Latest explicit preference overrides same category.",
                    "criteria": {
                        "NONE": "No durable preference",
                        "playstyle": "Longer term exploration/building/play preference",
                        "construction": "Design/material/style preference",
                        "constraints": "Boundaries/exclusions to follow during gameplay",
                        "interaction": "How much and when to discuss plans/advice",
                    },
                }
            },
            deadline_ms=1200,
        )
        kind = answer["kind"]["choice"]
        if kind != "NONE" and answer["kind"]["confidence"] >= 0.7:
            await self.save("preference:" + kind, text, source)
