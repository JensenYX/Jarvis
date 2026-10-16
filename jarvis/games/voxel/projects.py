from __future__ import annotations
from dataclasses import dataclass, field, asdict
import asyncio
import copy
import hashlib
import json
import time
from uuid import uuid4


@dataclass
class Project:
    project_id: str = field(default_factory=lambda: uuid4().hex)
    world_id: str = ""
    title: str = "一起建造"
    revision: int = 1
    status: str = "DISCUSSING"
    requests: list = field(default_factory=list)
    requirements: dict = field(default_factory=dict)
    question: dict | None = None
    site: dict | None = None
    blueprint: dict | None = None
    completed: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    authorization: dict | None = None
    last_feedback: str | None = None
    preparation: dict | None = None
    execution_intent: bool = False
    design_delegated: bool = False
    work_visits: dict = field(default_factory=dict)
    last_progress: float = 0.0

    def completed_count(self):
        if not self.blueprint or not self.site:
            return 0
        from .blueprint import parse_blueprint

        try:
            plan = parse_blueprint(
                self.blueprint, set(self.blueprint["palette"].values()), self.site
            )
        except (ValueError, KeyError):
            return 0
        wanted = {(c["x"], c["y"], c["z"], c["item_id"]) for c in plan.absolute()}
        observed = {
            (r["target"]["x"], r["target"]["y"], r["target"]["z"], r["item_id"])
            for r in self.completed
            if r.get("target") and r.get("item_id") and (not r.get("removed"))
        }
        return len(wanted & observed)

    def context(self, compact=False):
        blueprint = copy.deepcopy(self.blueprint)
        if compact and blueprint:
            blueprint = {
                k: blueprint[k]
                for k in ("title", "size", "count", "materials")
                if k in blueprint
            }
        return {
            "id": self.project_id,
            "title": self.title,
            "revision": self.revision,
            "status": self.status,
            "requests": copy.deepcopy(self.requests[-(4 if compact else 12) :]),
            "requirements": copy.deepcopy(self.requirements),
            "pending_question": copy.deepcopy(self.question),
            "site": copy.deepcopy(self.site),
            "blueprint": blueprint,
            "completed_count": self.completed_count(),
            "execution_requested": bool(self.authorization),
            "preparation": copy.deepcopy(self.preparation),
            "recent_facts": copy.deepcopy(self.observations[-(2 if compact else 8) :]),
        }

    def apply_request(self, text, source):
        self.requests.append(
            {"text": text[:800], "source_event_id": source, "at": time.time()}
        )
        self.requests = self.requests[-24:]
        self.revision += 1
        self.status = "DISCUSSING"
        self.authorization = None


class ProjectStore:
    def __init__(self, memory, user_id):
        self.memory = memory
        self.user = user_id
        self.lock = asyncio.Lock()

    async def save(self, project, source, *, current=True):
        data = copy.deepcopy(asdict(project))
        data["authorization"] = None
        data["execution_intent"] = False
        observed_at = time.time()
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        chunks = []
        start = 0
        while start < len(raw):
            end = min(start + 1300, len(raw))

            def encode(end):
                return json.dumps(
                    {
                        "kind": "voxel_project",
                        "index": len(chunks),
                        "digest": digest,
                        "data": raw[start:end],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            text = encode(end)
            while len(text) > 1950:
                end -= 100
                text = encode(end)
            chunks.append(text)
            start = end
        async with self.lock:
            await self.memory.replace_task_bundle(
                self.user,
                "voxel-project-id:" + project.world_id + ":" + project.project_id,
                chunks,
                source,
                observed_at,
            )
            if current:
                return await self.memory.replace_task_bundle(
                    self.user,
                    "voxel-project:" + project.world_id,
                    chunks,
                    source,
                    observed_at,
                )

    async def history(self, world):
        if not hasattr(self.memory, "connect"):
            return []

        def read():
            entity = json.dumps(["voxel-project:" + world], ensure_ascii=False)
            archive = json.dumps(
                ["voxel-project-id:" + world + ":"], ensure_ascii=False
            )[:-2]
            archive = (
                archive.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
            with self.memory.connect() as db:
                rows = db.execute(
                    "SELECT text,created_at FROM memories WHERE user_id=? AND kind='task_summary' AND (entities_json=? OR entities_json LIKE ? ESCAPE '\\') ORDER BY created_at DESC LIMIT 10000",
                    (self.user, entity, archive),
                ).fetchall()
            groups = {}
            for row in rows:
                try:
                    v = json.loads(row["text"])
                    groups.setdefault(v["digest"], []).append((v, row["created_at"]))
                except (KeyError, ValueError, TypeError):
                    continue
            projects = {}
            for chunks in groups.values():
                by_index = {v["index"]: v for v, _ in chunks}
                ordered = [by_index[k] for k in sorted(by_index)]
                if [v["index"] for v in ordered] != list(range(len(ordered))):
                    continue
                raw = "".join((v["data"] for v in ordered))
                if hashlib.sha256(raw.encode()).hexdigest() != ordered[0]["digest"]:
                    continue
                try:
                    value = json.loads(raw)
                    if value.get("world_id") != world:
                        continue
                    value["authorization"] = None
                    value["execution_intent"] = False
                    if value["status"] not in {"COMPLETED", "CANCELLED", "DISCUSSING"}:
                        value["status"] = "PAUSED"
                    p = Project(**value)
                    stamp = max((t for _, t in chunks))
                    if (
                        p.project_id not in projects
                        or stamp > projects[p.project_id][0]
                    ):
                        projects[p.project_id] = (stamp, p)
                except (ValueError, TypeError, KeyError):
                    continue
            return [
                p
                for _, p in sorted(
                    projects.values(), key=lambda row: row[0], reverse=True
                )[:8]
            ]

        return await asyncio.to_thread(read)

    async def load(self, world):
        records = await self.memory.read_task_bundle(
            self.user, "voxel-project:" + world
        )
        if not records:
            return None
        try:
            parts = sorted(
                (json.loads(row["text"]) for row in records), key=lambda d: d["index"]
            )
            if [p["index"] for p in parts] != list(range(len(parts))):
                return None
            raw = "".join((p["data"] for p in parts))
            digest = hashlib.sha256(raw.encode()).hexdigest()
            if any(
                (p["digest"] != digest or p["kind"] != "voxel_project" for p in parts)
            ):
                return None
            value = json.loads(raw)
            if value["world_id"] != world:
                return None
            value["authorization"] = None
            value["execution_intent"] = False
            if value["status"] in {
                "RUNNING",
                "PLANNING",
                "ARMING",
                "READY",
                "PREPARING_SITE",
            }:
                value["status"] = "PAUSED"
            return Project(**value)
        except (ValueError, TypeError, KeyError):
            return None
