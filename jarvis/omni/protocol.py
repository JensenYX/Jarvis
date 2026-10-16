from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Source = Literal["screen", "game_audio", "user_mic", "public_telemetry", "event_memory"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MediaStamp(Record):
    game_session_id: str = Field(min_length=1, max_length=128)
    scene_epoch: int = Field(ge=0)
    sequence_id: int = Field(ge=0)
    source_clock: str = Field(min_length=1, max_length=128)
    source_capture_ts_ms: float = Field(ge=0)
    capture_ts_ms: float = Field(ge=0)
    receive_ts_ms: float = Field(ge=0)
    clock_error_ms: float = Field(ge=0)


class MemoryOrigin(Record):
    memory_id: str
    user_id: str
    game: str
    game_session_id: str
    source: Literal["visual_evidence", "actual_user_microphone"]
    fact_status: Literal["observed", "inferred", "user_reported"]
    retained_at_unix_s: float = Field(ge=0)
    original_observation_ids: tuple[str, ...]


class Observation(Record):
    observation_id: str = Field(min_length=1, max_length=128)
    stamp: MediaStamp
    source: Source
    evidence_refs: tuple[str, ...]
    visibility: Literal[
        "player_visible", "player_audible", "declared_telemetry", "retained_record"
    ]
    available_at_ms: float = Field(ge=0)
    content_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    mime_type: str
    byte_length: int = Field(ge=0)
    duration_ms: float = Field(default=0, ge=0)
    channels: int = Field(default=1, ge=1, le=8)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    memory_origin: MemoryOrigin | None = None

    @model_validator(mode="after")
    def check_source(self):
        expected = {
            "screen": "player_visible",
            "game_audio": "player_audible",
            "user_mic": "player_audible",
            "public_telemetry": "declared_telemetry",
            "event_memory": "retained_record",
        }
        if self.visibility != expected[self.source]:
            raise ValueError("Visibility must describe the actual observation source")
        if (self.source == "event_memory") != (self.memory_origin is not None):
            raise ValueError("Only retrieved event memory requires a memory origin")
        if self.available_at_ms < self.stamp.receive_ts_ms:
            raise ValueError("An observation cannot be available before receipt")
        if (
            self.stamp.capture_ts_ms - self.stamp.clock_error_ms
            > self.stamp.receive_ts_ms
        ):
            raise ValueError("Capture time is in the future beyond clock uncertainty")
        return self


class EvidenceClaim(Record):
    claim_id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    subject: str | None = None
    value: Any = None
    evidence_refs: tuple[str, ...] = ()
    status: Literal["observed", "inferred", "contradicted", "unknown"]
    confidence_raw: float | None = None
    temporal_mode: Literal["current", "historical"] = "current"
    valid_until_ms: float | None = None
    invalidation_conditions: tuple[
        Literal["scene_change", "camera_change", "source_lost", "correction"], ...
    ] = ()
    contradicts: tuple[str, ...] = ()


class InteractionPlan(Record):
    decision_id: str
    state_version: str
    event_id: str
    intent: Literal[
        "SILENT", "ANSWER", "ALERT", "PRAISE", "SUGGEST", "ASK", "REVIEW", "COMMENT"
    ]
    claim_ids: tuple[str, ...] = ()
    audience: Literal["user"] = "user"
    utterance_goal: str
    earliest_start_ms: float
    latest_useful_start_ms: float
    max_spoken_duration_ms: int = Field(gt=0)
    interrupt_class: Literal["none", "normal", "urgent"] = "none"
    session_epoch: int = Field(ge=0)
    game_session_id: str | None = None
    scene_epoch: int | None = Field(default=None, ge=0)
    speech_generation: int = Field(ge=0)
    status: Literal[
        "proposed", "queued", "started", "completed", "interrupted", "expired"
    ] = "proposed"

    @model_validator(mode="after")
    def valid_window(self):
        if self.earliest_start_ms > self.latest_useful_start_ms:
            raise ValueError("Empty useful speech window")
        if (self.game_session_id is None) != (self.scene_epoch is None):
            raise ValueError("Game binding must include both session and scene")
        return self
