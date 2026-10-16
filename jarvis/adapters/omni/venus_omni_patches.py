from __future__ import annotations
import hashlib
import inspect
import textwrap
from jarvis.adapters.omni.venus_patches import (
    TokenChunkEmitter,
    _install_tts_callback,
    _native_audio_callback,
    approved_token,
)
from jarvis.adapters.omni.venus_tts_conditioning import complete_text_condition
from jarvis.adapters.omni.venus_approved_batch import approved_batch

SOURCE_SHA256 = "e3c3cdee80976fb4473edc830301a0cefda62ad2b4f554a9182ea857549dc34c"


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise RuntimeError(
            "Omni source transformation does not match the audited release"
        )
    return source.replace(before, after, 1)


def close_explicit_turn(source):
    return replace_once(
        source,
        "        else:\n            last_id = self.decoder.decode(\n",
        '        elif end_of_turn and self.ls_mode == "explicit":\n            last_id = torch.tensor([self.chunk_eos_token_id], dtype=torch.long, device=self.device)\n        else:\n            last_id = self.decoder.decode(\n',
    )


def fp16_stream_cache(token2wav):
    import torch

    if not token2wav.float16 or getattr(token2wav, "_harness_fp16_cache", False):
        return
    original = token2wav.set_stream_cache

    def set_stream_cache(prompt_wav):
        with torch.amp.autocast("cuda", dtype=torch.float16):
            return original(prompt_wav)

    token2wav.set_stream_cache = set_stream_cache
    token2wav._harness_fp16_cache = True


def recent_answer_block(duplex, n=5):
    import json

    decoder = duplex.decoder
    original = decoder.decode
    state = {"table": {}, "start": 0}

    def arm(backend):
        for text in backend:
            if not text.startswith("<backend>") or "视觉记录：" not in text:
                continue
            try:
                record = json.loads(
                    text[text.index("视觉记录：") + 5 : text.rindex("</backend>")]
                )
            except ValueError:
                record = {}
            table = {}
            for answer in record.get("recently_said") or []:
                ids = duplex.tokenizer.encode(answer, add_special_tokens=False)
                for i in range(len(ids) - n + 1):
                    table.setdefault(tuple(ids[i : i + n - 1]), set()).add(
                        ids[i + n - 1]
                    )
            state.update(table=table, start=len(decoder.generated_tokens))

    def decode(logits, *args, **kwargs):
        current = decoder.generated_tokens[state["start"] :]
        banned = (
            state["table"].get(tuple(current[-(n - 1) :]))
            if len(current) >= n - 1
            else None
        )
        if banned:
            logits = logits.clone()
            logits[0, list(banned)] = float("-inf")
        return original(logits, *args, **kwargs)

    decoder.decode = decode
    duplex._harness_recent_answer_block = arm
    return {"recent_answer_ngram_block": n}


class _TTSGraph:
    def __init__(self, tts, max_len):
        import torch
        from transformers import StaticCache

        self.tts, self.max_len = (tts, max_len)
        dtype = tts.emb_text.weight.dtype
        self.cache = StaticCache(
            config=tts.model.config,
            max_batch_size=1,
            max_cache_len=max_len,
            device=tts.device,
            dtype=dtype,
        )
        self.inputs = torch.zeros(
            1, 1, tts.model.config.hidden_size, device=tts.device, dtype=dtype
        )
        self.positions = torch.zeros(1, 1, dtype=torch.long, device=tts.device)
        self.cache_positions = torch.zeros(1, dtype=torch.long, device=tts.device)
        self.graph = self.logits = None

    def forward(self, inputs, positions, cache_positions):
        import torch.nn.utils.parametrize as P

        hidden = self.tts.model(
            inputs_embeds=inputs,
            position_ids=positions,
            cache_position=cache_positions,
            past_key_values=self.cache,
            use_cache=True,
        ).last_hidden_state
        with P.cached():
            return self.tts.head_code[0](hidden[:, -1:])

    def step(self, inputs, position):
        import torch

        self.inputs.copy_(inputs)
        self.positions.fill_(position)
        self.cache_positions.fill_(position)
        if self.graph is None:
            stream = torch.cuda.Stream()
            stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(stream):
                for _ in range(2):
                    self.forward(self.inputs, self.positions, self.cache_positions)
            torch.cuda.current_stream().wait_stream(stream)
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph, capture_error_mode="thread_local"):
                logits = self.forward(self.inputs, self.positions, self.cache_positions)
            self.graph, self.logits = (graph, logits)
        self.graph.replay()
        return self.logits


