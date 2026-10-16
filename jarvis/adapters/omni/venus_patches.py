from __future__ import annotations
import hashlib
import inspect
import textwrap

SOURCE_SHA256 = "b7b4070cead9b88347f6e39d8333706ec007e2a832d6bcde5755063bf1453774"


class SpeechAborted(Exception):
    pass


def approved_token(duplex, index):
    if index == 0:
        return duplex.speak_token_id
    if duplex._harness_approved_ids:
        return duplex._harness_approved_ids.popleft()
    if not duplex._harness_approved_ended:
        duplex._harness_approved_ended = True
        return duplex.tokenizer.convert_tokens_to_ids("<|turn_eos|>")
    duplex._harness_approved_ids = None
    return duplex.chunk_eos_token_id


class TokenChunkEmitter:
    def __init__(self, callback, chunk_size=25):
        self.callback = callback
        self.chunk_size = getattr(callback, "chunk_size", chunk_size)
        self.first_chunk_size = getattr(callback, "first_chunk_size", self.chunk_size)
        if not 1 <= self.first_chunk_size <= self.chunk_size:
            raise ValueError("Invalid first native TTS chunk size")
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
            size = self.first_chunk_size if self.offset == 0 else self.chunk_size
            while available - self.offset >= size:
                stop = self.offset + size
                self.callback(tokens[:, self.offset : stop, :], False)
                self.offset = stop
                size = self.chunk_size
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
    cls.generate_chunk = namespace["generate_chunk"]
    cls._harness_tts_callback_sha256 = hashlib.sha256(source.encode()).hexdigest()
    return cls._harness_tts_callback_sha256


def _native_audio_callback(duplex, callback, prompt_wav_path, end_of_turn, force_flush):
    import torch

    emitted = False
    drain = getattr(duplex, "_harness_drain_units", False)
    chunk_size = getattr(duplex, "_harness_speech_chunk_tokens", 25) if drain else 25

    def consume(tokens, final):
        nonlocal emitted
        device = torch.device(duplex.device)
        with torch.random.fork_rng(devices=[device] if device.type == "cuda" else []):
            waveform = duplex._generate_waveform_from_tokens(
                tokens,
                prompt_wav_path,
                is_last_chunk=final and end_of_turn,
                force_flush=chunk_size < 25
                or (force_flush and (final or (drain and (not emitted)))),
                pad_short_audio=not end_of_turn
                and (not getattr(duplex, "_harness_drain_units", False)),
            )
        if waveform is not None and len(waveform):
            emitted = True
            callback(waveform)

    consume.abort_requested = getattr(callback, "abort_requested", lambda: False)
    consume.chunk_size = chunk_size
    consume.first_chunk_size = (
        getattr(duplex, "_harness_speech_first_chunk_tokens", 25)
        if force_flush and drain
        else chunk_size
    )
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
        filtered_results = filter_delegate_pairs(self, results)
        if filtered_results is None:
            return None
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
    namespace = {}
    exec(
        compile(generate, "<reviewed Venus Audio inference patch>", "exec"),
        {
            **inspect.unwrap(cls.streaming_generate).__globals__,
            "_native_audio_callback": _native_audio_callback,
            "_approved_token": approved_token,
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
        "weights_modified": False,
    }
    return cls._harness_patch_manifest


def end_response(duplex):
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
