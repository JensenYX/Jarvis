from jarvis.adapters.weights import load_weights
import json
import numpy as np, torch
from jarvis.adapters.gates.audio_policy import (
    attach_lora,
    adapters,
    RoutePolicy,
    PROMPT,
    GAME_CONTEXT,
)


class TrainedRouter:
    def __init__(self, model, checkpoint, rank=8, threshold=0.8, game_checkpoint=None):
        self.model = model
        self.threshold = threshold
        attach_lora(model, rank, rank * 2, ("q_proj", "v_proj", "o_proj"))
        self.profile_paths = {"general": str(checkpoint)}
        if game_checkpoint:
            self.profile_paths["game"] = str(game_checkpoint)
        self.profile_states = {
            name: load_weights(path) for name, path in self.profile_paths.items()
        }
        expected = {
            name for name, _ in model.named_parameters() if name.endswith((".a", ".b"))
        }
        for name, state in self.profile_states.items():
            if set(state) != expected:
                raise ValueError("Incomplete or non-adapter weights: " + name)
        self.routing_profile = None
        self.select_checkpoint("none")
        for p in model.parameters():
            p.requires_grad_(False)
        self.policy = None
        self.prefix = None
        self.units = []
        self.committed = False
        self.floor = 0
        self.pending_audio_floor = None
        self.committed_action = None
        self.committed_sample = None
        self.last_direct_rearm = None
        self.direct_output_seen = False
        self.empty_direct_ended = False
        self.empty_direct_retry_speech = None

    def select_checkpoint(self, game):
        profile = (
            "game"
            if game in {"pvz", "voxel"} and "game" in self.profile_paths
            else "general"
        )
        if profile != self.routing_profile:
            _, unexpected = self.model.load_state_dict(
                self.profile_states[profile], strict=False
            )
            if unexpected:
                raise ValueError("Unmatched adapter keys: " + str(unexpected))
            self.routing_profile = profile
            self.checkpoint = self.profile_paths[profile]
        return profile

    def prepare(self, duplex, ref_audio, game):
        self.select_checkpoint(game)
        self.d = duplex
        self.policy = RoutePolicy(self.model, duplex.tokenizer)
        self.units = []
        self.committed = False
        self.floor = 0
        self.pending_audio_floor = None
        self.committed_action = None
        self.committed_sample = None
        self.last_direct_rearm = None
        self.direct_output_seen = False
        self.empty_direct_ended = False
        self.empty_direct_retry_speech = None
        prompt = PROMPT + GAME_CONTEXT.get(game, "")
        t = duplex.tokenizer
        parts = []
        with adapters(self.model, False), torch.no_grad():
            ids = t.encode(
                "<|im_start|>system\n" + prompt + "\n<|audio_start|>",
                add_special_tokens=False,
            )
            parts.append(duplex.decoder.embed_tokens(ids).detach().cpu())
            self.prompt_token_count = len(ids)
            self.prompt_text = prompt
            record = duplex.processor.process_audio([ref_audio])
            emb = self.model.get_audio_embedding(
                record, chunk_length=self.model.config.audio_chunk_length
            )
            parts.extend((x.detach().cpu() for group in emb for x in group))
            parts.append(
                duplex.decoder.embed_tokens(
                    t.encode("<|audio_end|><|im_end|>", add_special_tokens=False)
                )
                .detach()
                .cpu()
            )
        self.prefix = torch.cat(parts)
        self.context_prefix = None
        from jarvis.adapters.native_acoustics import SpeechAudio

        self.speech_audio = SpeechAudio(self.model, duplex)
        self.raw_units = []
        self.speech_units = []
        self.speech_start = None
        self.speech_vad_sample = -1
        self.speech_end = None
        self.endpoint_observed_sample = None
        self.speech_segments = 0

    def set_context(self, context):
        rows = json.loads(context) if context else []
        text = ""
        if rows:
            text = (
                "\nCurrent tasks: "
                + "; ".join(
                    (
                        row["objective"][:100] + " (" + row["question"][:320] + ")"
                        for row in rows[-6:]
                    )
                )
                + ". Any amendment, budget change, rule confirmation, question about results, cancellation, or open/try request concerning these tasks belongs to the harness. General unrelated conversation stays direct."
            )
        with adapters(self.model, False), torch.no_grad():
            ids = self.d.tokenizer.encode(
                "<|im_start|>system\n" + self.prompt_text + text + "\n<|audio_start|>",
                add_special_tokens=False,
            )
            self.context_prefix = (
                torch.cat(
                    [
                        self.d.decoder.embed_tokens(ids).detach().cpu(),
                        self.prefix[self.prompt_token_count :],
                    ]
                )
                if text
                else None
            )

    def observe_unit(self, embeddings, end_sample):
        if not embeddings:
            return
        self.units.append(
            {"end": end_sample, "embeds": torch.cat(embeddings).detach().cpu()}
        )
        if self.pending_audio_floor is not None and self.d.current_turn_ended:
            self.consume(self.pending_audio_floor)
        self.rearm_finished_direct()

    def rearm_finished_direct(self):
        if (
            self.committed
            and getattr(self, "committed_action", None) == 1
            and self.d.current_turn_ended
            and (self.speech_vad_sample > self.committed_sample)
        ):
            previous = self.committed_sample
            self.consume(previous)
            self.last_direct_rearm = {
                "admitted_sample": previous,
                "new_speech_sample": self.speech_vad_sample,
            }

    def begin_speech(self, sample):
        if sample <= self.speech_vad_sample or sample <= self.floor:
            return
        join = (
            not self.committed
            and self.speech_start is not None
            and (self.speech_end is not None)
            and (self.speech_start >= self.floor)
            and (0 <= sample - self.speech_end <= 3 * 16000)
            and (sample - self.speech_start <= 24 * 16000)
        )
        self.speech_vad_sample = sample
        self.rearm_finished_direct()
        if not join:
            self.speech_start = max(self.floor, (sample // 16000 - 1) * 16000, 0)
        self.speech_segments = self.speech_segments + 1 if join else 1
        self.speech_end = None
        self.endpoint_observed_sample = None
        self.rebuild_speech_audio()

    def routing_pcm(self, pcm, end_sample):
        if self.speech_end is None:
            return pcm
        cut = self.speech_end + 4096 - (end_sample - len(pcm))
        if cut >= len(pcm):
            return pcm
        result = pcm.copy()
        result[max(0, cut) :] = 0
        return result

    def rebuild_speech_audio(self):
        self.speech_audio.reset()
        self.speech_units = []
        with adapters(self.model, False), torch.no_grad():
            for unit in self.raw_units:
                if unit["end"] > self.speech_start:
                    pcm = self.routing_pcm(unit["pcm"], unit["end"])
                    self.speech_units.append(
                        {"end": unit["end"], "embeds": self.speech_audio.feed(pcm)}
                    )

    def end_speech(self, sample, observed_sample):
        if (
            sample <= self.speech_vad_sample
            or sample <= self.floor
            or self.speech_start is None
        ):
            return
        if observed_sample < sample:
            raise ValueError("Endpoint precedes acoustic evidence")
        self.speech_end = sample
        self.endpoint_observed_sample = observed_sample
        if not self.committed:
            self.rebuild_speech_audio()

    def observe_pcm(self, pcm, end_sample):
        self.raw_units.append({"end": end_sample, "pcm": pcm.copy()})
        self.raw_units = self.raw_units[-32:]
        if self.speech_start is not None and end_sample > self.speech_start:
            with adapters(self.model, False), torch.no_grad():
                self.speech_units.append(
                    {
                        "end": end_sample,
                        "embeds": self.speech_audio.feed(
                            self.routing_pcm(pcm, end_sample)
                        ),
                    }
                )
            self.speech_units = self.speech_units[-32:]

    def consume(self, end_sample):
        if not self.committed:
            return
        if end_sample <= self.floor:
            return
        if not self.d.current_turn_ended:
            self.pending_audio_floor = max(self.pending_audio_floor or 0, end_sample)
            return
        self.floor = max(self.floor, end_sample)
        self.units = [u for u in self.units if u["end"] > self.floor]
        self.speech_units = [u for u in self.speech_units if u["end"] > self.floor]
        self.committed = False
        self.pending_audio_floor = None
        self.committed_action = None
        self.committed_sample = None

    def discard_stale_decision(self, decision):
        if (
            decision
            and decision.get("action") in (1, 2)
            and self.committed
            and (self.committed_action == decision["action"])
            and (self.committed_sample == decision["audio_end_sample"])
        ):
            self.committed = False
            self.committed_action = None
            self.committed_sample = None
            self.pending_audio_floor = None
            return True
        return False

    def observe_direct_result(self, *, public_text, has_audio, ended):
        if not self.committed or self.committed_action != 1:
            return
        self.direct_output_seen = (
            getattr(self, "direct_output_seen", False)
            or bool(public_text.strip())
            or has_audio
        )
        if ended:
            self.empty_direct_ended = not self.direct_output_seen

    def rearm_empty_direct(self, units):
        if not (
            self.committed
            and self.committed_action == 1
            and units
            and getattr(self, "empty_direct_ended", False)
            and (not getattr(self, "direct_output_seen", False))
            and self.d.current_turn_ended
            and (self.pending_audio_floor is None)
            and (self.speech_end is not None)
            and (self.speech_end > self.committed_sample)
            and (self.endpoint_observed_sample is not None)
            and (units[-1]["end"] >= self.endpoint_observed_sample)
            and (
                getattr(self, "empty_direct_retry_speech", None)
                != self.speech_vad_sample
            )
        ):
            return
        self.empty_direct_retry_speech = self.speech_vad_sample
        self.committed = False
        self.committed_action = None
        self.committed_sample = None
        self.empty_direct_ended = False

    def choose(self):
        units = self.speech_units if self.speech_start is not None else self.units
        units = [unit for unit in units if unit["end"] > self.floor]
        self.rearm_empty_direct(units)
        if self.committed or not units:
            return None
        d = self.d
        with adapters(self.model, False), torch.no_grad():
            tail = (
                d.decoder.embed_tokens(
                    [d.listen_token_id, d.tokenizer.convert_tokens_to_ids("</unit>")]
                )
                .detach()
                .cpu()
            )
        parts = [self.prefix]
        for i, u in enumerate(units):
            parts.append(u["embeds"])
            if i < len(units) - 1:
                parts.append(tail)
        if sum((len(x) for x in parts)) > 4096:
            self.units = self.units[-30:]
            return None
        with adapters(self.model, True), torch.no_grad():
            lp, _ = self.policy.forward([torch.cat(parts)])
        probs = lp[0].exp().cpu().tolist()
        context_probs = None
        context_listen_wait = False
        context_delegate_wait = False
        if self.context_prefix is not None:
            with adapters(self.model, True), torch.no_grad():
                context_lp, _ = self.policy.forward(
                    [torch.cat([self.context_prefix, *parts[1:]])]
                )
            context_probs = context_lp[0].exp().cpu().tolist()
            context_action = int(np.argmax(context_probs))
            if context_probs[2] >= self.threshold or (
                int(np.argmax(probs)) == 0
                and context_action
                and (context_probs[context_action] >= self.threshold)
            ):
                probs = context_probs
            elif (
                int(np.argmax(probs)) == 1
                and probs[1] >= self.threshold
                and (context_action == 0)
                and (context_probs[0] >= self.threshold)
                and (self.speech_start is not None)
            ):
                probs = context_probs
                context_listen_wait = True
            elif (
                int(np.argmax(probs)) == 1
                and probs[1] >= self.threshold
                and (context_action == 2)
                and (context_probs[2] < self.threshold)
                and (self.speech_start is not None)
            ):
                probs = context_probs
                context_delegate_wait = True
        action = int(np.argmax(probs))
        if action and probs[action] < self.threshold:
            action = 0
        if action:
            self.committed = True
            self.committed_action = action
            self.committed_sample = units[-1]["end"]
            self.direct_output_seen = False
            self.empty_direct_ended = False
        return {
            "action": action,
            "probabilities": probs,
            "audio_end_sample": units[-1]["end"],
            "checkpoint": self.checkpoint,
            "asr_used_for_decision": False,
            "context_probabilities": context_probs,
            "context_listen_wait": context_listen_wait,
            "context_delegate_wait": context_delegate_wait,
            "acoustic_context": "speech_segment"
            if self.speech_start is not None
            else "session",
            "speech_start_sample": self.speech_start,
            "speech_end_sample": self.speech_end,
            "endpoint_observed_sample": self.endpoint_observed_sample,
            "speech_segments": self.speech_segments,
            "direct_rearm": getattr(self, "last_direct_rearm", None),
            "routing_profile": getattr(self, "routing_profile", "general"),
            "post_speech_ambience_gated": self.speech_end is not None,
        }

    def delegate(self):
        d = self.d
        tokens = d.tokenizer.encode(
            "<|speak|><delegate></delegate><|turn_eos|><|chunk_eos|></unit>",
            add_special_tokens=False,
        )
        with adapters(self.model, False), torch.no_grad():
            d.decoder.feed(d.decoder.embed_tokens(tokens))
        d.total_ids.extend(tokens)
        d.current_turn_ended = True
        return {
            "is_listen": False,
            "text": "",
            "end_of_turn": True,
            "audio_waveform": None,
            "delegates": ["__native_delegate__"],
            "n_tokens": len(tokens),
            "n_tts_tokens": 0,
            "trained_delegate": True,
        }
