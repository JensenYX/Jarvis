from __future__ import annotations
import argparse
import base64
import hashlib
import io
from pathlib import Path
import threading
import numpy as np
from PIL import Image
from jarvis.adapters.omni.venus_audio import DelegateParser, VenusAudio, create_app
from jarvis.adapters.omni.venus_omni_patches import install
from jarvis.adapters.omni.venus_reference_audio import (
    reset_reference_audio_normalization,
)


class VenusOmni(VenusAudio):
    def __init__(self, model_dir):
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
                init_vision=True,
                init_audio=True,
                init_tts=True,
            )
            .eval()
            .cuda()
        )
        self.model.requires_grad_(False)
        import os
        from jarvis.adapters.omni.venus_omni_patches import (
            enable_tts_graph,
            fp16_stream_cache,
            recent_answer_block,
        )

        tts_float16 = os.environ.get("VENUS_TTS_FLOAT16") == "1"
        tts_steps = int(os.environ.get("VENUS_TTS_STEPS", "10"))
        if not 2 <= tts_steps <= 10:
            raise ValueError("Flow-matching steps must be between 2 and 10")
        self.duplex = self.model.as_duplex(
            generate_audio=True,
            sliding_window_mode="basic",
            enable_float16=tts_float16,
            n_timesteps=tts_steps,
        )
        self.model.tts.config.s3_stream_n_timesteps = tts_steps
        fp16_stream_cache(self.model.tts.audio_tokenizer)
        speed = {"tts_float16": tts_float16, "tts_flow_steps": tts_steps}
        if os.environ.get("VENUS_TTS_GRAPH") == "1":
            speed.update(enable_tts_graph(self.model.tts))
        if os.environ.get("VENUS_RECENT_BLOCK"):
            speed.update(
                recent_answer_block(self.duplex, int(os.environ["VENUS_RECENT_BLOCK"]))
            )
        self.patch_manifest = {**install(self.duplex), **speed}
        self.frames = None
        self.media_sequence = -1
        self.diagnostic_tokens_exported = 0
        self.candidate_parser = DelegateParser(self.duplex.tokenizer)

    def end_response(self):
        super().end_response()
        self.candidate_parser = DelegateParser(self.duplex.tokenizer)

    def prepare(self, system_prompt, seed=42, batch_prefill=False):
        self.assert_owner()
        reset_reference_audio_normalization(self.model.processor)
        super().prepare(system_prompt, seed, batch_prefill)
        self.frames = None
        self.media_sequence = -1
        self.diagnostic_tokens_exported = 0
        self.candidate_parser = DelegateParser(self.duplex.tokenizer)

    def step(self, *args, **kwargs):
        import torch

        sink = getattr(kwargs.get("audio_callback"), "text_candidate", None)

        def candidate(tokens, ended):
            text, delegates = self.candidate_parser.feed(tokens)
            sink(
                {
                    "text": text,
                    "raw_ids": tokens,
                    "end_of_turn": bool(ended),
                    "contains_delegate": bool(delegates),
                }
            )

        self.duplex._harness_text_candidate_callback = candidate if sink else None
        try:
            result = super().step(*args, **kwargs)
        finally:
            self.duplex._harness_text_candidate_callback = None
        self.diagnostic_tokens_exported += len(result["raw_ids"])
        for field in ("total_ids", "res_ids", "total_hidden", "prefill_schema_tokens"):
            getattr(self.duplex, field).clear()
        self.cursor = 0
        decoder = self.duplex.decoder
        window = {
            "cache_length": decoder.get_cache_length(),
            "unit_count": len(decoder._unit_history),
            "system_preserve_length": decoder._system_preserve_length,
            "position_offset": decoder._position_offset,
            "window_enabled": decoder._window_enabled,
            "next_unit_id": decoder._next_unit_id,
            "sliding_window_mode": decoder._window_config.sliding_window_mode,
            "high_tokens": decoder._window_config.basic_window_high_tokens,
            "low_tokens": decoder._window_config.basic_window_low_tokens,
        }
        result["runtime_metrics"] = {
            "window": window,
            "cuda_allocated_bytes": torch.cuda.memory_allocated(),
            "cuda_reserved_bytes": torch.cuda.memory_reserved(),
            "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "diagnostic_tokens_exported": self.diagnostic_tokens_exported,
            "diagnostic_units_retained": len(self.duplex.total_hidden),
        }
        return result

    def set_media(self, packet):
        self.assert_owner()
        metadata = packet.get("metadata", {})
        sequence = int(metadata["sequence_id"])
        if sequence <= self.media_sequence:
            raise ValueError("Reordered media unit")
        audio_key = (
            "microphone_pcm"
            if getattr(self, "speech_audio_source", "mixed") == "microphone"
            else "pcm"
        )
        pcm = np.frombuffer(
            base64.b64decode(packet[audio_key], validate=True), dtype="<f4"
        ).copy()
        if len(pcm) != 16000 or not np.isfinite(pcm).all():
            raise ValueError(
                "Aligned Omni input needs one second of finite 16kHz mono PCM"
            )
        encoded = packet.get("frame")
        self.frames = None
        if (
            encoded is not None
            and getattr(self, "speech_visual_source", "screen") == "screen"
        ):
            data = base64.b64decode(encoded, validate=True)
            if len(data) > 4000000:
                raise ValueError("Frame exceeds byte limit")
            frame = Image.open(io.BytesIO(data))
            if frame.width * frame.height > 2073600:
                raise ValueError("Frame exceeds pixel limit")
            if metadata.get("frame_sha256") != hashlib.sha256(data).hexdigest():
                raise ValueError("Frame hash mismatch")
            if float(metadata["frame_capture_ts_ms"]) > float(
                metadata["audio_end_ts_ms"]
            ):
                raise ValueError("Future frame relative to audio unit")
            self.frames = [frame.convert("RGB")]
        self.media_sequence = sequence
        return pcm

    def prefill(self, pcm, backend):
        frames, self.frames = (self.frames, None)
        return self.duplex.streaming_prefill(
            audio_waveform=pcm, frame_list=frames, text_list=backend
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-dir", default="model/venus-release/Realtime-Venus-Omni"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8806)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(
        create_app(
            args.model_dir,
            model_factory=VenusOmni,
            model_name="Realtime-Venus-Omni",
            transport_limits={"incoming": 200, "controls": 128, "outgoing": 128},
            aligned_keep_latest=True,
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
