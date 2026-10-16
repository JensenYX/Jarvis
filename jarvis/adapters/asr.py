from __future__ import annotations
import argparse
import asyncio
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect


class StreamingASR:
    def __init__(
        self,
        model_dir,
        chunk_seconds=2.0,
        silence_ms=700,
        threshold=0.5,
        pre_roll_ms=512,
    ):
        import torch
        from qwen_asr import Qwen3ASRModel
        from jarvis.adapters.bounded_vad import BoundedVAD

        torch.set_num_threads(4)
        self.model = Qwen3ASRModel.LLM(
            model=model_dir,
            gpu_memory_utilization=0.25,
            max_new_tokens=64,
            max_model_len=8192,
            enforce_eager=True,
            disable_log_stats=True,
        )
        self.vad = BoundedVAD()
        self.chunk_seconds, self.silence_ms, self.threshold = (
            chunk_seconds,
            silence_ms,
            threshold,
        )
        self.pre_roll_blocks = max(1, (pre_roll_ms + 31) // 32)
        self.reset()
        warm = self.model.init_streaming_state(
            chunk_size_sec=self.chunk_seconds, language="English"
        )
        self.model.streaming_transcribe(
            np.zeros(round(self.chunk_seconds * 16000), dtype=np.float32), warm
        )
        self.model.finish_streaming_transcribe(warm)
        self.reset()

    def reset(self, language=None, context=""):
        self.language = language
        self.context = context
        self.vad.reset_states()
        self.buffer = np.zeros(0, dtype=np.float32)
        self.pre_roll = deque(maxlen=self.pre_roll_blocks)
        self.state = None
        self.total_samples = 0
        self.silent_samples = 0
        self.revision = 0
        self.text_history = deque(maxlen=3)
        self.utterance_id = None

    def close(self):
        self.model.model.llm_engine.engine_core.shutdown()

    def _transcript(self, final):
        text = self.state.text.strip()
        if not final and self.text_history and (text == self.text_history[-1]):
            return None
        if not final and (not text):
            return None
        self.revision += 1
        self.text_history.append(text)
        stable = (
            os.path.commonprefix(list(self.text_history))
            if len(self.text_history) > 1
            else ""
        )
        return {
            "type": "asr.final" if final else "asr.partial",
            "monotonic_ns": time.monotonic_ns(),
            "payload": {
                "utterance_id": self.utterance_id,
                "revision": self.revision,
                "text": text,
                "is_final": final,
                "stable_prefix": text if final else stable,
                "language": self.state.language or self.language or "",
                "audio_end_sample": self.total_samples,
            },
            "language": self.state.language,
            "stable_prefix_method": "common prefix of last 3 real decodes",
        }

    def accept(self, pcm, emit):
        import torch

        self.buffer = np.concatenate([self.buffer, pcm])
        while len(self.buffer) >= 512:
            block, self.buffer = (self.buffer[:512], self.buffer[512:])
            self.total_samples += 512
            probability = float(self.vad(torch.from_numpy(block), 16000).item())
            speaking = probability >= self.threshold
            if speaking and self.state is None:
                self.utterance_id = uuid4().hex
                self.revision = 0
                self.text_history.clear()
                self.state = self.model.init_streaming_state(
                    chunk_size_sec=self.chunk_seconds,
                    unfixed_chunk_num=2,
                    unfixed_token_num=5,
                    language=self.language,
                    context=self.context,
                )
                emit(
                    {
                        "type": "user.speech.started",
                        "monotonic_ns": time.monotonic_ns(),
                        "payload": {
                            "utterance_id": self.utterance_id,
                            "audio_sample": self.total_samples - 512,
                            "vad_probability": probability,
                        },
                    }
                )
                if self.pre_roll:
                    self.model.streaming_transcribe(
                        np.concatenate(list(self.pre_roll)), self.state
                    )
                self.pre_roll.clear()
            if self.state is not None:
                self.silent_samples = 0 if speaking else self.silent_samples + 512
                self.model.streaming_transcribe(block, self.state)
                event = self._transcript(False)
                if event:
                    emit(event)
                if self.silent_samples >= self.silence_ms * 16:
                    emit(
                        {
                            "type": "user.speech.ended",
                            "monotonic_ns": time.monotonic_ns(),
                            "payload": {
                                "utterance_id": self.utterance_id,
                                "audio_sample": self.total_samples
                                - self.silent_samples,
                                "endpoint_observed_sample": self.total_samples,
                            },
                        }
                    )
                    self.model.finish_streaming_transcribe(self.state)
                    emit(self._transcript(True))
                    self.state = None
                    self.silent_samples = 0
            else:
                self.pre_roll.append(block.copy())


def create_app(
    model_dir, chunk_seconds=2.0, silence_ms=700, threshold=0.5, pre_roll_ms=512
):
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-owner")
    state = {"model": None, "busy": False}

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        state["model"] = await loop.run_in_executor(
            executor,
            StreamingASR,
            model_dir,
            chunk_seconds,
            silence_ms,
            threshold,
            pre_roll_ms,
        )
        try:
            yield
        finally:
            try:
                await loop.run_in_executor(executor, state["model"].close)
            finally:
                state["model"] = None
                executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        import importlib.metadata

        return {
            "ready": state["model"] is not None,
            "busy": state["busy"],
            "model": Path(model_dir).name,
            "model_directory": str(model_dir),
            "inference_warmed_before_ready": True,
            "backend": "vllm_streaming",
            "chunk_size_sec": chunk_seconds,
            "vad": {
                "model": "silero-vad",
                "version": importlib.metadata.version("silero-vad"),
                "threshold": threshold,
                "silence_ms": silence_ms,
                "pre_roll_ms": pre_roll_ms,
                "state_policy": "original plus two staggered 5.12-second histories; 256ms warmup",
            },
            "input_sample_rate": 16000,
            "context_supported": True,
        }

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        if state["busy"]:
            await ws.close(code=1013, reason="Single ASR session busy")
            return
        state["busy"] = True
        loop = asyncio.get_running_loop()
        incoming, outgoing = (asyncio.Queue(), asyncio.Queue())

        def emit(event):
            loop.call_soon_threadsafe(outgoing.put_nowait, event)

        async def consume():
            while True:
                pcm = await incoming.get()
                await loop.run_in_executor(executor, state["model"].accept, pcm, emit)

        async def send():
            while True:
                await ws.send_json(await outgoing.get())

        tasks = []
        try:
            language = {"en": "English", "zh": "Chinese", "auto": None}.get(
                ws.query_params.get("language", "auto")
            )
            context = ws.query_params.get("context", "")[:1600]
            await loop.run_in_executor(
                executor, state["model"].reset, language, context
            )
            await ws.send_json({"type": "ready", "chunk_size_sec": chunk_seconds})
            tasks = [asyncio.create_task(consume()), asyncio.create_task(send())]
            while True:
                data = await ws.receive_bytes()
                pcm = np.frombuffer(data, dtype="<f4").copy()
                if len(pcm) > 16000 or not np.isfinite(pcm).all():
                    raise ValueError("Invalid ASR input PCM frame")
                await incoming.put(pcm)
                for task in tasks:
                    if task.done():
                        task.result()
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await loop.run_in_executor(executor, lambda: None)
            state["busy"] = False

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--model-dir")
    parser.add_argument("--chunk-seconds", type=float)
    parser.add_argument("--silence-ms", type=int)
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--pre-roll-ms", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    from jarvis.config import load_config

    config = load_config(args.config)["asr"]
    import uvicorn

    uvicorn.run(
        create_app(
            str(Path(args.model_dir or config["model_dir"]).resolve()),
            args.chunk_seconds or config["chunk_size_sec"],
            args.silence_ms or config["vad_silence_ms"],
            args.threshold or config["vad_threshold"],
            args.pre_roll_ms or config.get("pre_roll_ms", 512),
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
