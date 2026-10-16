from __future__ import annotations
import hashlib
import inspect
import textwrap

SOURCE_SHA256 = "b7b4070cead9b88347f6e39d8333706ec007e2a832d6bcde5755063bf1453774"


class SpeechAborted(Exception):
    pass


def approved_token(duplex, index):
    if index == 0:
        duplex._harness_approved_unit_ids = []
        duplex._harness_approved_chunk_finished = False
        return duplex.speak_token_id
    if duplex._harness_approved_ids:
        unit = getattr(duplex, "_harness_approved_unit_ids", [])
        if (
            getattr(duplex, "_harness_clean_speech", False)
            and getattr(duplex, "_harness_segment_approved", True)
            and (len(unit) >= 24)
        ):
            import re

            previous = duplex.tokenizer.decode(unit, skip_special_tokens=True)
            following = duplex.tokenizer.decode(
                [duplex._harness_approved_ids[0]], skip_special_tokens=True
            )
            sentence = bool(re.search('[.!?。！？]["\\u201d\\u2019]*$', previous))
            if (sentence or len(unit) >= 48) and (
                following[:1].isspace() or (sentence and previous[-1:] in "。！？")
            ):
                duplex._harness_approved_chunk_finished = True
                return duplex.chunk_eos_token_id
        token = duplex._harness_approved_ids.popleft()
        unit.append(token)
        duplex._harness_approved_unit_ids = unit
        return token
    if not duplex._harness_approved_ended:
        duplex._harness_approved_ended = True
        return duplex.tokenizer.convert_tokens_to_ids("<|turn_eos|>")
    duplex._harness_approved_ids = None
    return duplex.chunk_eos_token_id


class TokenChunkEmitter:
    def __init__(self, callback, chunk_size=25):
        self.callback, self.chunk_size = (callback, chunk_size)
        self.offset, self.finished = (0, False)

    def update(self, tokens, available, *, final=False):
        if (
            self.callback is not None
            and getattr(self.callback, "abort_requested", lambda: False)()
        ):
            raise SpeechAborted()
        if self.finished or not self.offset <= available <= tokens.shape[1]:
            raise ValueError("Non-monotonic native TTS token stream")
        if self.callback is not None:
            while available - self.offset >= self.chunk_size:
                stop = self.offset + self.chunk_size
                self.callback(tokens[:, self.offset : stop, :], False)
                self.offset = stop
            if final:
                self.callback(tokens[:, self.offset : available, :], True)
        if final:
            self.offset, self.finished = (available, True)


def _install_tts_callback(tts):
    cls = type(tts)
    if getattr(cls, "_harness_tts_callback_sha256", None):
        return cls._harness_tts_callback_sha256
    source = textwrap.dedent(inspect.getsource(cls.generate_chunk))
    marker = "    text_start_pos=None,\n"
    assert source.count(marker) == 1
    source = source.replace(marker, marker + "    token_callback=None,\n")
    marker = "    for t in range(max_new_token):\n"
    assert source.count(marker) == 1
    source = source.replace(
        marker,
        "    token_stream = _TokenChunkEmitter(token_callback)\n"
        + marker
        + "        token_stream.update(new_tokens, t)\n",
    )
    marker = "    return genrated_input_ids, past_key_values\n"
    assert source.count(marker) == 1
    source = source.replace(
        marker,
        "    self._harness_last_decode = {\n        'stop_reason': 'eos' if bool(finish.all()) else 'token_budget',\n        'returned_tokens': int(t), 'budget': int(max_new_token),\n    }\n    token_stream.update(new_tokens, t, final=True)\n"
        + marker,
    )
    namespace = {}
    globals_ = {
        **inspect.unwrap(cls.generate_chunk).__globals__,
        "_TokenChunkEmitter": TokenChunkEmitter,
    }
    exec(
        compile(source, "<reviewed Venus native TTS callback>", "exec"),
        globals_,
        namespace,
    )
    patched_generate = namespace["generate_chunk"]
    import functools

    @functools.wraps(patched_generate)
    def stable_chunk(self, *args, **kwargs):
        seed = getattr(self, "_harness_seed", None)
        if seed is None:
            return patched_generate(self, *args, **kwargs)
        import torch

        with torch.random.fork_rng(devices=[torch.device(self.device)]):
            torch.manual_seed(int(seed))
            return patched_generate(self, *args, **kwargs)

    cls.generate_chunk = stable_chunk
    cls._harness_tts_callback_sha256 = hashlib.sha256(source.encode()).hexdigest()
    return cls._harness_tts_callback_sha256


