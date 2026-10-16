from __future__ import annotations
import copy
import random
import threading
import time
from jarvis.adapters.omni.venus_omni_patches import SOURCE_SHA256

_ABSENT = object()
_DUPLEX_FIELDS = (
    "pending_logits",
    "_streaming_generate_count",
    "total_ids",
    "res_ids",
    "speak_count",
    "current_turn_ended",
    "total_hidden",
    "current_mode",
    "audio_chunk_idx",
    "prefill_schema_tokens",
    "_current_unit_prefill_tokens",
    "_harness_approved_ids",
    "_harness_approved_ended",
)


class GenerationCheckpoint:
    def __init__(self, duplex):
        import numpy as np
        import torch

        manifest = getattr(type(duplex), "_screen_patch_manifest", {})
        if manifest.get("source_sha256") != SOURCE_SHA256:
            raise ValueError("Pre-TTS rollback requires the audited Omni release")
        if duplex.pending_logits is None or duplex.decoder._pending_unit_id is None:
            raise ValueError("Checkpoint requires an open, freshly prefilled unit")
        self.owner = threading.get_ident()
        self.duplex = duplex
        self.decoder = copy.deepcopy(
            {
                k: v
                for k, v in vars(duplex.decoder).items()
                if k not in {"m", "tokenizer"} and (not callable(v))
            }
        )
        self.state = {
            name: copy.deepcopy(getattr(duplex, name))
            if hasattr(duplex, name)
            else _ABSENT
            for name in _DUPLEX_FIELDS
        }
        self.rng = (
            random.getstate(),
            np.random.get_state(),
            torch.get_rng_state(),
            torch.cuda.get_rng_state(duplex.device)
            if str(duplex.device).startswith("cuda")
            else None,
        )

    def restore(self):
        import numpy as np
        import torch

        if threading.get_ident() != self.owner:
            raise RuntimeError("Checkpoint belongs to a different model owner thread")
        target = self.duplex.decoder
        fields = {k for k, v in vars(target).items() if not callable(v)}
        for name in fields - {"m", "tokenizer"} - set(self.decoder):
            delattr(target, name)
        for name, value in copy.deepcopy(self.decoder).items():
            setattr(target, name, value)
        for name, value in self.state.items():
            if value is _ABSENT:
                if hasattr(self.duplex, name):
                    delattr(self.duplex, name)
            else:
                setattr(self.duplex, name, copy.deepcopy(value))
        random.setstate(self.rng[0])
        np.random.set_state(self.rng[1])
        torch.set_rng_state(self.rng[2])
        if self.rng[3] is not None:
            torch.cuda.set_rng_state(self.rng[3], self.duplex.device)


class _RejectedBeforeTTS(Exception):
    pass


def reviewed_generate(duplex, review, **generation):
    started = time.monotonic()
    checkpoint = GenerationCheckpoint(duplex)
    checkpoint_ms = (time.monotonic() - started) * 1000
    previous = getattr(duplex, "_harness_text_candidate_callback", None)
    decision = {"status": "NO_SPOKEN_CANDIDATE", "checkpoint_ms": checkpoint_ms}
    accepted = False

    def candidate(tokens, ended):
        nonlocal accepted
        decision["tokens"] = list(tokens)
        decision["end_of_turn"] = bool(ended)
        review_started = time.monotonic()
        try:
            answer = review(list(tokens), bool(ended))
            if type(answer) is not bool:
                raise TypeError("Review must return an explicit boolean")
        except Exception as error:
            decision.update(status="REVIEW_FAILED", error_type=type(error).__name__)
            raise _RejectedBeforeTTS() from error
        finally:
            decision["review_ms"] = (time.monotonic() - review_started) * 1000
        decision["status"] = (
            "APPROVED_FOR_SYNTHESIS" if answer else "REJECTED_BEFORE_TTS"
        )
        if not answer:
            raise _RejectedBeforeTTS()
        if previous is not None:
            previous(tokens, ended)
        accepted = True

    duplex._harness_text_candidate_callback = candidate
    try:
        result = duplex.streaming_generate(**generation)
    except _RejectedBeforeTTS:
        restore_started = time.monotonic()
        checkpoint.restore()
        decision["restore_ms"] = (time.monotonic() - restore_started) * 1000
        duplex._harness_text_candidate_callback = None
        result = duplex.streaming_generate(
            **{
                **generation,
                "force_listen": True,
                "start_response": False,
                "audio_callback": None,
            }
        )
    except BaseException:
        if not accepted:
            checkpoint.restore()
        raise
    finally:
        duplex._harness_text_candidate_callback = previous
    result["pre_tts_transaction"] = decision
    return result
