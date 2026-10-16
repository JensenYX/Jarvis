from __future__ import annotations
import argparse
import asyncio
import base64
import contextlib
import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from uuid import uuid4
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from jarvis.adapters.omni.venus_patches import SpeechAborted, end_response, install


class DelegateParser:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.open_id = tokenizer.convert_tokens_to_ids("<delegate>")
        self.close_id = tokenizer.convert_tokens_to_ids("</delegate>")
        self.turn_end = tokenizer.convert_tokens_to_ids("<|turn_eos|>")
        self.inside = False
        self.payload = []

    def feed(self, ids):
        visible, delegates = ([], [])
        for token in ids:
            if token == self.open_id:
                self.inside, self.payload = (True, [])
            elif token == self.close_id:
                if self.inside:
                    delegates.append(
                        self.tokenizer.decode(self.payload, skip_special_tokens=True)
                    )
                self.inside, self.payload = (False, [])
            elif token == self.turn_end:
                self.inside, self.payload = (False, [])
                visible.append(token)
            elif self.inside:
                self.payload.append(token)
            else:
                visible.append(token)
        return (self.tokenizer.decode(visible, skip_special_tokens=True), delegates)


def batch_system_prefill(decoder, prepare, concatenate):
    feed, register = (decoder.feed, decoder.register_system_prompt)
    blocks = []

    def collect(embeds, return_logits=False):
        if return_logits:
            flush()
            return feed(embeds, return_logits=True)
        blocks.append(embeds)

    def flush():
        if blocks:
            joined = concatenate(blocks)
            blocks.clear()
            for start in range(0, len(joined), 512):
                feed(joined[start : start + 512])

    def protect():
        flush()
        register()

    decoder.feed, decoder.register_system_prompt = (collect, protect)
    try:
        answer = prepare()
        flush()
        return answer
    finally:
        decoder.feed, decoder.register_system_prompt = (feed, register)