def graphed_generate_chunk(
    self,
    inputs_embeds,
    temperature,
    repetition_penalty,
    eos_token,
    force_no_stop=False,
    max_new_token=500,
    min_new_tokens=0,
    past_key_values=None,
    logits_processors=None,
    text_start_pos=None,
    token_callback=None,
):
    import torch
    from torch.nn import functional as F

    runner = self._harness_tts_graph
    if past_key_values is not None and past_key_values is not runner.cache:
        raise ValueError("TTS KV of this turn was not produced by the graph decoder")
    _, processors = self._harness_gen_logits(
        num_code=self.config.num_audio_tokens, repetition_penalty=repetition_penalty
    )
    eos_token = eos_token.to(inputs_embeds.device)
    finish = torch.zeros(1, device=inputs_embeds.device).bool()
    temperature = (
        temperature.unsqueeze(0)
        .expand(1, -1)
        .contiguous()
        .view(-1, 1)
        .to(inputs_embeds.device)
    )
    condition_length = inputs_embeds.shape[1]
    if text_start_pos + condition_length + max_new_token >= runner.max_len:
        raise ValueError("TTS turn exceeds the static graph cache")
    new_tokens = torch.zeros(
        1, max_new_token, self.num_vq, device=inputs_embeds.device, dtype=torch.long
    )
    token_stream = TokenChunkEmitter(token_callback)
    for t in range(max_new_token):
        token_stream.update(new_tokens, t)
        if t == 0:
            positions = torch.arange(
                text_start_pos,
                text_start_pos + condition_length,
                dtype=torch.long,
                device=self.device,
            ).unsqueeze(0)
            logits = runner.forward(inputs_embeds, positions, positions[0])
        else:
            logits = runner.step(
                self.emb_code[0](new_tokens[:, t - 1 : t, 0]),
                text_start_pos + condition_length + t - 1,
            )
        logits = logits[:, -1].float().reshape(1, -1) / temperature
        if t:
            history = new_tokens[:, 0:t].permute(0, 2, 1).reshape(self.num_vq, -1)
            for processor in processors:
                logits = processor(history, logits)
        if force_no_stop or t < min_new_tokens:
            logits[:, eos_token] = -torch.inf
        next_token = torch.multinomial(F.softmax(logits, dim=-1), num_samples=1).view(
            -1, self.num_vq
        )
        finish.logical_or_(next_token.eq(eos_token).any(1))
        new_tokens[:, t] = next_token
        if t == 0 and finish.any():
            break
        if finish.all():
            break
    self._harness_last_decode = {
        "stop_reason": "eos" if bool(finish.all()) else "token_budget",
        "returned_tokens": int(t),
        "budget": int(max_new_token),
        "graph": True,
    }
    token_stream.update(new_tokens, t, final=True)
    return (new_tokens[:, 0:t, :], runner.cache)


def enable_tts_graph(tts, max_len=4096):
    import inspect as _inspect

    tts._harness_tts_graph = _TTSGraph(tts, max_len)
    tts._harness_gen_logits = _inspect.unwrap(type(tts).generate_chunk).__globals__[
        "gen_logits"
    ]
    tts.generate_chunk = graphed_generate_chunk.__get__(tts)
    return {"tts_cuda_graph": True, "tts_static_cache_tokens": max_len}


