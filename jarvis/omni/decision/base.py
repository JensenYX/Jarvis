from __future__ import annotations
from abc import ABC, abstractmethod
import hashlib
import json
import math
from typing import Any, Literal
from pydantic import Field, model_validator
from jarvis.omni.protocol import Record


class UnsupportedCapability(ValueError):
    pass


class DecisionProtocolError(ValueError):
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


class Option(Record):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class Question(Record):
    id: str = Field(min_length=1)
    primitive: Literal["choice", "noul", "score"]
    instructions: str = Field(min_length=1)
    options: tuple[Option, ...] = ()

    @model_validator(mode="after")
    def validate_options(self):
        if self.primitive == "noul" and self.options:
            raise ValueError(
                "Noul uses fixed false/true semantics, not arbitrary options"
            )
        if self.primitive != "noul" and (not 1 <= len(self.options) <= 256):
            raise ValueError("Typed question requires 1–256 rubric/options")
        if len({o.id for o in self.options}) != len(self.options):
            raise ValueError("Option IDs must be unique even when descriptions repeat")
        return self

    def option_ids(self):
        return (
            ("false", "true")
            if self.primitive == "noul"
            else tuple((o.id for o in self.options))
        )

    def wire(self):
        result = {"type": self.primitive, "instructions": self.instructions}
        if self.primitive == "choice":
            result["criteria"] = {o.id: o.text for o in self.options}
        elif self.primitive == "score":
            result["criteria"] = [o.text for o in self.options]
        return result


class DecisionInput(Record):
    state: Any
    questions: tuple[Question, ...] = Field(min_length=1, max_length=256)
    images: tuple[str, ...] = ()
    audio: str | None = None
    observation_ids: tuple[str, ...] = ()
    input_track: Literal["screen_only", "public_telemetry"] = "screen_only"

    @model_validator(mode="after")
    def unique_questions(self):
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("Duplicate question ID")
        if any(
            (
                not image.startswith(
                    ("data:image/png;base64,", "data:image/jpeg;base64,")
                )
                for image in self.images
            )
        ):
            raise ValueError("Only encoded causal still images are accepted")
        if self.audio is not None and (
            not self.audio.startswith("data:audio/wav;base64,")
        ):
            raise ValueError("Audio must be an encoded causal WAV window")
        if self.audio and self.images:
            raise UnsupportedCapability(
                "Joint image/audio decision input is not validated"
            )
        return self

    def input_hash(self):
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()


class Answer(Record):
    question_id: str
    primitive: Literal["choice", "noul", "score"]
    implementation: Literal["native", "emulated"]
    option_ids: tuple[str, ...]
    raw_probs: tuple[float, ...]
    raw_logits: tuple[float, ...] | None = None
    choice: str | None = None
    expected_score: float | None = None
    noul: float | None = None
    native_confidence: float | None = None
    confidence_kind: str
    calibration_status: Literal["uncalibrated"] = "uncalibrated"
    raw: dict

    @model_validator(mode="after")
    def probabilities_match(self):
        if len(self.option_ids) != len(self.raw_probs) or not self.raw_probs:
            raise ValueError("Probability cardinality differs from option count")
        if (
            any((not math.isfinite(p) or p < 0 or p > 1 for p in self.raw_probs))
            or abs(sum(self.raw_probs) - 1) > 0.010000001
        ):
            raise ValueError("Invalid probability distribution")
        if self.raw_logits is not None and len(self.raw_logits) != len(self.raw_probs):
            raise ValueError("Logit cardinality differs from options")
        return self


class DecisionResult(Record):
    backend: str
    model: str
    revision: str
    precision: str
    input_hash: str
    answers: tuple[Answer, ...]
    elapsed_ms: float
    raw: dict


def normalize(question, raw, *, confidence_kind, implementation="native"):
    ids = question.option_ids()
    if question.primitive == "noul":
        probs = (1 - float(raw["noul"]), float(raw["noul"]))
        return Answer(
            question_id=question.id,
            primitive="noul",
            implementation=implementation,
            option_ids=ids,
            raw_probs=probs,
            noul=probs[1],
            confidence_kind="none_yes_probability_only",
            raw=raw,
        )
    distribution = raw["probabilities"]
    if question.primitive == "score":
        keys = set(map(str, distribution))
        if keys == {str(i) for i in range(len(ids))}:
            start = 0
        elif keys == {str(i + 1) for i in range(len(ids))}:
            start = 1
        else:
            raise ValueError("Unknown score index convention")
        probs = tuple(
            (
                float(distribution.get(str(i + start), distribution.get(i + start)))
                for i in range(len(ids))
            )
        )
        selected = None
        expected = sum((i * p for i, p in enumerate(probs)))
    else:
        if set(distribution) != set(ids):
            raise ValueError("Returned choice labels differ from requested options")
        probs = tuple((float(distribution[key]) for key in ids))
        selected, expected = (raw["choice"], None)
        if selected not in ids:
            raise ValueError("Unknown selected option")
    logits = raw.get("logits")
    if isinstance(logits, dict):
        logits = tuple(
            (logits.get(str(i), logits.get(key)) for i, key in enumerate(ids))
        )
    return Answer(
        question_id=question.id,
        primitive=question.primitive,
        implementation=implementation,
        option_ids=ids,
        raw_probs=probs,
        raw_logits=logits,
        choice=selected,
        expected_score=expected,
        native_confidence=raw.get("confidence"),
        confidence_kind=confidence_kind,
        raw=raw,
    )


class DecisionBackend(ABC):
    capabilities: dict

    @abstractmethod
    async def decide(
        self, request: DecisionInput, *, timeout_s: float = 10
    ) -> DecisionResult:
        pass

    async def close(self):
        pass
