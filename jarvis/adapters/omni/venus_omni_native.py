import argparse
import base64
import hashlib
import json
from pathlib import Path
import time
import types
from uuid import uuid4
import numpy as np
import torch
from jarvis.omni.native_context import decision_prompt
from jarvis.adapters.omni.venus_audio import create_app
from jarvis.adapters.omni.venus_omni import VenusOmni
from jarvis.adapters.omni.venus_branches import OmniBranchState
from jarvis.adapters.gates.omni_policy import (
    attach_lora,
    load_adapter,
    adapters,
    RoutePolicy,
)
from jarvis.adapters.gates.omni_stream import NativeRouteSession
from jarvis.paths import resource, weight, model_path
from jarvis.adapters.weights import load_weights


class NativeVenusOmni(VenusOmni):
    def __init__(
        self,
        model_dir,
        checkpoint,
        *,
        engagement_policy=False,
        full_prefix_decisions=False,
    ):
        super().__init__(model_dir)

        def read_checkpoint(path):
            path = Path(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return digest, load_weights(path)

        digest, state = read_checkpoint(checkpoint)
        self.adapter_states = {"default": (digest, state)}
        attach_lora(self.model)
        load_adapter(self.model, state)
        init_tts = self.model.init_tts
        self.model.init_tts = types.MethodType(lambda model, *a, **k: None, self.model)
        try:
            self.route_duplex = self.model.as_duplex(
                generate_audio=False, sliding_window_mode="off"
            )
        finally:
            self.model.init_tts = init_tts
        self.policy = RoutePolicy(self.model, self.duplex.tokenizer)
        self.request_policy = self.policy
        self.engagement_policy = None
        if engagement_policy:
            from jarvis.adapters.gates.omni_engagement_policy import EngagementPolicy

            self.engagement_policy = EngagementPolicy(self.model, self.duplex.tokenizer)
        self.full_prefix_decisions = full_prefix_decisions
        self.route_prompt = resource(
            "prompts", "omni-native-route-prompt.txt"
        ).read_text()
        self.request_prompt = self.route_prompt
        self.patch_manifest = {
            **self.patch_manifest,
            "owner_protocol": "omni_native_route_v1",
            "route_adapter_sha256": digest,
            "route_actor_asr": False,
            "route_audio": "separate_microphone",
            "speech_adapter": False,
            "base_weights_frozen": True,
            "weights_modified": True,
            "trainable_route_parameters": 6193152,
            "route_execution": "full_prefix"
            if full_prefix_decisions
            else "incremental_kv",
        }
        self.route_prior_context = None
        self.native_emit = None
        self.native_mode = "listen"
        self.native_game = "unknown"
        self.proactive = False
        self.native_reset = True
        self.last_sample = None
        self.consumed_sample = -1
        self.route_state = None

    def prepare(self, *args, **kwargs):
        with adapters(self.model, False):
            super().prepare(*args, **kwargs)
        self.native_reset = True
        self.native_mode = "listen"
        self.last_sample = None
        self.route_state = None
        self.route_prior_context = None

    def transport_start(self, start, emit):
        self.native_emit = emit
        cfg = start.get("native_routing", {})
        selected = cfg.get("adapter", "default")
        if selected not in self.adapter_states:
            raise ValueError("Unregistered native routing adapter")
        digest, state = self.adapter_states[selected]
        load_adapter(self.model, state)
        self.patch_manifest = {
            **self.patch_manifest,
            "route_adapter_sha256": digest,
            "route_adapter": selected,
        }
        self.native_game = "unknown"
        self.proactive = cfg.get("proactive", False)
        if type(self.proactive) is not bool or (
            self.proactive and self.engagement_policy is None
        ):
            raise ValueError(
                "Proactive routing requires the native engagement policy capability"
            )
        self.policy = self.engagement_policy if self.proactive else self.request_policy
        self.route_prompt = (
            resource("prompts", "omni-native-engagement-prompt.txt").read_text()
            if self.proactive
            else self.request_prompt
        )
        self.use_prior_context = bool(cfg.get("prior_context", False))
        self.continuous_decision = bool(cfg.get("continuous_decision_context", False))
        self.speech_audio_source = cfg.get("speech_audio_source", "mixed")
        if self.speech_audio_source not in {"mixed", "microphone"}:
            raise ValueError("Unknown native speech audio source")
        self.speech_visual_context = cfg.get("speech_visual_context", True)
        if type(self.speech_visual_context) is not bool:
            raise ValueError("Native speech visual context must be boolean")
        self.speech_text_end = cfg.get("speech_text_end", False)
        if type(self.speech_text_end) is not bool:
            raise ValueError("Native speech text termination must be boolean")
        self.duplex._harness_tts_text_end = self.speech_text_end
        self.patch_manifest = {
            **self.patch_manifest,
            "continuous_decision_context": self.continuous_decision,
            "speech_audio_source": self.speech_audio_source,
            "speech_visual_context": self.speech_visual_context,
            "speech_text_end": self.speech_text_end,
            "proactive": self.proactive,
            "route_action_space": "listen_direct_delegate_request_scene"
            if self.proactive
            else "listen_direct_delegate",
        }
        self.threshold = float(cfg.get("threshold", 0.8))
        self.route_capacity = int(cfg.get("max_tokens", 4096))
        self.frame_stride = int(cfg.get("frame_stride", 3))
        self.route_window_units = cfg.get("decision_window_units", 0)
        if (
            type(self.route_window_units) is not int
            or self.route_window_units < 0
            or self.route_window_units == 1
            or (self.route_window_units and (not self.full_prefix_decisions))
        ):
            raise ValueError(
                "A bounded decision window requires full-prefix execution and at least two units"
            )
        self.decision_diagnostics = bool(cfg.get("decision_diagnostics", False))
        self.patch_manifest = {
            **self.patch_manifest,
            "decision_window_units": self.route_window_units,
        }
        if not 0.5 <= self.threshold <= 1 or self.frame_stride < 1:
            raise ValueError("Invalid native routing settings")
        self.native_reset = True
        self.consumed_sample = -1

    def transport_control(self, command, epoch):
        if "native_context" in command:
            if int(command.get("generation_epoch", epoch)) < epoch:
                return True
            text = (
                json.dumps(command["native_context"], ensure_ascii=False)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
            )
            if len(text) > 24000:
                raise ValueError("Native operation context exceeds capacity")
            prior_context = command["native_context"].get("prior_observations")
            if self.proactive and prior_context != self.route_prior_context:
                self.native_reset = True
            self.route_prior_context = prior_context
            self.native_emit(
                {
                    "type": "native.context.applied",
                    "generation_epoch": epoch,
                    "prior_context_sha256": hashlib.sha256(
                        json.dumps(
                            self.route_prior_context,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                }
            )
            return True
        if command.get("native_suspend"):
            self.native_mode = "suspended"
            self.native_reset = True
            return True
        if "game" in command:
            if int(command.get("generation_epoch", epoch)) < epoch:
                return True
            if command["game"] != self.native_game:
                self.native_game = command["game"]
                self.native_reset = True
        if "router_consumed_sample" in command:
            self.consumed_sample = max(
                self.consumed_sample, int(command["router_consumed_sample"])
            )
            self.native_reset = True
            if self.native_mode == "delegate":
                self.native_mode = "listen"
            return True
        if command.get("native_resume"):
            if not self.continuous_decision or self.native_mode not in {
                "listen",
                "backend",
            }:
                self.native_reset = True
            self.native_mode = "listen"
            return True
        if command.get("start_response"):
            self.native_mode = "backend"
        if command.get("end_response"):
            if not self.continuous_decision or self.native_mode not in {
                "listen",
                "backend",
            }:
                self.native_reset = True
            self.native_mode = "listen"
        return False

    def set_media(self, packet):
        pcm = super().set_media(packet)
        if "microphone_pcm" not in packet:
            raise ValueError("Native routing requires separate microphone PCM")
        self.route_mic = np.frombuffer(
            base64.b64decode(packet["microphone_pcm"], validate=True), dtype="<f4"
        ).copy()
        if len(self.route_mic) != 16000 or not np.isfinite(self.route_mic).all():
            raise ValueError("Invalid decision microphone")
        self.route_sample = int(packet["metadata"]["microphone_end_sample"])
        self.route_has_mic = (
            packet["metadata"].get("clock_audio_kind") != "synthetic_silence"
        )
        self.route_frames = self.frames
        self.route_media_context = {
            key: packet["metadata"][key]
            for key in (
                "audio_end_ts_ms",
                "frame_capture_ts_ms",
                "frame_observation_id",
                "frame_sha256",
                "game_session_id",
                "scene_epoch",
            )
            if key in packet["metadata"]
        }
        if not self.speech_visual_context:
            self.frames = None
        return (
            self.route_mic.copy() if self.speech_audio_source == "microphone" else pcm
        )

    def _capture(self, embeds, return_logits=False):
        if embeds.ndim == 1:
            embeds = embeds.unsqueeze(0)
        self.route_blocks.append(embeds.detach().cpu())
        if return_logits:
            return (
                torch.zeros(
                    (1, self.model.config.vocab_size), device=self.model.device
                ),
                embeds[-1:].unsqueeze(0),
            )

    def _decision(self):
        d = self.route_duplex
        live = OmniBranchState.capture(self.duplex)
        feed = d.decoder.feed
        d.decoder.feed = self._capture
        try:
            with torch.inference_mode(), adapters(self.model, False):
                if self.native_reset:
                    self.route_blocks = []
                    self.model.reset_session()
                    prompt = decision_prompt(
                        self.route_prompt,
                        self.native_game,
                        self.route_prior_context if self.use_prior_context else None,
                        proactive=self.proactive,
                    )
                    d.prepare(prefix_system_prompt=prompt)
                    self.native_emit(
                        {
                            "type": "native.decision_context",
                            "audio_end_sample": self.route_sample,
                            "prompt_sha256": hashlib.sha256(
                                prompt.encode()
                            ).hexdigest(),
                            "game": self.native_game,
                            "proactive": self.proactive,
                            "prior_context_used": bool(
                                self.use_prior_context and self.route_prior_context
                            ),
                            "prefix_tokens": sum(
                                (len(block) for block in self.route_blocks)
                            ),
                        }
                    )
                    if self.route_window_units:
                        from jarvis.adapters.gates.omni_stream import CausalRouteWindow

                        self.route_window = CausalRouteWindow(
                            torch.cat(self.route_blocks), self.route_window_units
                        )
                        self.route_blocks = []
                    if getattr(self, "full_prefix_decisions", False):
                        from jarvis.adapters.gates.omni_stream import (
                            FullPrefixRouteSession,
                        )

                        self.router = FullPrefixRouteSession(
                            self.policy, max_tokens=self.route_capacity
                        )
                    elif self.proactive:
                        from jarvis.adapters.gates.omni_engagement_policy import (
                            EngagementSession,
                        )

                        self.router = EngagementSession(
                            self.policy, max_tokens=self.route_capacity
                        )
                    else:
                        self.router = NativeRouteSession(
                            self.policy, max_tokens=self.route_capacity
                        )
                    self.route_units = 0
                    self.native_reset = False
                else:
                    self.route_state.install(d)
                frames = (
                    self.route_frames
                    if self.route_units % self.frame_stride == 0
                    else None
                )
                result = d.streaming_prefill(
                    audio_waveform=self.route_mic, frame_list=frames
                )
                if not result["success"]:
                    raise RuntimeError("Native microphone prefill failed")
                terminator = d.decoder.embed_tokens(
                    [d.listen_token_id, d.tokenizer.convert_tokens_to_ids("</unit>")]
                )
                if self.route_window_units:
                    prefix = self.route_window.append(
                        torch.cat(self.route_blocks), terminator, self.route_sample
                    )
                    self.route_blocks = []
                    from jarvis.adapters.gates.omni_stream import FullPrefixRouteSession

                    self.router = FullPrefixRouteSession(
                        self.policy, max_tokens=self.route_capacity
                    )
                else:
                    prefix = torch.cat(self.route_blocks)
                    self._capture(terminator)
                self.route_units += 1
                self.route_state = OmniBranchState.capture(d)
        finally:
            d.decoder.feed = feed
            live.install(self.duplex)
        if len(prefix) > self.route_capacity:
            self.native_reset = True
            self.native_emit(
                {
                    "type": "native.context_reset",
                    "reason": "decision_capacity",
                    "audio_end_sample": self.route_sample,
                }
            )
            return [1.0, 0.0, 0.0, 0.0] if self.proactive else [1.0, 0.0, 0.0]
        return self.router.step(prefix, self.route_sample)[0].cpu().tolist()

    def transport_step_context(self, hold, start, response, epoch, backend):
        if backend or (start and self.native_mode == "backend"):
            self.native_mode = "backend"
        if (
            self.last_sample is not None
            and self.route_sample != self.last_sample + 16000
        ):
            self.native_reset = True
            self.native_emit(
                {
                    "type": "native.context_reset",
                    "reason": "microphone_gap",
                    "audio_end_sample": self.route_sample,
                }
            )
        self.last_sample = self.route_sample
        first = False
        listening = self.native_mode == "listen" or (
            self.continuous_decision and self.native_mode == "backend"
        )
        proactive = getattr(self, "proactive", False)
        has_input = self.route_has_mic or (proactive and bool(self.route_frames))
        if listening and has_input and (self.route_sample > self.consumed_sample):
            began = time.perf_counter()
            probs = self._decision()
            if getattr(self, "decision_diagnostics", False):
                self.native_emit(
                    {
                        "type": "native.decision_diagnostic",
                        "audio_end_sample": self.route_sample,
                        "probabilities": probs,
                        "media_context": getattr(self, "route_media_context", {}),
                        "decision_window_units": self.route_window_units,
                        "asr_admission": False,
                    }
                )
            native_action = int(np.argmax(probs))
            action = 2 if native_action == 3 else native_action
            if action and probs[native_action] >= self.threshold:
                interrupted = response if self.native_mode == "backend" else None
                if interrupted:
                    self.end_response()
                    backend.clear()
                self.native_wire = uuid4().hex
                self.native_mode = "direct" if action == 1 else "delegate"
                first = action == 1
                self.native_emit(
                    {
                        "type": "native.route",
                        "action": action,
                        "probabilities": probs,
                        "native_action_id": native_action,
                        "action_origin": "scene"
                        if native_action == 3
                        else "request"
                        if action == 2
                        else "speech",
                        "media_context": getattr(self, "route_media_context", {}),
                        "response_id": self.native_wire,
                        "audio_end_sample": self.route_sample,
                        "generation_epoch": epoch,
                        "decision_compute_ms": (time.perf_counter() - began) * 1000,
                        "interrupted_backend_response_id": interrupted,
                        "asr_admission": False,
                    }
                )
        if self.native_mode == "backend":
            return (hold, start, response, {})
        foreground = self.native_mode == "direct"
        return (
            not foreground,
            first,
            getattr(self, "native_wire", response),
            {
                "native_foreground": foreground,
                "native_audio_end_sample": self.route_sample,
            },
        )

    def step(self, *args, **kwargs):
        with adapters(self.model, False):
            result = super().step(*args, **kwargs)
        if self.native_mode == "direct" and result.get("end_of_turn"):
            self.native_mode = "listen"
            self.native_reset = True
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model-dir", default=str(model_path("venus-omni")))
    p.add_argument("--checkpoint", default=str(weight("omni")))
    p.add_argument("--engagement-policy", action="store_true")
    p.add_argument(
        "--full-prefix-decisions",
        action="store_true",
        help="Recompute the complete causal decision prefix without KV reuse",
    )
    p.add_argument("--port", type=int, default=8921)
    args = p.parse_args()
    import uvicorn

    uvicorn.run(
        create_app(
            args.model_dir,
            model_factory=lambda path: NativeVenusOmni(
                path,
                args.checkpoint,
                engagement_policy=args.engagement_policy,
                full_prefix_decisions=args.full_prefix_decisions,
            ),
            model_name="Realtime-Venus-Omni-native",
            transport_limits={"incoming": 200, "controls": 128, "outgoing": 128},
            aligned_keep_latest=True,
            owner_protocol="omni_native_route_v1",
        ),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