def install(duplex):
    cls = type(duplex)
    if getattr(cls, "_screen_patched", False):
        return cls._screen_patch_manifest
    with open(inspect.getsourcefile(cls), "rb") as handle:
        source_hash = hashlib.sha256(handle.read()).hexdigest()
    if source_hash != SOURCE_SHA256:
        raise RuntimeError(f"Unreviewed Omni source: {source_hash}")
    native_convert = cls._convert_results_to_tts_input

    def convert(self, results):
        return complete_text_condition(self, results, native_convert)

    cls._convert_results_to_tts_input = convert
    tts_hash = _install_tts_callback(duplex.model.tts)
    waveform = textwrap.dedent(inspect.getsource(cls._generate_waveform_from_tokens))
    waveform = replace_once(
        waveform,
        "    force_flush: bool = False,\n",
        "    force_flush: bool = False,\n    pad_short_audio: bool = True,\n",
    )
    waveform = replace_once(
        waveform,
        "if not is_last_chunk and len(audio_waveform) < min_samples:",
        "if pad_short_audio and not is_last_chunk and len(audio_waveform) < min_samples:",
    )
    namespace = {}
    exec(
        compile(waveform, "<audited Omni native vocoder callback>", "exec"),
        cls._generate_waveform_from_tokens.__globals__,
        namespace,
    )
    cls._generate_waveform_from_tokens = namespace["_generate_waveform_from_tokens"]
    source = textwrap.dedent(inspect.getsource(cls.streaming_generate))
    source = replace_once(
        source,
        "    text_repetition_window_size=512,\n",
        "    text_repetition_window_size=512,\n    force_listen=False,\n    start_response=False,\n    audio_callback=None,\n",
    )
    source = replace_once(
        source,
        "force_listen = self._streaming_generate_count < self.force_listen_count",
        "force_listen = force_listen or self._streaming_generate_count < self.force_listen_count",
    )
    source = replace_once(
        source,
        "    for j in range(max_new_speak_tokens_per_chunk):\n",
        "    approved_ids = None\n    if (not force_listen and self.ls_mode == 'explicit'\n            and getattr(self, '_harness_batch_approved', False)\n            and getattr(self, '_harness_approved_ids', None) is not None):\n        approved_ids, approved_logits, approved_hidden = _approved_batch(self, max_new_speak_tokens_per_chunk)\n    for j in range(max_new_speak_tokens_per_chunk):\n",
    )
    source = replace_once(
        source,
        "            last_id = torch.tensor([self.listen_token_id], dtype=torch.long, device=self.device)\n        else:\n",
        "            last_id = torch.tensor([self.listen_token_id], dtype=torch.long, device=self.device)\n        elif start_response and j == 0:\n            last_id = torch.tensor([self.speak_token_id], dtype=torch.long, device=self.device)\n        elif approved_ids is not None:\n            last_id = torch.tensor([approved_ids[j]], dtype=torch.long, device=self.device)\n        elif getattr(self, '_harness_approved_ids', None) is not None:\n            last_id = torch.tensor([_approved_token(self, j)], dtype=torch.long, device=self.device)\n        else:\n",
    )
    source = replace_once(
        source,
        "                logits, _ = self.decoder.feed(self.decoder.embed_token(last_id.item()), return_logits=True)\n",
        "                if approved_ids is not None:\n                    logits = approved_logits\n                else:\n                    logits, _ = self.decoder.feed(self.decoder.embed_token(last_id.item()), return_logits=True)\n",
    )
    source = replace_once(
        source,
        "            logits, hidden = self.decoder.feed(self.decoder.embed_token(last_id.item()), return_logits=True)\n",
        "            if approved_ids is not None:\n                logits, hidden = approved_logits, approved_hidden[:, j:j+1]\n            else:\n                logits, hidden = self.decoder.feed(self.decoder.embed_token(last_id.item()), return_logits=True)\n",
    )
    source = close_explicit_turn(source)
    source = replace_once(
        source,
        "if j != 0:",
        "if j != 0 or last_id.item() not in self.chunk_speak_token_ids:",
    )
    source = replace_once(
        source,
        "    llm_end_time = time.time()\n",
        "    llm_end_time = time.time()\n    candidate_callback = getattr(self, '_harness_text_candidate_callback', None)\n    if candidate_callback is not None and not is_listen:\n        candidate_callback(list(total_ids_in_unit), end_of_turn)\n",
    )
    source = replace_once(
        source,
        "    max_token_per_chunk = 25 + 1\n",
        "    max_token_per_chunk = 501 if getattr(self, '_harness_drain_units', False) else (156 if end_of_turn else 26)\n",
    )
    source = replace_once(
        source,
        "    if self.tts_current_turn_start_time is None:\n",
        "    if getattr(self, '_harness_drain_units', False):\n        min_token_per_chunk = 0\n\n    if self.tts_current_turn_start_time is None:\n",
    )
    source = replace_once(
        source,
        "    new_tokens, old_kv = self.model.tts.generate_chunk(\n",
        "    token_callback = None\n    if audio_callback is not None and (end_of_turn or getattr(self, '_harness_drain_units', False)):\n        token_callback = _native_audio_callback(self, audio_callback, prompt_wav_path, end_of_turn, force_flush)\n    new_tokens, old_kv = self.model.tts.generate_chunk(\n        token_callback=token_callback,\n",
    )
    source = replace_once(
        source,
        "    tts_end_time = time.time()\n",
        "    tts_end_time = time.time()\n    tts_decode = getattr(self.model.tts, '_harness_last_decode', {})\n    if getattr(self, '_harness_drain_units', False) and tts_decode.get('stop_reason') == 'token_budget':\n        self._harness_speech_incomplete = True\n",
    )
    source = replace_once(
        source,
        '        "n_tts_tokens": new_tokens.numel(),\n',
        '        "n_tts_tokens": new_tokens.numel(),\n        "tts_decode": tts_decode,\n        "audio_complete": not getattr(self, "_harness_speech_incomplete", False),\n',
    )
    source = replace_once(
        source,
        "    audio_waveform = self._generate_waveform_from_tokens(\n        new_tokens, prompt_wav_path, end_of_turn, force_flush=force_flush\n    )\n",
        "    audio_waveform = None\n    if token_callback is None:\n        audio_waveform = self._generate_waveform_from_tokens(\n            new_tokens, prompt_wav_path, end_of_turn, force_flush=force_flush\n        )\n",
    )
    namespace = {}
    exec(
        compile(source, "<audited Omni generation controls>", "exec"),
        {
            **inspect.unwrap(cls.streaming_generate).__globals__,
            "_approved_token": approved_token,
            "_native_audio_callback": _native_audio_callback,
            "_approved_batch": approved_batch,
        },
        namespace,
    )
    cls.streaming_generate = namespace["streaming_generate"]
    cls._screen_patched = True
    cls._screen_patch_manifest = {
        "source_sha256": source_hash,
        "generate_patch_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "waveform_patch_sha256": hashlib.sha256(waveform.encode()).hexdigest(),
        "tts_callback_sha256": tts_hash,
        "stream_helper_sha256": hashlib.sha256(
            (
                inspect.getsource(TokenChunkEmitter)
                + inspect.getsource(_native_audio_callback)
            ).encode()
        ).hexdigest(),
        "first_speech_chunk_protocol": "optional_short_initial_flush_v1",
        "speech_chunk_protocol": "optional_16_token_native_flush_v1",
        "zero_prefix_protocol": "optional_exact_zero_initial_160ms_v1",
        "weights_modified": False,
        "native_cadence_ms": 1000,
        "text_candidate_protocol": "native_text_before_tts_v1",
        "tts_text_end_protocol": "optional_native_text_eos_v1",
        "approved_batch_protocol": "optional_causal_approved_prefill_v1",
        "approved_batch_token_budget": 48,
        "turn_end_protocol": "explicit_turn_eos_forces_chunk_eos_v1",
        "approved_batch_sha256": hashlib.sha256(
            inspect.getsource(approved_batch).encode()
        ).hexdigest(),
        "tts_text_end_patch_sha256": hashlib.sha256(
            inspect.getsource(complete_text_condition).encode()
        ).hexdigest(),
        "changes": [
            "dynamic force listen",
            "explicit speak start",
            "native approved wording",
            "bounded final speech drain",
            "incremental native speech with epoch abort",
            "optional unreviewed text candidate before native TTS",
        ],
    }
    return cls._screen_patch_manifest
