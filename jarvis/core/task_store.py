import asyncio
import json
import sqlite3
import time
from jarvis.core.schemas import Task, TaskResult


class TaskStore:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS task_records (user_id TEXT NOT NULL, task_id TEXT NOT NULL, version INTEGER NOT NULL, updated_at REAL NOT NULL, task_json TEXT NOT NULL, result_json TEXT, PRIMARY KEY(user_id,task_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS history_resets (user_id TEXT PRIMARY KEY, cleared_at REAL NOT NULL, profile_cleared_at REAL NOT NULL DEFAULT 0)"
            )

    def connect(self):
        return sqlite3.connect(self.path, timeout=20)

    async def save(self, user_id, task, result=None):
        stamp = time.time()
        task_json = task.model_dump_json()
        result_json = result.model_dump_json() if result else None

        def write():
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                reset = db.execute(
                    "SELECT cleared_at FROM history_resets WHERE user_id=?", (user_id,)
                ).fetchone()
                if reset and task.created_at <= reset[0]:
                    return
                db.execute(
                    "INSERT INTO task_records VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,task_id) DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at,task_json=excluded.task_json,result_json=CASE WHEN excluded.version=task_records.version THEN COALESCE(excluded.result_json,task_records.result_json) ELSE excluded.result_json END WHERE excluded.version>task_records.version OR (excluded.version=task_records.version AND excluded.updated_at>=task_records.updated_at)",
                    (
                        user_id,
                        task.task_id,
                        task.version,
                        stamp,
                        task_json,
                        result_json,
                    ),
                )

        await asyncio.to_thread(write)

    async def load(self, user_id, limit=24):

        def read():
            with self.connect() as db:
                return db.execute(
                    "SELECT task_json,result_json FROM task_records WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()

        rows = await asyncio.to_thread(read)
        return [
            (
                Task.model_validate_json(t),
                TaskResult.model_validate_json(r) if r else None,
            )
            for t, r in reversed(rows)
        ]

    async def search(self, user_id, query, limit=4):
        from jarvis.core.tools import terms

        stop = set("的我你吗得之还个这那是了要把给帮能可不么一".strip()) | set(
            "i we our it its this that they could would can please you me my the a an to for of and with about what how now today want help tell give think remember previous previously earlier 之前 现在 一下 一个 这个 那个 任务 帮我 给我 你能 能不能 可以 什么 怎么 告诉".split()
        )
        wanted = terms(query) - stop
        if not wanted:
            return []

        def read():
            with self.connect() as db:
                metadata = db.execute(
                    "SELECT task_id,json_extract(task_json,'$.objective'),json_extract(result_json,'$.artifacts') FROM task_records WHERE user_id=? ORDER BY updated_at DESC LIMIT 1000",
                    (user_id,),
                ).fetchall()
                ranked = sorted(
                    (
                        (
                            len(
                                wanted & terms((objective or "") + " " + (files or ""))
                            ),
                            -i,
                            identifier,
                        )
                        for i, (identifier, objective, files) in enumerate(metadata)
                    ),
                    reverse=True,
                )
                rows = []
                for score, _, identifier in ranked[:limit]:
                    if score:
                        rows.append(
                            db.execute(
                                "SELECT task_json,result_json FROM task_records WHERE user_id=? AND task_id=?",
                                (user_id, identifier),
                            ).fetchone()
                        )
                return rows

        return [
            (
                Task.model_validate_json(t),
                TaskResult.model_validate_json(r) if r else None,
            )
            for t, r in await asyncio.to_thread(read)
        ]

    async def get(self, user_id, task_id):

        def read():
            with self.connect() as db:
                return db.execute(
                    "SELECT task_json,result_json FROM task_records WHERE user_id=? AND task_id=?",
                    (user_id, task_id),
                ).fetchone()

        row = await asyncio.to_thread(read)
        return (
            (
                Task.model_validate_json(row[0]),
                TaskResult.model_validate_json(row[1]) if row[1] else None,
            )
            if row
            else None
        )

    async def catalog(self, user_id, limit=1000):

        def read():
            with self.connect() as db:
                rows = db.execute(
                    "SELECT task_id,version,json_extract(task_json,'$.objective'),json_extract(task_json,'$.status'),json_extract(task_json,'$.created_at'),json_extract(task_json,'$.request_group_id'),json_extract(task_json,'$.request_index'),json_extract(task_json,'$.request_count'),json_extract(task_json,'$.last_mentioned_at'),json_extract(result_json,'$.artifacts'),json_extract(task_json,'$.origin_session_id') FROM task_records WHERE user_id=? ORDER BY json_extract(task_json,'$.created_at') DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            return [
                {
                    "task_id": r[0],
                    "version": r[1],
                    "objective": r[2][:100],
                    "status": r[3],
                    "created_at": r[4],
                    "request_group_id": r[5],
                    "request_index": r[6] or 1,
                    "request_count": r[7] or 1,
                    "last_mentioned_at": r[8] or r[4],
                    "origin_session_id": r[10],
                    "files": [
                        a.get("title", a.get("filename", ""))[:60]
                        for a in json.loads(r[9] or "[]")
                    ],
                    "file_names": [
                        a.get("filename", "") for a in json.loads(r[9] or "[]")
                    ],
                }
                for r in rows
            ]

        return await asyncio.to_thread(read)
