from __future__ import annotations
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from fastapi import FastAPI, HTTPException, Request
from jarvis.omni.decision.base import (
    Answer,
    DecisionInput,
    DecisionResult,
    UnsupportedCapability,
)
from jarvis.paths import resource, model_path

ROOT = resource()


def audited_module(path, name):
    model = json.loads(resource("models.json").read_text())["jev-omni"]
    expected = model["source_files"].get(path.name)
    if expected is None or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(
            "JEV model support file does not match the installed model release"
        )
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Owner:
    def __init__(self, directory, variant="unified", base_directory=None):
        import torch
        from transformers import AutoProcessor, Gemma4UnifiedForConditionalGeneration

        self.thread_id = threading.get_ident()
        self.directory = directory = directory.resolve()
        self.variant = variant
        self.torch = torch
        torch.set_num_threads(4)
        torch.set_float32_matmul_precision("highest")
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        loader = audited_module(directory / "load_model.py", "screen_jev_head")
        self.helper = audited_module(directory / "jev_omni.py", "screen_jev_helper")
        model = (
            Gemma4UnifiedForConditionalGeneration.from_pretrained(
                directory / "unified",
                dtype=torch.bfloat16,
                local_files_only=True,
                attn_implementation="sdpa",
            )
            .eval()
            .cuda()
        )
        config = json.loads((directory / "decision_config.json").read_text())
        head = loader.Head256(config["hidden_size"]).cuda().eval()
        head.load_state_dict(
            torch.load(directory / "head.pt", map_location="cuda", weights_only=True)
        )
        _, decoder = self.helper._find_backbone(model)
        processor = AutoProcessor.from_pretrained(
            directory / "unified", local_files_only=True
        )
        self.precision = "unified_bf16_head_fp32_autocast"
        model.requires_grad_(False)
        head.requires_grad_(False)
        self.classifier = self.helper.JevOmni(model, head, processor, decoder)
        self.revision = json.loads(resource("models.json").read_text())["jev-omni"][
            "revision"
        ]
        self.metrics = {
            "load_s": time.monotonic() - started,
            "allocated_bytes": torch.cuda.memory_allocated(),
            "peak_bytes": torch.cuda.max_memory_allocated(),
            "weights_dtype": str(next(model.parameters()).dtype),
            "head_dtype": str(head.linear.weight.dtype),
        }
        self.metrics["variant"] = variant
        self.metrics["parameter_dtypes"] = {
            str(dtype): sum((p.numel() for p in model.parameters() if p.dtype == dtype))
            for dtype in {p.dtype for p in model.parameters()}
        }

    def decide(self, request):
        from PIL import Image

        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Jev hook state may only be accessed by its owner")
        if len(request.images) > 16:
            raise UnsupportedCapability("At most 16 already-causal frames")
        started = time.monotonic()
        torch, classifier = (self.torch, self.classifier)
        images = []
        for image in request.images:
            binary = base64.b64decode(image.split(",", 1)[1], validate=True)
            if len(binary) > 4000000:
                raise ValueError("Image exceeds byte limit")
            picture = Image.open(io.BytesIO(binary))
            if picture.width * picture.height > 2073600:
                raise ValueError("Image exceeds pixel limit")
            images.append(picture.convert("RGB"))
        answers = []
        with tempfile.TemporaryDirectory(prefix="jev-media-") as temporary:
            audio_path = None
            if request.audio:
                binary = base64.b64decode(request.audio.split(",", 1)[1], validate=True)
                if len(binary) > 12000000:
                    raise ValueError("Audio window exceeds byte limit")
                source, audio_path = (
                    Path(temporary) / "input.wav",
                    Path(temporary) / "mono.wav",
                )
                source.write_bytes(binary)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-y",
                        "-v",
                        "error",
                        "-i",
                        str(source),
                        "-t",
                        "30",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        str(audio_path),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=20,
                )
            for q in request.questions:
                if len(q.option_ids()) < 2:
                    raise UnsupportedCapability(
                        "Jev-Omni publisher head requires at least two options"
                    )
                options = (
                    [
                        "False: the proposition is not supported",
                        "True: the proposition is supported",
                    ]
                    if q.primitive == "noul"
                    else [f"{o.id}: {o.text}" for o in q.options]
                )
                content = [{"type": "image", "image": image} for image in images]
                if audio_path:
                    content.append({"type": "audio", "audio": str(audio_path)})
                content.append(
                    {
                        "type": "text",
                        "text": self.helper._prompt(
                            json.dumps(request.state, ensure_ascii=False),
                            q.instructions,
                            options,
                        ),
                    }
                )
                inputs = classifier.processor.apply_chat_template(
                    [{"role": "user", "content": content}],
                    add_generation_prompt=True,
                    tokenize=True,
                    return_dict=True,
                    return_tensors="pt",
                    enable_thinking=False,
                )
                inputs = {
                    k: v.to("cuda", dtype=torch.bfloat16)
                    if torch.is_floating_point(v)
                    else v.to("cuda")
                    for k, v in inputs.items()
                }
                classifier._capture.clear()
                with (
                    torch.inference_mode(),
                    torch.autocast("cuda", dtype=torch.bfloat16),
                ):
                    classifier.model(**inputs, use_cache=False, **classifier._extra)
                    logits = classifier.head(
                        classifier._capture["hidden"],
                        torch.tensor([len(options)], device="cuda"),
                    )[0, : len(options)]
                    probs = logits.softmax(-1).float().cpu().tolist()
                    raw_logits = logits.float().cpu().tolist()
                best = max(range(len(probs)), key=probs.__getitem__)
                ids = q.option_ids()
                answers.append(
                    Answer(
                        question_id=q.id,
                        primitive=q.primitive,
                        implementation="native"
                        if q.primitive == "choice"
                        else "emulated",
                        option_ids=ids,
                        raw_probs=probs,
                        raw_logits=raw_logits,
                        choice=ids[best] if q.primitive == "choice" else None,
                        expected_score=sum((i * p for i, p in enumerate(probs)))
                        if q.primitive == "score"
                        else None,
                        noul=probs[1] if q.primitive == "noul" else None,
                        native_confidence=max(probs),
                        confidence_kind="maximum_option_probability",
                        raw={
                            "prediction_index": best,
                            "options": options,
                            "probabilities": probs,
                            "mapping": "false_true"
                            if q.primitive == "noul"
                            else "zero_based_rubric"
                            if q.primitive == "score"
                            else "ordered_option_ids",
                        },
                    )
                )
        return DecisionResult(
            backend="jev_omni",
            model="akhilaaa3/Jev-Omni",
            revision=self.revision,
            precision=self.precision,
            input_hash=request.input_hash(),
            answers=tuple(answers),
            elapsed_ms=(time.monotonic() - started) * 1000,
            raw={
                "variant": self.variant,
                "owner_serialized": True,
                "model_metrics": self.metrics,
                "peak_bytes": torch.cuda.max_memory_allocated(),
                "image_count": len(images),
            },
        )


