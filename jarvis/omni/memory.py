from __future__ import annotations
import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time


@dataclass(frozen=True)
class Scope:
    user_id: str
    game: str
    game_session_id: str


class ScreenMemory:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                "\n                CREATE TABLE IF NOT EXISTS screen_events (\n                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, game TEXT NOT NULL,\n                    game_session_id TEXT NOT NULL, scene_epoch INTEGER NOT NULL,\n                    text TEXT NOT NULL, fact_status TEXT NOT NULL, event_kind TEXT NOT NULL,\n                    evidence_json TEXT NOT NULL, source TEXT NOT NULL,\n                    player_id TEXT, round_id TEXT, observed_at_ms REAL NOT NULL,\n                    created_at REAL NOT NULL, last_seen_at REAL NOT NULL,\n                    status TEXT NOT NULL DEFAULT 'active', supersedes_id TEXT,\n                    delivered_at_ms REAL, UNIQUE(user_id,game,game_session_id,text,source)\n                );\n                CREATE INDEX IF NOT EXISTS screen_event_scope ON screen_events(user_id,game,game_session_id,status);\n                CREATE TABLE IF NOT EXISTS screen_resets(user_id TEXT PRIMARY KEY, cleared_at REAL NOT NULL);\n                CREATE TABLE IF NOT EXISTS screen_knowledge (\n                    id TEXT PRIMARY KEY, game TEXT NOT NULL, patch TEXT NOT NULL,\n                    text TEXT NOT NULL, source_url TEXT NOT NULL, source_revision TEXT NOT NULL\n                );\n            "
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    async def write(
        self,
        scope: Scope,
        *,
        text,
        fact_status,
        event_kind,
        evidence,
        source,
        scene_epoch,
        observed_at_ms,
        initiated_at,
        player_id=None,
        round_id=None,
        supersedes_id=None,
    ):
        if fact_status not in {"observed", "inferred", "user_reported"}:
            raise ValueError("Uncertain events are not factual memories")
        if (
            source not in {"visual_evidence", "actual_user_microphone"}
            or not evidence
            or (not text.strip())
        ):
            raise ValueError("A memory requires a known source and actual evidence")
        if source == "actual_user_microphone" and fact_status != "user_reported":
            raise ValueError(
                "A user correction is a report, not newly observed gameplay"
            )
        if supersedes_id and source != "actual_user_microphone":
            raise ValueError(
                "Only an explicit user correction may supersede a retained event"
            )
        if event_kind not in {
            "event",
            "suggestion",
            "goal",
            "correction",
            "local_preference",
        }:
            raise ValueError("Unknown event memory kind")
        text = text[:1000]
        identifier = hashlib.sha256(
            json.dumps(
                [scope.user_id, scope.game, scope.game_session_id, text, source],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

        def write():
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                reset = db.execute(
                    "SELECT cleared_at FROM screen_resets WHERE user_id=?",
                    (scope.user_id,),
                ).fetchone()
                if reset and initiated_at <= reset["cleared_at"]:
                    return None
                if supersedes_id:
                    old = db.execute(
                        "SELECT id FROM screen_events WHERE id=? AND user_id=? AND game=? AND game_session_id=? AND status='active'",
                        (
                            supersedes_id,
                            scope.user_id,
                            scope.game,
                            scope.game_session_id,
                        ),
                    ).fetchone()
                    if old is None:
                        raise ValueError(
                            "Correction refers to a missing or out-of-scope event"
                        )
                    db.execute(
                        "UPDATE screen_events SET status='superseded' WHERE id=?",
                        (supersedes_id,),
                    )
                now = time.time()
                db.execute(
                    "INSERT INTO screen_events(id,user_id,game,game_session_id,scene_epoch,text,fact_status,\n                    event_kind,evidence_json,source,player_id,round_id,observed_at_ms,created_at,last_seen_at,supersedes_id)\n                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)\n                    ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (
                        identifier,
                        scope.user_id,
                        scope.game,
                        scope.game_session_id,
                        scene_epoch,
                        text,
                        fact_status,
                        event_kind,
                        json.dumps(evidence, ensure_ascii=False),
                        source,
                        player_id,
                        round_id,
                        observed_at_ms,
                        now,
                        now,
                        supersedes_id,
                    ),
                )
                db.execute(
                    "DELETE FROM screen_events WHERE user_id=? AND game=? AND game_session_id=? AND id NOT IN\n                    (SELECT id FROM screen_events WHERE user_id=? AND game=? AND game_session_id=? ORDER BY last_seen_at DESC,id LIMIT 512)",
                    (scope.user_id, scope.game, scope.game_session_id) * 2,
                )
                return identifier

        return await asyncio.to_thread(write)

    async def candidates(
        self, scope: Scope, *, before_ms, limit=24, player_id=None, round_id=None
    ):

        def read():
            clauses = [
                "user_id=?",
                "game=?",
                "game_session_id=?",
                "status='active'",
                "observed_at_ms<=?",
            ]
            params = [scope.user_id, scope.game, scope.game_session_id, before_ms]
            for key, value in [("player_id", player_id), ("round_id", round_id)]:
                if value is not None:
                    clauses.append(f"{key}=?")
                    params.append(value)
            with self.connect() as db:
                rows = db.execute(
                    "SELECT * FROM screen_events WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY last_seen_at DESC,id LIMIT ?",
                    (*params, min(64, max(1, limit))),
                ).fetchall()
            result = []
            for row in rows:
                record = dict(row)
                record["evidence"] = json.loads(record.pop("evidence_json"))
                record["temporal_scope"] = (
                    "historical; does not establish current game state"
                )
                result.append(record)
            return result

        return await asyncio.to_thread(read)

    async def prior_events(self, scope: Scope, *, available_before, limit=8):

        def read():
            with self.connect() as db:
                rows = db.execute(
                    "SELECT * FROM screen_events WHERE user_id=? AND game=?\n                    AND game_session_id!=? AND status='active' AND event_kind!='local_preference'\n                    AND created_at<=? ORDER BY last_seen_at DESC,id LIMIT ?",
                    (
                        scope.user_id,
                        scope.game,
                        scope.game_session_id,
                        available_before,
                        min(32, max(1, limit)),
                    ),
                ).fetchall()
            result = []
            for row in rows:
                record = dict(row)
                record["evidence"] = json.loads(record.pop("evidence_json"))
                record["temporal_scope"] = (
                    "prior game session; original monotonic time is not current time"
                )
                result.append(record)
            return result

        return await asyncio.to_thread(read)

    async def delivered(self, scope: Scope, identifiers, at_ms):

        def write():
            with self.connect() as db:
                for identifier in identifiers:
                    db.execute(
                        "UPDATE screen_events SET delivered_at_ms=? WHERE id=? AND user_id=? AND game=?\n                        AND game_session_id=? AND status='active' ",
                        (
                            at_ms,
                            identifier,
                            scope.user_id,
                            scope.game,
                            scope.game_session_id,
                        ),
                    )

        await asyncio.to_thread(write)

    async def clear(self, user_id):

        def erase():
            with self.connect() as db:
                db.execute(
                    "INSERT INTO screen_resets VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET cleared_at=excluded.cleared_at",
                    (user_id, time.time()),
                )
                return db.execute(
                    "DELETE FROM screen_events WHERE user_id=?", (user_id,)
                ).rowcount

        return await asyncio.to_thread(erase)

    async def knowledge(self, game, patch):
        if not patch:
            return []

        def read():
            with self.connect() as db:
                return [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM screen_knowledge WHERE game=? AND patch=? LIMIT 24",
                        (game, patch),
                    )
                ]

        return await asyncio.to_thread(read)

    async def add_knowledge(self, *, game, patch, text, source_url, source_revision):
        if not all((game, patch, text, source_revision)) or not source_url.startswith(
            "https://"
        ):
            raise ValueError(
                "Knowledge requires a game patch and a cited, versioned source"
            )
        identifier = hashlib.sha256(
            json.dumps([game, patch, text, source_url, source_revision]).encode()
        ).hexdigest()

        def write():
            with self.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO screen_knowledge VALUES (?,?,?,?,?,?)",
                    (identifier, game, patch, text, source_url, source_revision),
                )

        await asyncio.to_thread(write)
        return identifier
