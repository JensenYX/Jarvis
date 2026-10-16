from __future__ import annotations
import time
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field


def new_id() -> str:
    return uuid4().hex


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Event(StrictModel):
    event_id: str = Field(default_factory=new_id)
    session_id: str
    seq: int
    monotonic_ns: int = Field(default_factory=time.monotonic_ns)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class TranscriptUpdate(StrictModel):
    utterance_id: str
    revision: int = Field(ge=1)
    text: str
    is_final: bool
    stable_prefix: str = ""
    audio_end_sample: int = Field(ge=0)
    language: str = ""


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NEEDS_INPUT = "NEEDS_INPUT"
    COMPLETED = "COMPLETED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class Task(StrictModel):
    task_id: str = Field(default_factory=new_id)
    version: int = 1
    logical_request_id: str
    objective: str
    status: TaskStatus = TaskStatus.QUEUED
    source_utterance_id: str
    created_at: float = Field(default_factory=time.time)
    provisional: bool = False
    allowed_tool_scope: list[str] = Field(default_factory=lambda: ["read_only"])
    history: list[dict] = Field(default_factory=list)
    memory: list[dict] = Field(default_factory=list)
    skill: str | None = None
    observations: list[dict] = Field(default_factory=list)
    response_language: str = "auto"
    request_group_id: str | None = None
    request_index: int = 1
    request_count: int = 1
    origin_session_id: str | None = None
    last_mentioned_at: float = 0
    progress: dict = Field(default_factory=dict)
    delivery_state: Literal["pending", "playing", "delivered", "awaiting_user"] = (
        "pending"
    )
    delivery_hold_reason: str | None = None
    delivery_held_at: float = 0
    expected_output: Literal["auto", "file", "lookup", "answer"] = "auto"
    execution_mode: Literal["worker", "fast"] = "worker"
    completed_at: float = 0


class TaskResult(StrictModel):
    task_id: str
    version: int
    status: Literal["completed", "needs_input", "failed", "cancelled"]
    spoken_answer: str
    evidence: list[dict] = Field(default_factory=list)
    needs_user_input: bool = False
    details: str = ""
    artifacts: list[dict] = Field(default_factory=list)


class Decision(StrictModel):
    utterance_id: str
    revision: int
    decision_epoch: int
    route: Literal[
        "FAST",
        "MEMORY",
        "WORKER",
        "CLARIFY",
        "CONTROL",
        "IGNORE",
        "WAIT",
        "BATCH",
        "PREFETCH_MEMORY",
        "PREFETCH_WORKER",
    ]
    confidence: float = Field(ge=0, le=1)
    control: str = "NONE"
    target_task_id: str | None = None
    target_version: int | None = None
    task_relation: Literal["NEW", "REFINE", "REPLACE", "NONE"] = "NEW"
    skill_choice: str = "NONE"
    missing_slot: str = "NONE"
    need_memory: bool = False
    prefetch_resolution: str = "DISCARD"
    decision_origin: Literal[
        "jev", "deepseek_fallback", "terminal_fallback", "local"
    ] = "jev"
    raw: dict = Field(default_factory=dict)


class DecisionSnapshot(StrictModel):
    utterance_id: str
    revision: int
    decision_epoch: int
    task_summary: list[dict] = Field(default_factory=list)
    memory_candidates: list[dict] = Field(default_factory=list)
    skill_candidates: list[dict] = Field(default_factory=list)
    policy_version: str


class AudioChunk(StrictModel):
    generation_epoch: int
    chunk_id: str = Field(default_factory=new_id)
    sample_rate: int = 24000
    pcm: str
    response_id: str


class PlaybackAck(StrictModel):
    generation_epoch: int
    chunk_id: str
    played_samples: int = Field(ge=0)
    interrupted: bool = False
    playback_end_sample: int | None = Field(default=None, ge=0)
    playback_context_end_s: float | None = Field(default=None, ge=0)
    playback_end_client_ms: float | None = Field(default=None, ge=0)
    playback_clock_method: str | None = None
