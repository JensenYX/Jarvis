import base64, json, re, urllib.request
from difflib import SequenceMatcher
import numpy as np


def normalize(s):
    s = (
        s.lower()
        .replace("forty dollars", "40")
        .replace("sixty dollars", "60")
        .replace("forty", "40")
        .replace("sixty", "60")
        .replace("twenty-five", "25")
        .replace("twenty five", "25")
    )
    return re.sub("[^a-z0-9]", "", s)


def heard(wave):
    from scipy.signal import resample_poly

    pcm = resample_poly(wave, 2, 3).astype("<f4")
    req = urllib.request.Request(
        "http://127.0.0.1:8768/transcribe",
        data=json.dumps(
            {"pcm": base64.b64encode(pcm.tobytes()).decode(), "language": "en"}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=12))["text"]


def matches(expected, observed):
    a, b = (normalize(expected), normalize(observed))
    if a == b:
        return True
    if not a or not b:
        return False
    words = re.findall("[a-z]+", expected.lower())
    tail = normalize(" ".join(words[-2:]))
    return (
        SequenceMatcher(None, a, b).ratio() >= 0.9
        and bool(tail)
        and b.endswith(tail)
        and (len(b) >= len(a) * 0.9)
    )


def synthesize(owner, text, attempt):
    import torch
    from jarvis.adapters.venus_patches import _native_audio_callback

    d = owner.duplex
    t = d.model.tts
    ids = d.tokenizer.encode(text, add_special_tokens=False)
    prefix = d.tokenizer.encode(
        "<|im_start|>system\nStreaming Omni Conversation. Speak clearly in English.<|im_end|><unit><|speak|>",
        add_special_tokens=False,
    )
    chunks = []
    with (
        torch.random.fork_rng(devices=[torch.device(d.device)]),
        torch.inference_mode(),
    ):
        torch.manual_seed(1729 + attempt * 101)
        d._reset_token2wav_for_new_turn()
        token_ids = torch.tensor([prefix + ids], device=d.device)
        h = owner.model.llm.model(
            input_ids=token_ids, use_cache=False
        ).last_hidden_state[:, -len(ids) :, :]
        proj = torch.nn.functional.normalize(t.projector_semantic(h), p=2, dim=-1)
        emb = t.emb_text(token_ids[:, -len(ids) :]) + proj
        ending = t.emb_text(
            torch.tensor(
                [[t.config.text_eos_token_id, t.audio_bos_token_id]], device=d.device
            )
        )
        emb = torch.cat([emb, ending], dim=1)
        callback = _native_audio_callback(
            d, lambda w: chunks.append(w.copy()), None, True, True
        )
        t.generate_chunk(
            inputs_embeds=emb,
            temperature=torch.tensor([0.8], device=d.device),
            repetition_penalty=1.05,
            eos_token=d.tts_eos_token,
            max_new_token=501,
            min_new_tokens=0,
            past_key_values=None,
            text_start_pos=0,
            token_callback=callback,
        )
        d._reset_token2wav_for_new_turn()
    return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)


def verify(owner, text, wave, abort):
    tries = []
    best = wave
    score = -1
    for attempt in range(5):
        if abort and abort():
            from jarvis.adapters.venus_patches import SpeechAborted

            raise SpeechAborted()
        if attempt:
            wave = synthesize(owner, text, attempt)
        try:
            transcript = heard(wave)
        except Exception as ex:
            tries.append({"attempt": attempt, "error": type(ex).__name__})
            continue
        ok = matches(text, transcript)
        ratio = SequenceMatcher(None, normalize(text), normalize(transcript)).ratio()
        tries.append(
            {
                "attempt": attempt,
                "heard": transcript,
                "matched": ok,
                "duration": len(wave) / 24000,
            }
        )
        if ratio > score:
            best = wave
            score = ratio
        if ok:
            return (
                wave,
                {
                    "passed": True,
                    "attempts": tries,
                    "same_model": True,
                    "same_text": True,
                },
            )
    return (
        best,
        {"passed": False, "attempts": tries, "same_model": True, "same_text": True},
    )