def _native_audio_callback(duplex, callback, prompt_wav_path, end_of_turn, force_flush):
    import torch

    emitted = False

    def consume(tokens, final):
        nonlocal emitted
        device = torch.device(duplex.device)
        with torch.random.fork_rng(devices=[device] if device.type == "cuda" else []):
            waveform = duplex._generate_waveform_from_tokens(
                tokens,
                prompt_wav_path,
                is_last_chunk=final and end_of_turn,
                force_flush=force_flush
                and (
                    final
                    or (
                        getattr(duplex, "_harness_drain_units", False) and (not emitted)
                    )
                ),
                pad_short_audio=not end_of_turn
                and (not getattr(duplex, "_harness_drain_units", False)),
            )
        if waveform is not None and len(waveform):
            emitted = True
            callback(waveform)

    consume.abort_requested = getattr(callback, "abort_requested", lambda: False)
    return consume


def filter_delegate_pairs(duplex, results):
    open_id = duplex.tokenizer.convert_tokens_to_ids("<delegate>")
    close_id = duplex.tokenizer.convert_tokens_to_ids("</delegate>")
    skip = getattr(duplex, "_delegate_tts_skip", False)
    kept = []
    saw_delegate = skip
    for result in results:
        token = result[0]
        if token == open_id:
            skip = True
            saw_delegate = True
        elif token == close_id:
            skip = False
            saw_delegate = True
        elif not skip:
            kept.append(result)
    if any((r[2] for r in results)):
        skip = False
    duplex._delegate_tts_skip = skip
    if saw_delegate and (
        not any((r[0] not in duplex.tokenizer.all_special_ids for r in kept))
    ):
        return None
    return kept


def _muted_result(
    self,
    prompt_wav_path,
    end_of_turn,
    text,
    current_time,
    start_time,
    llm_start_time,
    llm_end_time,
    count,
):
    import time
    import torch

    waveform = None
    if self.tts_past_key_values is not None:
        bos = self.model.tts.emb_text(
            torch.tensor(
                [self.model.tts.audio_bos_token_id],
                device=self.device,
                dtype=torch.long,
            )
        ).unsqueeze(0)
        tokens, _ = self.model.tts.generate_chunk(
            inputs_embeds=bos,
            temperature=self.tts_temperature,
            repetition_penalty=self.tts_repetition_penalty,
            eos_token=self.tts_eos_token,
            force_no_stop=False,
            max_new_token=26,
            min_new_tokens=0,
            past_key_values=self.tts_past_key_values,
            logits_processors=self.tts_logits_processors,
            text_start_pos=self.tts_text_start_pos,
        )
        waveform = self._generate_waveform_from_tokens(
            tokens, prompt_wav_path, True, force_flush=True
        )
        self.tts_text_start_pos = 0
        self.tts_past_key_values = None
        self.tts_current_turn_start_time = None
        self._reset_token2wav_for_new_turn()
        self._delegate_turn_preflushed = not end_of_turn
    elif end_of_turn:
        self._delegate_turn_preflushed = False
    return {
        "is_listen": False,
        "text": text,
        "audio_waveform": waveform,
        "end_of_turn": end_of_turn,
        "current_time": current_time,
        "cost_llm": llm_end_time - llm_start_time,
        "cost_tts_prep": 0.0,
        "cost_tts": 0.0,
        "cost_token2wav": 0.0,
        "cost_all": time.time() - start_time,
        "n_tokens": count,
        "n_tts_tokens": 0,
        "delegate_muted": True,
    }