class VenusAudio:
    def __init__(self, model_dir: str):
        import torch
        from transformers import AutoModel, set_seed

        self.owner = threading.get_ident()
        self.model_dir = Path(model_dir).resolve()
        set_seed(42)
        torch.set_num_threads(4)
        self.model = (
            AutoModel.from_pretrained(
                str(self.model_dir),
                trust_remote_code=True,
                local_files_only=True,
                attn_implementation="sdpa",
                torch_dtype=torch.bfloat16,
                init_vision=False,
                init_audio=True,
                init_tts=True,
            )
            .eval()
            .cuda()
        )
        self.model.requires_grad_(False)
        self.duplex = self.model.as_duplex(
            generate_audio=True, sliding_window_mode="basic"
        )
        self.patch_manifest = install(self.duplex)

    def assert_owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError("Venus KV may only be accessed by its owner thread")

    def prepare(self, system_prompt: str, seed: int = 42, batch_prefill=False):
        import librosa
        import torch
        from transformers import set_seed

        self.assert_owner()
        set_seed(seed)
        reference = self.model_dir / "assets/HT_ref_audio.wav"
        ref_audio, _ = librosa.load(reference, sr=16000, mono=True)
        with torch.inference_mode():
            self.model.reset_session()
            prepare = lambda: self.duplex.prepare(
                prefix_system_prompt=system_prompt,
                ref_audio=ref_audio,
                prompt_wav_path=str(reference),
            )
            if batch_prefill:
                batch_system_prefill(
                    self.duplex.decoder,
                    prepare,
                    lambda blocks: torch.cat(blocks, dim=0),
                )
            else:
                prepare()
        self.duplex._delegate_tts_skip = False
        self.duplex._delegate_turn_preflushed = False
        self.duplex._harness_speech_incomplete = False
        self.duplex._harness_approved_ids = None
        self.duplex._harness_approved_ended = False
        self.parser = DelegateParser(self.duplex.tokenizer)
        self.cursor = 0
        self.input_samples = 0
        self.response_id = uuid4().hex

    def end_response(self):
        import torch

        self.assert_owner()
        with torch.inference_mode():
            end_response(self.duplex)
        self.cursor = len(self.duplex.total_ids)
        self.parser = DelegateParser(self.duplex.tokenizer)

    def step(
        self,
        pcm: np.ndarray,
        backend: list[str] | None = None,
        hold=False,
        start_response=False,
        audio_callback=None,
    ):
        import torch

        self.assert_owner()
        text_only = pcm is None
        approved_input = getattr(self.duplex, "_harness_approved_ids", None) is not None
        if text_only and (
            hold
            or not start_response
            or (not backend)
            or (
                getattr(self.duplex, "_harness_approved_ids", None) is None
                and (not getattr(self.duplex, "_harness_immediate_native", False))
            )
        ):
            raise ValueError("Text-only speech requires an explicit approved response")
        if not text_only and len(pcm) != 16000:
            raise ValueError(
                "Venus owner steps require exactly one second of 16 kHz audio"
            )
        start = time.monotonic_ns()
        arm = getattr(self.duplex, "_harness_recent_answer_block", None)
        if arm and backend:
            arm(backend)
        with torch.inference_mode():
            prefill = self.prefill(pcm, backend)
            if not prefill.get("success"):
                raise RuntimeError(f"Venus prefill: {prefill.get('reason')}")
            try:
                result = self.duplex.streaming_generate(
                    max_new_speak_tokens_per_chunk=48
                    if getattr(self.duplex, "_harness_batch_approved", False)
                    and getattr(self.duplex, "_harness_approved_ids", None) is not None
                    else getattr(self.duplex, "_harness_native_speech_tokens", 20),
                    decode_mode=getattr(
                        self.duplex, "_harness_text_decode_mode", "sampling"
                    ),
                    temperature=float(os.environ.get("VENUS_TEXT_TEMPERATURE", "0.7")),
                    top_k=20,
                    top_p=0.8,
                    force_listen=hold,
                    start_response=start_response,
                    audio_callback=None if hold else audio_callback,
                )
            except SpeechAborted:
                result = {
                    "is_listen": False,
                    "end_of_turn": False,
                    "audio_waveform": None,
                    "audio_complete": False,
                    "speech_aborted": True,
                    "n_tts_tokens": 0,
                }
        raw_ids = self.duplex.total_ids[self.cursor :]
        self.cursor = len(self.duplex.total_ids)
        text, delegates = self.parser.feed(raw_ids)
        wave = result.pop("audio_waveform", None)
        self.input_samples += 0 if text_only else len(pcm)
        return {
            **result,
            "text": text,
            "raw_ids": raw_ids,
            "raw_text": self.duplex.tokenizer.decode(
                raw_ids, skip_special_tokens=False
            ),
            "delegates": delegates,
            "sample_rate": 24000,
            "pcm": None
            if wave is None or result["is_listen"] or hold
            else base64.b64encode(
                np.asarray(wave, dtype="<f4").reshape(-1).tobytes()
            ).decode(),
            "step_started_ns": start,
            "step_finished_ns": time.monotonic_ns(),
            "audio_end_sample": self.input_samples,
            "hold": hold,
            "explicit_response_start": start_response,
            "input_kind": ("approved_text" if approved_input else "native_text")
            if text_only
            else "audio",
            "backend_applied": [
                text for text in backend or [] if text.startswith("<backend>")
            ],
            "question_applied": [
                text for text in backend or [] if not text.startswith("<backend>")
            ],
        }

    def prefill(self, pcm, backend):
        return self.duplex.streaming_prefill(audio_waveform=pcm, text_list=backend)


def enqueue_aligned_media(incoming, command, *, keep_latest=False, capacity=4):
    dropped = []
    while incoming.qsize() >= capacity:
        if not keep_latest:
            raise ValueError("Media queue exceeded four seconds; resynchronize capture")
        try:
            dropped.append(incoming.get_nowait()["metadata"])
        except queue.Empty:
            break
    incoming.put_nowait(command)
    return dropped


def prepare_session(model, start, preparer=None, *, system_prompt=None):
    options = dict(start)
    if system_prompt is not None:
        options["system_prompt"] = system_prompt
    if preparer is not None:
        return preparer(model, options)
    return model.prepare(
        options["system_prompt"],
        int(options.get("seed", 42)),
        options.get("batch_system_prefill", False),
    )


