from __future__ import annotations
from jarvis.paths import data_root
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import queue
import threading
import time
from pathlib import Path
from uuid import uuid4
import numpy as np
from aiohttp import web, WSMsgType


class VoiceChat:
    def __init__(
        self,
        model_dir,
        upstream,
        engine_type="native",
        torch_threads=None,
        quiet_steps=False,
        converted_llm=None,
        converted_eartts=None,
        max_model_len=None,
    ):
        import torch

        if torch_threads is not None:
            if torch_threads < 1:
                raise ValueError("torch_threads must be positive")
            torch.set_num_threads(torch_threads)
            torch.set_num_interop_threads(torch_threads)
        from omegaconf import OmegaConf

        if "vllm" in engine_type:
            import vllm.config.model as vllm_model_config

            if not hasattr(vllm_model_config, "CustomInputSpec"):
                vllm_model_config.CustomInputSpec = vllm_model_config.CustomIOSpec
        from nemo.collections.speechlm2.inference.factory.s2s_pipeline_builder import (
            S2SPipelineBuilder,
        )

        config = (
            Path(upstream)
            / "examples/speechlm2/nemo_inference_pipelines/conf/s2s_streaming.yaml"
        )
        self.function_template = (
            Path(upstream) / "examples/speechlm2/function_calling/template.jinja"
        ).read_text()
        cfg = OmegaConf.load(config)
        for key, value in {
            "s2s.model_path": model_dir,
            "s2s.llm_checkpoint_path": model_dir,
            "s2s.speaker_reference": None,
            "s2s.speaker_name": "Aria",
            "s2s.engine_type": engine_type,
            "s2s.device_id": 0,
            "s2s.vllm_llm_config.device_id": None,
            "s2s.vllm_tts_config.device_id": None,
            "s2s.system_prompt": "You are a helpful voice assistant. Respond in English.",
            "s2s.tts_system_prompt": None,
            "s2s.enable_builtin_tools": False,
            "s2s.fc_async_enabled": True,
            "s2s.fc_async_two_phase": True,
            "s2s.fc_tool_timeout_sec": 180.0,
            "s2s.fc_convert_num_to_text": False,
            "streaming.chunk_size_in_secs": 0.08,
            "streaming.buffer_size_in_secs": 5.6,
        }.items():
            OmegaConf.update(cfg, key, value, force_add=True)
        for kind, path in [("llm", converted_llm), ("tts", converted_eartts)]:
            if path is not None:
                if (
                    not (Path(path) / "config.json").is_file()
                    or not (Path(path) / "model.safetensors").is_file()
                ):
                    raise ValueError("Incomplete preconverted checkpoint: " + str(path))
                OmegaConf.update(
                    cfg, f"s2s.vllm_{kind}_config.engine_path", path, force_add=True
                )
        if max_model_len is not None:
            if (
                engine_type != "vllm_llm_vllm_eartts"
                or not 6144 <= max_model_len <= 131072
            ):
                raise ValueError(
                    "Context override requires both vLLM engines and 6144..131072 tokens"
                )
            for key in (
                "s2s.vllm_llm_config.max_model_len",
                "s2s.vllm_tts_config.max_model_len",
                "streaming.max_len",
            ):
                OmegaConf.update(cfg, key, max_model_len, force_add=True)
        self.context_capacity = {
            "llm": int(cfg.s2s.vllm_llm_config.max_model_len),
            "eartts": int(cfg.s2s.vllm_tts_config.max_model_len),
            "streaming": int(cfg.streaming.max_len),
        }
        self.pipeline = S2SPipelineBuilder.build_pipeline(cfg)
        self.engine_type = engine_type
        self.cache_manifest = {"implementation": "upstream", "engine_type": engine_type}
        print(json.dumps({"cache": self.cache_manifest}), flush=True)
        self.pipeline.open_session()
        self.pipeline.warmup()
        self.pipeline.close_session()
        self.cleanup_error = None
        self.closed_sessions = 0
        if quiet_steps:
            from nemo.utils import logging as nemo_logging

            nemo_logging.set_verbosity(nemo_logging.WARNING)
        self.chunk = int(
            self.pipeline.chunk_size_in_secs * self.pipeline.input_sample_rate
        )
        self.native_output_rate = int(self.pipeline.output_sample_rate)
        self.output_rate = 24000
        self.model_dir = model_dir

    def close_stream(self, stream, stop):
        stop.set()
        pipeline = self.pipeline
        background = getattr(pipeline, "_fc_async_bg", {})
        bg = background.get(stream, {})
        for key in ("abort_event", "quit_async_event"):
            event = bg.get(key)
            if event is not None:
                event.set()
        thread = bg.get("thread")
        try:
            if thread is not None:
                thread.join(timeout=10)
                if thread.is_alive():
                    raise RuntimeError(
                        "Native function thread did not stop; actor quarantined"
                    )
            abort = getattr(pipeline.s2s_model, "abort_request", None)
            if callable(abort):
                abort(str(stream))
            pipeline.close_session()
            background.pop(stream, None)
            pipeline.tool_registry = {}
            self.closed_sessions += 1
        except Exception as exc:
            self.cleanup_error = str(exc)
            raise

    def run(self, start, incoming, replies, stop, emit):
        import torch
        from nemo.collections.asr.inference.streaming.framing.request import Frame
        from nemo.collections.speechlm2.inference.streaming.framing.s2s_request_options import (
            S2SRequestOptions,
        )

        pipeline = self.pipeline
        stream = int(uuid4().int % 2**31)
        pending = {}
        tools = [t.get("function", t) for t in start.get("tools", [])]

        def handler(name):

            def call(arguments):
                call_id = uuid4().hex
                waiter = queue.Queue()
                pending[call_id] = waiter
                emit(
                    {
                        "type": "tool.call",
                        "name": name,
                        "arguments": arguments,
                        "call_id": call_id,
                    }
                )
                deadline = time.monotonic() + 180
                try:
                    while not stop.is_set() and time.monotonic() < deadline:
                        try:
                            result = waiter.get(timeout=0.1)
                            return json.dumps(result, ensure_ascii=True)
                        except queue.Empty:
                            continue
                    return json.dumps({"error": "Tool transport stopped or timed out"})
                finally:
                    pending.pop(call_id, None)

            return call

        pipeline.tool_registry = {t["name"]: handler(t["name"]) for t in tools}
        system = str(
            start.get("policy")
            or "You are a helpful spoken assistant. Respond in English."
        )
        if tools:
            from jinja2 import Environment

            system = (
                Environment()
                .from_string(self.function_template)
                .render(system_message=system, tools=tools)
            )
        options = S2SRequestOptions(system_prompt=system)
        buffer = np.zeros(0, np.float32)
        text_pos, asr_pos = (0, 0)
        first = True
        input_samples = 0
        prior_agent_idle = True
        try:
            with torch.inference_mode():
                pipeline.generate_step(
                    [
                        Frame(
                            samples=torch.empty(0),
                            stream_id=stream,
                            is_first=True,
                            is_last=False,
                            options=options,
                        )
                    ]
                )
            first = False
            emit(
                {
                    "type": "session",
                    "session_id": str(stream),
                    "provider": start.get("provider", "voicechat_native"),
                    "input_sample_rate": 16000,
                    "output_sample_rate": self.output_rate,
                    "generation_epoch": 0,
                    "native_frame_ms": 80,
                }
            )
            while not stop.is_set():
                while not replies.empty():
                    reply = replies.get_nowait()
                    if reply.get("call_id") in pending:
                        pending[reply["call_id"]].put(reply.get("result"))
                try:
                    block = incoming.get(timeout=0.01)
                except queue.Empty:
                    continue
                buffer = np.concatenate([buffer, block])
                while len(buffer) >= self.chunk and (not stop.is_set()):
                    frame = Frame(
                        samples=torch.from_numpy(buffer[: self.chunk].copy()),
                        stream_id=stream,
                        is_first=first,
                        is_last=False,
                        options=options if first else None,
                    )
                    buffer = buffer[self.chunk :]
                    started = time.monotonic_ns()
                    with torch.inference_mode():
                        pipeline.generate_step([frame])
                    first = False
                    input_samples += self.chunk
                    state = pipeline.get_or_create_state(stream)
                    agent_idle = bool(state.is_agent_idle())
                    agent_turn_ended = not prior_agent_idle and agent_idle
                    prior_agent_idle = agent_idle
                    audio = state.audio_buffer
                    if audio is not None and audio.numel():
                        wave = audio.detach().float().cpu().numpy().reshape(-1)
                        if self.native_output_rate != self.output_rate:
                            from scipy.signal import resample_poly
                            from math import gcd

                            divisor = gcd(self.output_rate, self.native_output_rate)
                            wave = resample_poly(
                                wave,
                                self.output_rate // divisor,
                                self.native_output_rate // divisor,
                            )
                        emit(
                            {
                                "type": "audio",
                                "pcm": base64.b64encode(
                                    wave.astype("<f4").tobytes()
                                ).decode(),
                                "sample_rate": self.output_rate,
                                "generation_epoch": 0,
                                "response_id": str(stream),
                                "chunk_id": uuid4().hex,
                            }
                        )
                    text, asr = (state.get_output_text(), state.get_output_asr_text())
                    delta, asr_delta = (
                        text[text_pos:],
                        asr[asr_pos:] if len(asr) >= asr_pos else asr,
                    )
                    text_pos, asr_pos = (len(text), len(asr))
                    emit(
                        {
                            "type": "event",
                            "event": {
                                "type": "native.step",
                                "model": "voicechat",
                                "audio_end_sample": input_samples,
                                "step_started_ns": started,
                                "step_finished_ns": time.monotonic_ns(),
                                "text": delta,
                                "asr": asr_delta,
                                "agent_idle": agent_idle,
                                "agent_turn_ended": agent_turn_ended,
                                "turn_boundary_source": "upstream_bos_eos_or_abort",
                                "input_backlog_samples": len(buffer)
                                + incoming.qsize() * 320,
                            },
                        }
                    )
                    state.cleanup_after_response()
        except Exception as exc:
            import traceback

            traceback.print_exc()
            emit({"type": "error", "stage": "voicechat", "error": str(exc)})
        finally:
            try:
                self.close_stream(stream, stop)
            finally:
                emit({"type": "owner.stopped"})