def install(duplex) -> dict:
    cls = type(duplex)
    if getattr(cls, "_harness_patched", False):
        return cls._harness_patch_manifest
    source_file = inspect.getsourcefile(cls)
    with open(source_file, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    if digest != SOURCE_SHA256:
        raise RuntimeError(
            f"Unreviewed Audio inference source {digest}; review before patching"
        )
    tts_callback_sha = _install_tts_callback(duplex.model.tts)
    waveform_source = textwrap.dedent(
        inspect.getsource(cls._generate_waveform_from_tokens)
    )
    marker = "    force_flush: bool = False,\n"
    assert waveform_source.count(marker) == 1
    waveform_source = waveform_source.replace(
        marker, marker + "    pad_short_audio: bool = True,\n"
    )
    marker = "if not is_last_chunk and len(audio_waveform) < min_samples:"
    assert waveform_source.count(marker) == 1
    waveform_source = waveform_source.replace(
        marker, "if pad_short_audio and " + marker[3:]
    )
    namespace = {}
    exec(
        compile(waveform_source, "<reviewed Venus native vocoder callback>", "exec"),
        cls._generate_waveform_from_tokens.__globals__,
        namespace,
    )
    cls._generate_waveform_from_tokens = namespace["_generate_waveform_from_tokens"]
    original_convert = cls._convert_results_to_tts_input

    def filtered(self, results):
        if getattr(self, "_native_autonomy", False):
            from jarvis.adapters.native_protocol import NativeParser

            parser = getattr(self, "_native_tts_parser", None)
            if parser is None:
                parser = self._native_tts_parser = NativeParser(self.tokenizer)
            kept = []
            for row in results:
                text, _ = parser.feed([row[0]])
                if text and (
                    text.strip() or getattr(self, "_native_tts_public_in_turn", False)
                ):
                    kept.append(row)
                    self._native_tts_public_in_turn = True
                elif (
                    row[2]
                    and getattr(self, "_native_tts_public_in_turn", False)
                    and (not parser.inside)
                    and (not parser.hidden)
                ):
                    kept.append(row)
                if row[2]:
                    self._native_tts_public_in_turn = False
            filtered_results = kept or None
        else:
            filtered_results = filter_delegate_pairs(self, results)
        if filtered_results is None:
            return None
        if getattr(self, "_harness_clean_speech", False):
            import re

            public = self.tokenizer.decode(
                [r[0] for r in filtered_results], skip_special_tokens=True
            ).strip()
            if not public and self.tts_past_key_values is None:
                return None
            self._harness_sentence_eos = bool(
                getattr(self, "_harness_approved_chunk_finished", False)
                or re.search('[.!?。！？][\\"\\”\\’]*$', public)
            )
        if getattr(self, "_harness_clean_speech", False):
            import torch

            ids = [
                row[0]
                for row in filtered_results
                if row[0] not in self.tokenizer.all_special_ids
            ]
            finished = any((row[2] for row in filtered_results)) or getattr(
                self, "_harness_sentence_eos", False
            )
            if ids:
                language = (
                    "Chinese"
                    if getattr(self, "_harness_speech_language", "en") == "zh"
                    else "English"
                )
                prefix = self.tokenizer.encode(
                    "<|im_start|>system\nStreaming Omni Conversation. Speak clearly in "
                    + language
                    + ".<|im_end|><unit><|speak|>",
                    add_special_tokens=False,
                )
                token_ids = torch.tensor([prefix + ids], device=self.device)
                h = self.model.llm.model(
                    input_ids=token_ids, use_cache=False
                ).last_hidden_state[:, -len(ids) :, :]
                hidden = torch.nn.functional.normalize(
                    self.model.tts.projector_semantic(h), p=2, dim=-1
                )
                embeds = self.model.tts.emb_text(token_ids[:, -len(ids) :]) + hidden
            else:
                embeds = self.model.tts.emb_text.weight.new_empty(
                    (1, 0, self.model.tts.config.hidden_size)
                )
            tail = ([self.model.tts.config.text_eos_token_id] if finished else []) + [
                self.model.tts.audio_bos_token_id
            ]
            ending = self.model.tts.emb_text(torch.tensor([tail], device=self.device))
            return torch.cat([embeds, ending], dim=1)
        if getattr(self, "_harness_text_eos", False) and any(
            (row[2] for row in filtered_results)
        ):
            import torch

            content = [row for row in filtered_results if not row[2]]
            embeds = original_convert(self, content)
            eos = self.model.tts.emb_text(
                torch.tensor(
                    [self.model.tts.config.text_eos_token_id],
                    device=self.device,
                    dtype=torch.long,
                )
            ).unsqueeze(0)
            return torch.cat([embeds[:, :-1], eos, embeds[:, -1:]], dim=1)
        return original_convert(self, filtered_results)

    generate = textwrap.dedent(inspect.getsource(cls.streaming_generate))
    marker = "    text_repetition_window_size=512,\n"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        marker
        + "    force_listen=False,\n    start_response=False,\n    audio_callback=None,\n",
    )
    marker = "force_listen = self._streaming_generate_count < self.force_listen_count"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        "force_listen = force_listen or self._streaming_generate_count < self.force_listen_count",
    )
    marker = "            last_id = torch.tensor([self.listen_token_id], dtype=torch.long, device=self.device)\n        else:\n"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        marker.replace(
            "        else:\n",
            "        elif start_response and j == 0:\n            # A held question may no longer trigger native turn-taking after\n            # several silent units. Explicit route approval opens one new turn;\n            # all answer tokens and speech are still generated by Venus.\n            last_id = torch.tensor([self.speak_token_id], dtype=torch.long, device=self.device)\n        elif getattr(self, '_harness_approved_ids', None) is not None:\n            last_id = torch.tensor([_approved_token(self, j)], dtype=torch.long, device=self.device)\n        else:\n",
        ),
    )
    generate = generate.replace(
        "if j != 0:", "if j != 0 or last_id.item() not in self.chunk_speak_token_ids:"
    )
    marker = "    tts_prep_end_time = time.time()\n"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        "    if tts_condition is None or (\n        end_of_turn and not generated_text.strip()\n        and getattr(self, '_delegate_turn_preflushed', False)\n    ):\n        return self._harness_muted_result(\n            prompt_wav_path, end_of_turn, generated_text, current_time,\n            start_time, llm_start_time, llm_end_time, len(total_ids_in_unit),\n        )\n    self._delegate_turn_preflushed = False\n"
        + marker,
    )
    marker = "    tts_prep_end_time = time.time()\n"
    generate = generate.replace(
        marker,
        marker
        + "    protocol_end_of_turn = end_of_turn\n    end_of_turn = end_of_turn or bool(getattr(self, '_harness_sentence_eos', False))\n",
    )
    last_return = generate.rfind('"end_of_turn": end_of_turn,')
    assert last_return >= 0
    generate = generate[:last_return] + generate[last_return:].replace(
        '"end_of_turn": end_of_turn,', '"end_of_turn": protocol_end_of_turn,', 1
    )
    marker = "    max_token_per_chunk = 25 + 1\n"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        "    max_token_per_chunk = 501 if getattr(self, '_harness_drain_units', False) else (156 if end_of_turn else 26)\n",
    )
    marker = "    if self.tts_current_turn_start_time is None:\n"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        "    if getattr(self, '_harness_drain_units', False):\n        min_token_per_chunk = 0\n\n"
        + marker,
    )
    marker = "    new_tokens, old_kv = self.model.tts.generate_chunk(\n"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        "    token_callback = None\n    if audio_callback is not None and (end_of_turn or getattr(self, '_harness_drain_units', False)):\n        token_callback = _native_audio_callback(\n            self, audio_callback, prompt_wav_path, end_of_turn, force_flush,\n        )\n"
        + marker
        + "        token_callback=token_callback,\n",
    )
    marker = "    tts_end_time = time.time()\n"
    generate = generate.replace(
        marker,
        marker
        + "    tts_decode = getattr(self.model.tts, '_harness_last_decode', {})\n    if getattr(self, '_harness_drain_units', False) and tts_decode.get('stop_reason') == 'token_budget':\n        self._harness_speech_incomplete = True\n",
    )
    marker = '        "n_tts_tokens": new_tokens.numel(),\n'
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        marker
        + '        "tts_decode": tts_decode,\n        "audio_complete": not getattr(self, \'_harness_speech_incomplete\', False),\n',
    )
    marker = "    audio_waveform = self._generate_waveform_from_tokens(\n        new_tokens, prompt_wav_path, end_of_turn, force_flush=force_flush\n    )\n"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        "    audio_waveform = None\n    if token_callback is None:\n        audio_waveform = self._generate_waveform_from_tokens(\n            new_tokens, prompt_wav_path, end_of_turn, force_flush=force_flush\n        )\n",
    )
    generate = generate.replace(
        "if last_id.item() == self.listen_token_id and (not self.current_turn_ended):",
        'if last_id.item() == self.listen_token_id and (not self.current_turn_ended) and not (getattr(self,"_native_allow_interrupt",False) and j == 0 and not getattr(getattr(self,"_native_tts_parser",None),"inside",False)):',
    )
    generate = generate.replace(
        "                logits=logits,",
        '                logits=_native_mask(self,logits) if getattr(self,"_native_grammar",False) else logits,',
    )
    marker = "        self.total_ids.append(last_id.item())\n"
    assert generate.count(marker) == 1
    generate = generate.replace(
        marker,
        marker
        + '        if getattr(self,"_native_grammar",False):_native_observe(self,last_id.item())\n',
    )
    from jarvis.adapters.native_grammar import (
        mask as native_mask,
        observe as native_observe,
    )

    namespace = {}
    exec(
        compile(generate, "<reviewed Venus Audio inference patch>", "exec"),
        {
            **inspect.unwrap(cls.streaming_generate).__globals__,
            "_native_audio_callback": _native_audio_callback,
            "_approved_token": approved_token,
            "_native_mask": native_mask,
            "_native_observe": native_observe,
        },
        namespace,
    )
    cls.streaming_generate = namespace["streaming_generate"]
    cls._convert_results_to_tts_input = filtered
    cls._harness_muted_result = _muted_result
    cls._harness_patched = True
    cls._harness_patch_manifest = {
        "source_sha256": digest,
        "generate_patch_sha256": hashlib.sha256(generate.encode()).hexdigest(),
        "tts_callback_sha256": tts_callback_sha,
        "waveform_patch_sha256": hashlib.sha256(waveform_source.encode()).hexdigest(),
        "stream_helper_sha256": hashlib.sha256(
            (
                inspect.getsource(TokenChunkEmitter)
                + inspect.getsource(_native_audio_callback)
            ).encode()
        ).hexdigest(),
        "abort_patch_sha256": hashlib.sha256(
            inspect.getsource(end_response).encode()
        ).hexdigest(),
        "delegate_patch_sha256": hashlib.sha256(
            (
                inspect.getsource(filter_delegate_pairs)
                + inspect.getsource(_muted_result)
            ).encode()
        ).hexdigest(),
        "changes": [
            "dynamic force_listen",
            "delegate token/hidden-state masking",
            "fully muted chunk flush",
            "turn-local abort preserving conversation KV",
            "one-shot speak token on explicit response approval",
            "bounded native final TTS drain (156 tokens)",
            "incremental final TTS PCM with isolated decoder RNG and one vocoder flush",
            "opt-in per-conditioning TTS drain with incremental PCM, EOS/budget telemetry and epoch abort",
            "opt-in verified backend wording through native LLM hidden states and native TTS",
        ],
        "native_protocol": {
            "opt_in": True,
            "grammar": "protocol-only; one sentence delegate closes on generated punctuation",
            "listen_interrupt": "allow model listen choice at unit start outside private spans",
        },
        "weights_modified": False,
    }
    return cls._harness_patch_manifest


def end_response(duplex):
    duplex._native_tts_parser = None
    duplex._harness_speech_incomplete = False
    duplex._harness_approved_ids = None
    duplex._harness_approved_ended = False
    if not duplex.current_turn_ended:
        tokens = duplex.tokenizer.encode(
            "<unit><|speak|><|turn_eos|><|chunk_eos|></unit>", add_special_tokens=False
        )
        duplex.decoder.register_unit_start()
        for token in tokens:
            duplex.decoder.feed(duplex.decoder.embed_token(token))
        duplex.prefill_schema_tokens.append([tokens[0]])
        duplex.total_ids.extend(tokens[1:])
        duplex.decoder.register_unit_end(
            input_type="text",
            generated_tokens=tokens[1:],
            is_listen=False,
            generated_text="",
        )
    duplex.current_turn_ended = True
    duplex.tts_text_start_pos = 0
    duplex.tts_past_key_values = None
    duplex.tts_current_turn_start_time = None
    duplex._delegate_tts_skip = False
    duplex._delegate_turn_preflushed = False
    duplex._reset_token2wav_for_new_turn()