def create_app(
    model_dir: str,
    *,
    model_factory=VenusAudio,
    model_name="Realtime-Venus-Audio",
    transport_limits=None,
    aligned_keep_latest=False,
    session_preparer=None,
    owner_protocol=None,
):
    from contextlib import asynccontextmanager

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="venus-owner")
    state = {"model": None, "busy": False}

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        state["model"] = await loop.run_in_executor(executor, model_factory, model_dir)
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        model = state["model"]
        return {
            "ready": model is not None,
            "busy": state["busy"],
            "model": model_name,
            "model_directory": str(model.model_dir) if model else None,
            "input_sample_rate": 16000,
            "output_sample_rate": 24000,
            "patches": model.patch_manifest if model else None,
        }

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        if state["busy"]:
            await ws.close(code=1013, reason="Single-session model owner is busy")
            return
        state["busy"] = True
        limits = transport_limits or {}
        incoming, controls = (
            queue.Queue(limits.get("incoming", 0)),
            queue.Queue(limits.get("controls", 0)),
        )
        outgoing = asyncio.Queue(limits.get("outgoing", 0))
        stop = threading.Event()
        epoch = [0]
        media_mode = [None]
        loop = asyncio.get_running_loop()
        model = state["model"]

        def emit(message):
            if not transport_limits:
                loop.call_soon_threadsafe(outgoing.put_nowait, message)
                return
            if stop.is_set():
                return
            pending = asyncio.run_coroutine_threadsafe(outgoing.put(message), loop)
            try:
                pending.result(timeout=2)
            except FutureTimeout:
                pending.cancel()
                stop.set()
                asyncio.run_coroutine_threadsafe(
                    ws.close(code=1013, reason="Model output backpressure"), loop
                )

        def owner(start):
            try:
                candidates = start.get("emit_text_candidates", False)
                if candidates and (
                    model.patch_manifest.get("text_candidate_protocol")
                    != "native_text_before_tts_v1"
                    or not start.get("incremental_tts", True)
                ):
                    raise ValueError(
                        "Text candidates require the declared Omni protocol and incremental TTS"
                    )
                prepare_session(model, start, session_preparer)
                if owner_protocol is not None:
                    if start.get("owner_protocol") != owner_protocol:
                        raise ValueError(
                            "This model service requires explicit protocol negotiation"
                        )
                    model.transport_start(start, emit)
                model.duplex._harness_drain_units = bool(
                    start.get("tts_drain_units", False)
                )
                immediate_approved = bool(start.get("speech_immediate_approved", False))
                immediate_native = bool(start.get("speech_immediate_native", False))
                if immediate_native and (
                    owner_protocol is not None or model_name != "Realtime-Venus-Omni"
                ):
                    raise ValueError("Immediate native text requires conventional Omni")
                if immediate_approved and (
                    owner_protocol is not None or model_name != "Realtime-Venus-Omni"
                ):
                    raise ValueError(
                        "Immediate approved speech requires the conventional Omni owner"
                    )
                reservation_ms = start.get("speech_decision_reservation_ms", 0)
                if (
                    type(reservation_ms) is not int
                    or not 0 <= reservation_ms <= 2000
                    or (reservation_ms and (not immediate_approved))
                ):
                    raise ValueError(
                        "Decision reservation requires immediate approved speech and at most 2000 ms"
                    )
                first_chunk_tokens = start.get("speech_first_chunk_tokens", 25)
                native_speech_tokens = start.get("native_speech_tokens", 20)
                text_decode_mode = start.get("speech_text_decode_mode", "sampling")
                if text_decode_mode not in {"sampling", "greedy"}:
                    raise ValueError("Unsupported speech text decoding mode")
                if type(
                    native_speech_tokens
                ) is not int or native_speech_tokens not in (12, 16, 20):
                    raise ValueError(
                        "Unsupported native language generation chunk size"
                    )
                speech_chunk_tokens = start.get("speech_chunk_tokens", 25)
                trim_zero_prefix_ms = start.get("speech_trim_zero_prefix_ms", 0)
                if (
                    type(speech_chunk_tokens) is not int
                    or speech_chunk_tokens not in (16, 25)
                    or type(trim_zero_prefix_ms) is not int
                    or (trim_zero_prefix_ms not in (0, 160))
                ):
                    raise ValueError("Unsupported speech chunk or zero-prefix option")
                if (
                    type(first_chunk_tokens) is not int
                    or first_chunk_tokens not in (6, 8, 12, 16, 25)
                    or first_chunk_tokens > speech_chunk_tokens
                    or (
                        (
                            first_chunk_tokens != 25
                            or speech_chunk_tokens != 25
                            or trim_zero_prefix_ms
                        )
                        and (
                            owner_protocol is not None
                            or model_name != "Realtime-Venus-Omni"
                            or (not start.get("incremental_tts", True))
                            or (not start.get("tts_drain_units"))
                        )
                    )
                ):
                    raise ValueError(
                        "Short first speech chunk requires incremental conventional Omni drain"
                    )
                if owner_protocol is None and model_name == "Realtime-Venus-Omni":
                    source = start.get("speech_audio_source", "mixed")
                    if source not in {"mixed", "microphone"}:
                        raise ValueError("Invalid Omni speech audio source")
                    model.speech_audio_source = source
                    visual_source = start.get("speech_visual_source", "screen")
                    if visual_source not in {"screen", "jev_evidence_only"}:
                        raise ValueError("Invalid Venus visual input scope")
                    model.speech_visual_source = visual_source
                    model.duplex._harness_tts_text_end = bool(
                        start.get("speech_text_end", False)
                    )
                    model.duplex._harness_batch_approved = bool(
                        start.get("speech_batch_approved", False)
                    )
                    model.duplex._harness_speech_first_chunk_tokens = first_chunk_tokens
                    model.duplex._harness_speech_chunk_tokens = speech_chunk_tokens
                    model.duplex._harness_native_speech_tokens = native_speech_tokens
                    model.duplex._harness_text_decode_mode = text_decode_mode
                    model.duplex._harness_immediate_native = immediate_native
                    model.patch_manifest = {
                        **model.patch_manifest,
                        "speech_audio_source": source,
                        "speech_visual_source": visual_source,
                        "speech_text_end": model.duplex._harness_tts_text_end,
                        "speech_batch_approved": model.duplex._harness_batch_approved,
                        "speech_immediate_approved": immediate_approved,
                        "speech_immediate_native": immediate_native,
                        "speech_decision_reservation_ms": reservation_ms,
                        "speech_first_chunk_tokens": first_chunk_tokens,
                        "native_speech_tokens": native_speech_tokens,
                        "speech_text_decode_mode": text_decode_mode,
                        "speech_chunk_tokens": speech_chunk_tokens,
                        "speech_trim_zero_prefix_ms": trim_zero_prefix_ms,
                    }
                hold = start.get("hold", True)
                applied_epoch = int(start.get("generation_epoch", 0))
                response_id = start.get("response_id", uuid4().hex)
                pending_backend = []
                pending_question = False
                pending_start = False
                buffer = np.zeros(0, dtype=np.float32)
                deferred_block = None
                media_defer_until = 0.0
                emit({"type": "ready", "patches": model.patch_manifest})
                while not stop.is_set():
                    while not controls.empty():
                        command = controls.get_nowait()
                        applied_epoch = max(
                            applied_epoch,
                            int(command.get("generation_epoch", applied_epoch)),
                        )
                        if (
                            "defer_media_ms" in command
                            and int(command.get("generation_epoch", applied_epoch))
                            == applied_epoch
                        ):
                            media_defer_until = command["_media_defer_deadline"]
                        if owner_protocol is not None and model.transport_control(
                            command, applied_epoch
                        ):
                            continue
                        if command.get("reset_context"):
                            prepare_session(
                                model,
                                start,
                                session_preparer,
                                system_prompt=command["system_prompt"],
                            )
                            model.duplex._harness_drain_units = bool(
                                start.get("tts_drain_units", False)
                            )
                            pending_backend.clear()
                            pending_start = False
                            hold = True
                            pending_question = False
                            buffer = np.zeros(0, dtype=np.float32)
                            deferred_block = None
                            media_defer_until = 0.0
                            while not incoming.empty():
                                incoming.get_nowait()
                            emit(
                                {
                                    "type": "context.reset",
                                    "generation_epoch": command["generation_epoch"],
                                }
                            )
                            continue
                        if command.get("end_response"):
                            model.end_response()
                            pending_backend.clear()
                            pending_question = False
                            pending_start = False
                        if "hold" in command:
                            hold = command["hold"]
                        if command.get("start_response") and (not hold):
                            pending_start = True
                            media_defer_until = 0.0
                        response_id = command.get("response_id", response_id)
                        if command.get("backend"):
                            pending_backend.append(
                                "<backend>" + command["backend"] + "</backend>"
                            )
                        if command.get("question"):
                            pending_backend.append(command["question"])
                            pending_question = True
                        if command.get("approved_text"):
                            from collections import deque

                            tokens = model.duplex.tokenizer.encode(
                                command["approved_text"], add_special_tokens=False
                            )
                            specials = set(model.duplex.tokenizer.all_special_ids)
                            model.duplex._harness_approved_ids = deque(
                                (t for t in tokens if t not in specials)
                            )
                            model.duplex._harness_approved_ended = False
                        emit(
                            {
                                "type": "control.applied",
                                "generation_epoch": applied_epoch,
                                "response_id": response_id,
                                "hold": hold,
                                "monotonic_ns": time.monotonic_ns(),
                                **(
                                    {
                                        "defer_media_ms": command["defer_media_ms"],
                                        "media_defer_until_ns": int(
                                            media_defer_until * 1000000000.0
                                        ),
                                    }
                                    if "defer_media_ms" in command
                                    else {}
                                ),
                            }
                        )
                    media_metadata = None
                    text_only = (
                        pending_start
                        and (not hold)
                        and bool(pending_backend)
                        and (
                            immediate_approved
                            and getattr(model.duplex, "_harness_approved_ids", None)
                            is not None
                            or (immediate_native and pending_question)
                        )
                    )
                    if not text_only:
                        remaining = media_defer_until - time.monotonic()
                        if remaining > 0:
                            stop.wait(min(0.01, remaining))
                            continue
                        if deferred_block is None:
                            try:
                                block = incoming.get(timeout=0.02)
                            except queue.Empty:
                                continue
                        else:
                            block, deferred_block = (deferred_block, None)
                        if not controls.empty():
                            deferred_block = block
                            continue
                        if isinstance(block, dict):
                            media_metadata = block.get("metadata")
                            block = model.set_media(block)
                        buffer = np.concatenate([buffer, block])
                        if len(buffer) < 16000:
                            continue
                    input_sample_count = 0 if text_only else 16000
                    input_kind = (
                        (
                            "approved_text"
                            if getattr(model.duplex, "_harness_approved_ids", None)
                            is not None
                            else "native_text"
                        )
                        if text_only
                        else "audio"
                    )
                    step_epoch = applied_epoch
                    step_response = response_id
                    native_fields = {}
                    if owner_protocol is not None and hasattr(
                        model, "transport_step_context"
                    ):
                        hold, pending_start, step_response, native_fields = (
                            model.transport_step_context(
                                hold,
                                pending_start,
                                step_response,
                                step_epoch,
                                pending_backend,
                            )
                        )
                    step_started_ns = time.monotonic_ns()
                    fragment_index = 0
                    candidate_id = f"{step_response}:{step_epoch}:{model.input_samples + input_sample_count}"

                    def stream_candidate(candidate):
                        stale = step_epoch != epoch[0] or stop.is_set()
                        emit(
                            {
                                "type": "text.candidate",
                                "generation_epoch": step_epoch,
                                "response_id": step_response,
                                "candidate_id": candidate_id,
                                "text": candidate["text"] if not stale else "",
                                "raw_ids": candidate["raw_ids"] if not stale else [],
                                "end_of_turn": candidate["end_of_turn"],
                                "contains_delegate": candidate["contains_delegate"],
                                "decision_status": "UNREVIEWED",
                                "dropped_stale_epoch": stale,
                                "step_started_ns": step_started_ns,
                                "candidate_ready_ns": time.monotonic_ns(),
                                "audio_end_sample": model.input_samples
                                + input_sample_count,
                                "media": media_metadata,
                            }
                        )

                    def stream_audio(waveform):
                        nonlocal fragment_index
                        stale = step_epoch != epoch[0] or stop.is_set()
                        waveform = np.asarray(waveform, dtype="<f4").reshape(-1)
                        removed_prefix = 0
                        prefix_samples = trim_zero_prefix_ms * 24
                        if (
                            not stale
                            and pending_start
                            and (fragment_index == 0)
                            and prefix_samples
                            and (len(waveform) > prefix_samples)
                            and (not np.any(waveform[:prefix_samples]))
                        ):
                            waveform = waveform[prefix_samples:]
                            removed_prefix = prefix_samples
                        emit(
                            {
                                "type": "step",
                                "generation_epoch": step_epoch,
                                "response_id": step_response,
                                "hold": hold,
                                "is_listen": False,
                                "end_of_turn": False,
                                "text": "",
                                "delegates": [],
                                "sample_rate": 24000,
                                "pcm": None
                                if stale
                                else base64.b64encode(waveform.tobytes()).decode(),
                                "removed_zero_prefix_samples": removed_prefix,
                                "audio_fragment": True,
                                "fragment_index": fragment_index,
                                **native_fields,
                                "dropped_stale_epoch": stale,
                                "audio_end_sample": model.input_samples
                                + input_sample_count,
                                "input_kind": input_kind,
                                "step_started_ns": step_started_ns,
                                "step_finished_ns": time.monotonic_ns(),
                                **(
                                    {"text_candidate_id": candidate_id}
                                    if candidates
                                    else {}
                                ),
                            }
                        )
                        fragment_index += 1

                    stream_audio.abort_requested = lambda: (
                        step_epoch != epoch[0] or stop.is_set()
                    )
                    if candidates:
                        stream_audio.text_candidate = stream_candidate
                    result = model.step(
                        None if text_only else buffer[:16000],
                        pending_backend or None,
                        hold,
                        pending_start,
                        audio_callback=stream_audio
                        if start.get("incremental_tts", True)
                        else None,
                    )
                    buffer = buffer[input_sample_count:]
                    pending_backend = []
                    pending_question = False
                    pending_start = False
                    result.update(
                        type="step",
                        generation_epoch=step_epoch,
                        response_id=step_response,
                        input_backlog_samples=len(buffer)
                        + incoming.qsize()
                        * (16000 if media_mode[0] == "aligned" else 320),
                        transport_queue_depths={
                            "incoming": incoming.qsize(),
                            "controls": controls.qsize(),
                            "outgoing": outgoing.qsize(),
                        },
                    )
                    result.update(native_fields)
                    if media_metadata is not None:
                        result["media"] = media_metadata
                    if candidates:
                        result["text_candidate_id"] = candidate_id
                    if step_epoch != epoch[0]:
                        result["pcm"] = None
                        result["dropped_stale_epoch"] = True
                    emit(result)
            except Exception as error:
                emit({"type": "error", "stage": "venus_owner", "error": str(error)})
            finally:
                emit({"type": "owner.stopped"})

        async def sender():
            try:
                while True:
                    message = await outgoing.get()
                    await ws.send_json(message)
                    if message["type"] == "owner.stopped":
                        return
            finally:
                if transport_limits:
                    stop.set()
                    with contextlib.suppress(Exception):
                        await ws.close()

        future = None
        send_task = None
        try:
            start = await asyncio.wait_for(ws.receive_json(), 15)
            if start.get("type") != "start" or not isinstance(
                start.get("system_prompt"), str
            ):
                raise ValueError("Expected start with system_prompt")
            epoch[0] = int(start.get("generation_epoch", 0))
            future = loop.run_in_executor(executor, owner, start)
            send_task = asyncio.create_task(sender())
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if media_mode[0] == "aligned":
                        raise ValueError(
                            "Cannot mix timestamped media with unaligned PCM"
                        )
                    media_mode[0] = "audio"
                    pcm = np.frombuffer(message["bytes"], dtype="<f4").copy()
                    if len(pcm) > 16000 or not np.isfinite(pcm).all():
                        raise ValueError("Invalid PCM frame")
                    incoming.put_nowait(pcm)
                elif message.get("text"):
                    command = json.loads(message["text"])
                    if command.get("type") == "media.step" and hasattr(
                        model, "set_media"
                    ):
                        if media_mode[0] == "audio":
                            raise ValueError(
                                "Cannot mix unaligned PCM with timestamped media"
                            )
                        media_mode[0] = "aligned"
                        dropped = enqueue_aligned_media(
                            incoming, command, keep_latest=aligned_keep_latest
                        )
                        for metadata in dropped:
                            await outgoing.put(
                                {
                                    "type": "media.gap",
                                    "reason": "model_input_backpressure",
                                    "skipped_unit": metadata,
                                    "model_input_only": True,
                                }
                            )
                        continue
                    if command.get("type") != "control":
                        raise ValueError("Unknown model command")
                    if "defer_media_ms" in command:
                        duration = command["defer_media_ms"]
                        limit = start.get("speech_decision_reservation_ms", 0)
                        if (
                            not limit
                            or type(duration) is not int
                            or (not 0 <= duration <= limit)
                        ):
                            raise ValueError(
                                "Media reservation exceeds the negotiated limit"
                            )
                        command["_media_defer_deadline"] = (
                            time.monotonic() + duration / 1000
                        )
                    if "generation_epoch" in command:
                        epoch[0] = max(epoch[0], int(command["generation_epoch"]))
                    controls.put_nowait(command)
        except WebSocketDisconnect:
            pass
        except Exception as error:
            if not transport_limits:
                raise
            with contextlib.suppress(Exception):
                await ws.send_json(
                    {
                        "type": "error",
                        "stage": "venus_transport",
                        "error": str(error)[:300],
                    }
                )
                await ws.close(code=1013, reason="Model transport failed")
        finally:
            stop.set()
            if future:
                await asyncio.shield(future)
            if send_task:
                send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)
            state["busy"] = False

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="model/Realtime-Venus-Audio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(create_app(args.model_dir), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