def create_app(args):
    app = web.Application()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voicechat-owner")
    state = {"model": None, "busy": False}

    async def setup(app):
        state["model"] = await asyncio.get_running_loop().run_in_executor(
            executor,
            VoiceChat,
            args.model_dir,
            args.upstream,
            args.engine_type,
            args.torch_threads,
            args.quiet_steps,
            args.converted_llm,
            args.converted_eartts,
            args.max_model_len,
        )

    async def health(request):
        model = state["model"]
        return web.json_response(
            {
                "ready": model is not None and model.cleanup_error is None,
                "busy": state["busy"],
                "cleanup_error": model.cleanup_error if model else None,
                "closed_sessions": model.closed_sessions if model else 0,
                "model": "NVIDIA-NemotronLabs-VoiceChat-11B",
                "engine_type": args.engine_type,
                "context_capacity": model.context_capacity if model else None,
                "native_function_calls": True,
                "weights_modified": False,
                "cache": state["model"].cache_manifest if state["model"] else None,
            }
        )

    async def session(request):
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        if state["busy"] or state["model"] is None or state["model"].cleanup_error:
            await ws.close(code=1013, message=b"Model busy")
            return ws
        state["busy"] = True
        incoming, replies = (queue.Queue(), queue.Queue())
        outgoing = asyncio.Queue()
        stop = threading.Event()
        loop = asyncio.get_running_loop()
        future = send_task = None
        try:
            start = await asyncio.wait_for(ws.receive_json(), 20)
            if start.get("type") != "start":
                raise ValueError("Expected session start")

            def emit(event):
                loop.call_soon_threadsafe(outgoing.put_nowait, event)

            async def send():
                while True:
                    event = await outgoing.get()
                    if event["type"] == "owner.stopped":
                        break
                    await ws.send_json(event)

            send_task = asyncio.create_task(send())
            future = loop.run_in_executor(
                executor, state["model"].run, start, incoming, replies, stop, emit
            )
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    block = np.frombuffer(msg.data, dtype="<f4").copy()
                    if len(block) > 16000 or not np.isfinite(block).all():
                        raise ValueError("Invalid audio frame")
                    incoming.put(block)
                elif msg.type == WSMsgType.TEXT:
                    value = json.loads(msg.data)
                    if value.get("type") == "tool.result":
                        replies.put(value)
                    elif value.get("type") == "end":
                        break
        finally:
            stop.set()

            async def finish_owner():
                try:
                    if future is not None:
                        await future
                except Exception as exc:
                    state["model"].cleanup_error = str(exc)
                finally:
                    if send_task is not None:
                        send_task.cancel()
                        await asyncio.gather(send_task, return_exceptions=True)
                    state["busy"] = False

            cleanup = asyncio.create_task(finish_owner())
            app["owner_cleanup"] = cleanup
            await asyncio.shield(cleanup)
        return ws

    app.on_startup.append(setup)
    app.router.add_get("/health", health)
    app.router.add_get("/ws", session)
    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model-dir", required=True)
    p.add_argument("--upstream", default=str(data_root() / "sources/voicechat"))
    p.add_argument(
        "--engine-type",
        choices=["native", "vllm_llm", "vllm_eartts", "vllm_llm_vllm_eartts"],
        default="native",
    )
    p.add_argument("--torch-threads", type=int, default=None)
    p.add_argument("--converted-llm", default=None)
    p.add_argument("--converted-eartts", default=None)
    p.add_argument(
        "--max-model-len",
        type=int,
        default=None,
        help="Explicit shared LLM/EarTTS context capacity; leaves upstream defaults unchanged when omitted",
    )
    p.add_argument(
        "--quiet-steps",
        action="store_true",
        help="Suppress verbose upstream per-frame INFO logs after initialization",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=28100)
    a = p.parse_args()
    web.run_app(create_app(a), host=a.host, port=a.port)
