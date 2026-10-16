from __future__ import annotations
import copy
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import inspect
from pathlib import Path
import random
import threading
from jarvis.adapters.omni.venus_generation_checkpoint import _DUPLEX_FIELDS
from jarvis.adapters.omni.venus_omni_patches import SOURCE_SHA256

_PROCESSOR_SHA = "702488fc928f4f9a48f7a60fd56625efb29a37826e590f40b13be9cb2867ffe4"
_VOCODER_SHA = "0598c25ad9883f75d6d5ca1e9276c03adbfa0357a29442164198ea6931a06efb"
_FIELDS = tuple(
    dict.fromkeys(
        (
            *_DUPLEX_FIELDS,
            "audio_buffer",
            "input_audio_buffer",
            "tts_text_start_pos",
            "tts_past_key_values",
            "tts_current_turn_start_time",
            "token2wav_initialized",
            "token2wav_buffer",
            "flow_cache_base",
            "hift_cache_base",
            "pre_lookahead",
            "_delegate_tts_skip",
            "_delegate_turn_preflushed",
            "_delegate_open_id",
            "_delegate_close_id",
            "_harness_speech_incomplete",
            "_harness_drain_units",
            "_harness_tts_text_end",
        )
    )
)
_VOCODER_FIELDS = ("cache", "stream_cache", "hift_cache_dict")


def _named(target, names):
    return {name: getattr(target, name) for name in names if hasattr(target, name)}


def _install(target, state, names):
    for name in names:
        if name in state:
            setattr(target, name, state[name])
        elif hasattr(target, name):
            delattr(target, name)


@dataclass
class OmniBranchState:
    values: dict

    @classmethod
    def capture(cls, duplex, *, clone=False):
        import numpy as np
        import torch

        values = {
            "decoder": {
                k: v
                for k, v in vars(duplex.decoder).items()
                if k not in {"m", "tokenizer"} and (not callable(v))
            },
            "duplex": _named(duplex, _FIELDS),
            "input_model": _named(duplex.model, ("audio_past_key_values",)),
            "processor": duplex.processor.get_streaming_snapshot(),
            "tts_model": _named(duplex.model.tts, ("_harness_last_decode",)),
            "vocoder": _named(duplex.model.tts.audio_tokenizer, _VOCODER_FIELDS),
            "rng": (
                random.getstate(),
                np.random.get_state(),
                torch.get_rng_state(),
                torch.cuda.get_rng_state(duplex.device),
            ),
        }
        return cls(copy.deepcopy(values) if clone else values)

    def install(self, duplex, *, clone=False):
        import numpy as np
        import torch

        v = copy.deepcopy(self.values) if clone else self.values
        fields = {
            k
            for k, x in vars(duplex.decoder).items()
            if k not in {"m", "tokenizer"} and (not callable(x))
        }
        _install(duplex.decoder, v["decoder"], fields | set(v["decoder"]))
        _install(duplex, v["duplex"], _FIELDS)
        _install(duplex.model, v["input_model"], ("audio_past_key_values",))
        duplex.processor.restore_streaming_snapshot(v["processor"])
        _install(duplex.model.tts, v["tts_model"], ("_harness_last_decode",))
        _install(duplex.model.tts.audio_tokenizer, v["vocoder"], _VOCODER_FIELDS)
        random.setstate(v["rng"][0])
        np.random.set_state(v["rng"][1])
        torch.set_rng_state(v["rng"][2])
        torch.cuda.set_rng_state(v["rng"][3], duplex.device)


@dataclass(eq=False)
class DraftBranch:
    scope: tuple[str, str, int]
    state: OmniBranchState | None
    closed: bool = False


class OmniBranches:
    def __init__(self, duplex):
        if (
            getattr(type(duplex), "_screen_patch_manifest", {}).get("source_sha256")
            != SOURCE_SHA256
        ):
            raise ValueError("Branches require the audited Omni generator")
        for obj, expected in [
            (duplex.processor, _PROCESSOR_SHA),
            (duplex.model.tts.audio_tokenizer, _VOCODER_SHA),
        ]:
            if (
                hashlib.sha256(
                    Path(inspect.getsourcefile(type(obj))).read_bytes()
                ).hexdigest()
                != expected
            ):
                raise ValueError("Unreviewed processor/vocoder state layout")
        self.duplex = duplex
        self.owner = threading.get_ident()
        self.branches = set()
        self.active = None

    def _check(self, branch=None):
        if threading.get_ident() != self.owner:
            raise RuntimeError("Branches belong to the model owner thread")
        if branch is not None and (branch not in self.branches or branch.closed):
            raise ValueError("Unknown or closed draft branch")

    def fork(self, scope):
        self._check()
        if self.active is not None or self.duplex.decoder._pending_unit_id is not None:
            raise RuntimeError("Fork only at a live completed-unit boundary")
        if (
            len(scope) != 3
            or not all((isinstance(x, str) and x for x in scope[:2]))
            or type(scope[2]) is not int
        ):
            raise ValueError("Scope requires session, response and integer epoch")
        if self.branches:
            raise RuntimeError("Only one pending draft is supported per owner")
        branch = DraftBranch(
            tuple(scope), OmniBranchState.capture(self.duplex, clone=True)
        )
        self.branches.add(branch)
        return branch

    @contextmanager
    def activate(self, branch):
        self._check(branch)
        if self.active is not None:
            raise RuntimeError("Nested branch activation is not supported")
        live = OmniBranchState.capture(self.duplex)
        self.active = branch
        try:
            branch.state.install(self.duplex)
            yield self.duplex
            branch.state = OmniBranchState.capture(self.duplex)
        except BaseException:
            branch.closed = True
            branch.state = None
            self.branches.discard(branch)
            raise
        finally:
            live.install(self.duplex)
            self.active = None

    def discard(self, branch):
        self._check(branch)
        if self.active is not None:
            raise RuntimeError("Cannot discard an active branch")
        branch.state = None
        branch.closed = True
        self.branches.remove(branch)

    def commit(self, branch, *, scope):
        self._check(branch)
        if self.active is not None or tuple(scope) != branch.scope:
            raise ValueError("Cannot commit active or differently bound draft")
        branch.state.install(self.duplex)
        branch.state = None
        branch.closed = True
        self.branches.remove(branch)