def create_app(directory, variant="unified", *, owner_factory=Owner):
    from jarvis.adapters.inference_lifecycle import (
        InferenceControl,
        InferenceQueue,
        InferenceAborted,
    )

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-omni-owner")
    state = {"owner": None, "pending": 0}
    queue = InferenceQueue(executor, capacity=4)

    @asynccontextmanager
    async def lifespan(app):
        state["owner"] = await asyncio.get_running_loop().run_in_executor(
            executor, owner_factory, directory, variant
        )
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {
            "ready": state["owner"] is not None,
            "pending": queue.pending,
            "model": "Jev-Omni/unified" if variant == "unified" else "Jev-Omni/legacy",
            "queue_protocol": "bounded_priority_fifo_v1",
            "metrics": state["owner"].metrics if state["owner"] else None,
        }

    async def run_decision(request: DecisionInput, transport: Request):
        method = state["owner"].decide
        received = time.monotonic()
        control = InferenceControl()

        def operation():
            started = time.monotonic()
            value = method(request)
            return value.model_copy(
                update={
                    "raw": {
                        **value.raw,
                        "queue_wait_ms": (started - received) * 1000,
                        "owner_execution_ms": (time.monotonic() - started) * 1000,
                    }
                }
            )

        try:
            return await queue.invoke(
                operation,
                control=control,
                disconnected=transport.is_disconnected,
                cancel_on_disconnect=True,
                priority=10
                if transport.headers.get("x-inference-priority") == "background"
                else 0,
            )
        except OverflowError as exc:
            raise HTTPException(429, "Decision owner queue is full") from exc
        except InferenceAborted as exc:
            raise HTTPException(499, "Cancelled decision request") from exc
        except (ValueError, UnsupportedCapability) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/decide")
    async def decide(request: DecisionInput, transport: Request):
        return await run_decision(request, transport)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=model_path("jev-omni"))
    parser.add_argument("--port", type=int, default=8810)
    parser.add_argument("--variant", choices=["unified"], default="unified")
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(
        create_app(args.model_dir, args.variant), host="127.0.0.1", port=args.port
    )


if __name__ == "__main__":
    main()
