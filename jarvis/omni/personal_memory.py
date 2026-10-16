from __future__ import annotations
import asyncio
from collections import defaultdict
import hashlib
import logging
import time
from uuid import uuid4
import httpx
from pydantic import BaseModel, ConfigDict, Field


class QueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    concurrency: int = Field(default=2, ge=1, le=8)
    max_attempts: int = Field(default=4, ge=1, le=8)
    attempt_timeout_s: float = Field(default=30, gt=0, le=30)
    retry_base_s: float = Field(default=5, ge=0, le=60)
    retry_max_s: float = Field(default=60, ge=0, le=300)
    pending_ttl_s: float = Field(default=3600, gt=0, le=86400)
    max_pending_per_user: int = Field(default=64, ge=1, le=512)
    max_pending_total: int = Field(default=4096, ge=1, le=16384)
    receipt_limit: int = Field(default=4096, ge=1, le=16384)


class MemoryExtractionQueue:
    def __init__(self, memory, worker, settings=None):
        self.memory, self.worker = (memory, worker)
        self.settings = QueueSettings.model_validate(settings or {})
        self.wake = asyncio.Event()
        self.tasks = []
        self.listeners = defaultdict(set)
        self.notification_fences = {}
        with memory.connect() as db:
            db.executescript(
                "\n                CREATE TABLE IF NOT EXISTS memory_extraction_jobs (\n                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source_event_id TEXT NOT NULL,\n                    observed_at REAL NOT NULL, utterance TEXT NOT NULL, utterance_sha256 TEXT NOT NULL,\n                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,\n                    next_attempt_at REAL NOT NULL, lease_token TEXT, lease_until REAL,\n                    created_at REAL NOT NULL, updated_at REAL NOT NULL, last_error TEXT,\n                    UNIQUE(user_id,source_event_id)\n                );\n                CREATE INDEX IF NOT EXISTS extraction_queue_order\n                    ON memory_extraction_jobs(user_id,status,observed_at,created_at,id);\n            "
            )

    def subscribe(self, user_id, emit):
        self.listeners[user_id].add(emit)

        def unsubscribe():
            self.listeners[user_id].discard(emit)
            if not self.listeners[user_id]:
                self.listeners.pop(user_id, None)

        return unsubscribe

    def emit(self, user_id, event_type, **payload):
        if payload.get("observed_at", float("inf")) <= self.notification_fences.get(
            user_id, 0
        ):
            return
        for listener in tuple(self.listeners.get(user_id, ())):
            try:
                listener(event_type, **payload)
            except Exception:
                logging.getLogger(__name__).exception("Memory queue listener failed")

    def history_cleared(self, user_id, at):
        self.notification_fences[user_id] = at
        self.notification_fences = {
            u: t for u, t in self.notification_fences.items() if u in self.listeners
        }

    async def submit(self, user_id, text, source_event_id, *, observed_at):
        if not text.strip() or len(text) > 16000:
            raise ValueError("Invalid extraction utterance")
        digest = hashlib.sha256(text.encode()).hexdigest()

        def write():
            with self.memory.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                reset = db.execute(
                    "SELECT cleared_at FROM history_resets WHERE user_id=?", (user_id,)
                ).fetchone()
                if reset and observed_at <= reset["cleared_at"]:
                    return None
                old = db.execute(
                    "SELECT id,utterance_sha256 FROM memory_extraction_jobs WHERE user_id=? AND source_event_id=?",
                    (user_id, source_event_id),
                ).fetchone()
                if old:
                    if old["utterance_sha256"] != digest:
                        raise ValueError(
                            "Extraction source was reused with different text"
                        )
                    return old["id"]
                count = db.execute(
                    "SELECT COUNT(*),SUM(user_id=?) FROM memory_extraction_jobs WHERE status IN ('pending','leased')",
                    (user_id,),
                ).fetchone()
                if (
                    count[0] >= self.settings.max_pending_total
                    or (count[1] or 0) >= self.settings.max_pending_per_user
                ):
                    raise OverflowError("Memory extraction queue is full")
                identifier, now = (uuid4().hex, time.time())
                db.execute(
                    "INSERT INTO memory_extraction_jobs\n                    (id,user_id,source_event_id,observed_at,utterance,utterance_sha256,status,next_attempt_at,created_at,updated_at)\n                    VALUES (?,?,?,?,?,?,'pending',?,?,?)",
                    (
                        identifier,
                        user_id,
                        source_event_id,
                        observed_at,
                        text,
                        digest,
                        now,
                        now,
                        now,
                    ),
                )
                return identifier

        identifier = await asyncio.to_thread(write)
        if identifier:
            self.emit(
                user_id,
                "memory.extraction_queued",
                job_id=identifier,
                source_event_id=source_event_id,
                observed_at=observed_at,
                saved=False,
            )
            self.wake.set()
        return identifier

    async def claim(self):

        def claim():
            now = time.time()
            with self.memory.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE memory_extraction_jobs SET status='failed',utterance='',\n                    lease_token=NULL,lease_until=NULL,updated_at=?,last_error='RetryBudgetExpired'\n                    WHERE status IN ('pending','leased') AND\n                    (created_at<? OR (attempts>=? AND (status='pending' OR lease_until<=?)))",
                    (
                        now,
                        now - self.settings.pending_ttl_s,
                        self.settings.max_attempts,
                        now,
                    ),
                )
                db.execute(
                    "DELETE FROM memory_extraction_jobs WHERE status IN ('complete','failed') AND id NOT IN\n                    (SELECT id FROM memory_extraction_jobs WHERE status IN ('complete','failed')\n                    ORDER BY updated_at DESC,id LIMIT ?)",
                    (self.settings.receipt_limit,),
                )
                row = db.execute(
                    "SELECT q.* FROM memory_extraction_jobs q\n                    WHERE ((q.status='pending' AND q.next_attempt_at<=?) OR\n                           (q.status='leased' AND q.lease_until<=?))\n                    AND NOT EXISTS (SELECT 1 FROM memory_extraction_jobs earlier\n                        WHERE earlier.user_id=q.user_id AND earlier.status IN ('pending','leased')\n                        AND (earlier.observed_at,earlier.created_at,earlier.id)<(q.observed_at,q.created_at,q.id))\n                    ORDER BY q.next_attempt_at,q.observed_at,q.id LIMIT 1",
                    (now, now),
                ).fetchone()
                if row is None:
                    return None
                job = dict(row)
                job.update(lease_token=uuid4().hex, attempts=row["attempts"] + 1)
                db.execute(
                    "UPDATE memory_extraction_jobs SET status='leased',attempts=?,lease_token=?,lease_until=?,updated_at=?\n                    WHERE id=?",
                    (
                        job["attempts"],
                        job["lease_token"],
                        now + self.settings.attempt_timeout_s + 60,
                        now,
                        job["id"],
                    ),
                )
                return job

        return await asyncio.to_thread(claim)

    async def commit(self, job, records):

        def write():
            with self.memory.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                active = db.execute(
                    "SELECT 1 FROM memory_extraction_jobs WHERE id=? AND status='leased' AND lease_token=? AND lease_until>?",
                    (job["id"], job["lease_token"], time.time()),
                ).fetchone()
                if active is None:
                    return None
                saved = []
                for record in records:
                    result = self.memory.write_record(
                        db,
                        job["user_id"],
                        record,
                        job["source_event_id"],
                        job["observed_at"],
                    )
                    if result:
                        saved.append(result)
                db.execute(
                    "UPDATE memory_extraction_jobs SET status='complete',utterance='',lease_token=NULL,\n                    lease_until=NULL,updated_at=?,last_error=NULL WHERE id=?",
                    (time.time(), job["id"]),
                )
                return saved

        saved = await asyncio.to_thread(write)
        if saved is not None:
            for record in saved:
                self.emit(
                    job["user_id"],
                    "memory.written",
                    observed_at=job["observed_at"],
                    **record,
                )
            self.emit(
                job["user_id"],
                "memory.extraction_committed",
                job_id=job["id"],
                source_event_id=job["source_event_id"],
                written_records=len(saved),
                attempts=job["attempts"],
                observed_at=job["observed_at"],
            )
        return saved

    async def fail(self, job, error, *, retryable=True, cancelled=False):
        retry = cancelled or (
            retryable and job["attempts"] < self.settings.max_attempts
        )
        delay = (
            0
            if cancelled
            else min(
                self.settings.retry_max_s,
                self.settings.retry_base_s * 2 ** (job["attempts"] - 1),
            )
        )

        def write():
            with self.memory.connect() as db:
                return db.execute(
                    "UPDATE memory_extraction_jobs SET status=?,next_attempt_at=?,lease_token=NULL,\n                    lease_until=NULL,updated_at=?,last_error=?,utterance=CASE WHEN ? THEN utterance ELSE '' END\n                    WHERE id=? AND status='leased' AND lease_token=?",
                    (
                        "pending" if retry else "failed",
                        time.time() + delay,
                        time.time(),
                        type(error).__name__,
                        retry,
                        job["id"],
                        job["lease_token"],
                    ),
                ).rowcount

        if await asyncio.to_thread(write):
            self.emit(
                job["user_id"],
                "memory.extraction_deferred" if retry else "memory.write_failed",
                job_id=job["id"],
                source_event_id=job["source_event_id"],
                attempts=job["attempts"],
                error=type(error).__name__,
                retry_in_s=delay if retry else None,
                observed_at=job["observed_at"],
            )

    async def process_one(self):
        job = await self.claim()
        if job is None:
            return False

        def emit(kind, **payload):
            self.emit(job["user_id"], kind, observed_at=job["observed_at"], **payload)

        emit(
            "memory.extraction_started",
            job_id=job["id"],
            source_event_id=job["source_event_id"],
            attempts=job["attempts"],
        )
        try:
            async with asyncio.timeout(self.settings.attempt_timeout_s):
                records = await self.memory.propose_extraction(
                    job["user_id"],
                    job["utterance"],
                    job["source_event_id"],
                    self.worker,
                    emit,
                )
            await self.commit(job, records)
        except asyncio.CancelledError as error:
            await self.fail(job, error, cancelled=True)
            raise
        except Exception as error:
            retryable = not isinstance(
                error, httpx.HTTPStatusError
            ) or error.response.status_code in {408, 429, 500, 502, 503, 504}
            await self.fail(job, error, retryable=retryable)
        return True

    def start(self):
        if self.tasks:
            raise RuntimeError("Memory queue already started")

        async def consume():
            while True:
                self.wake.clear()
                try:
                    if await self.process_one():
                        continue
                except Exception as error:
                    logging.getLogger(__name__).error(
                        "Memory queue iteration failed: %s", type(error).__name__
                    )
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=0.5)
                except TimeoutError:
                    pass

        self.tasks = [
            asyncio.create_task(consume()) for _ in range(self.settings.concurrency)
        ]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
