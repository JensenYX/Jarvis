import base64
import re
import time
import unicodedata
from difflib import SequenceMatcher
import httpx


def normalized_text(text):
    return "".join(
        (c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())
    )


def repair_phrase(expected, suffix):
    if (
        suffix
        and 0 < len(normalized_text(suffix)) < 3
        and any(("一" <= c <= "鿿" for c in suffix))
    ):
        start = expected.rfind(suffix)
        boundary = (
            max(
                (expected.rfind(mark, 0, start) for mark in "，,。.!?！？；;：:"),
                default=-1,
            )
            + 1
        )
        clause = expected[boundary:].strip()
        if 3 <= len(normalized_text(clause)) <= 24:
            return clause
    if not suffix or not suffix[0].isascii() or (not suffix[0].isalpha()):
        return suffix
    start = expected.rfind(suffix)
    words = list(re.finditer("[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", expected[:start]))
    for word in reversed(words[-2:]):
        candidate = expected[word.start() :].strip()
        if len(candidate.split()) > 5 or len(candidate) > 48:
            break
        if any((c in expected[word.end() : start] for c in ".!?。！？")):
            break
        suffix = candidate
    return suffix


def missing_suffix(expected, heard, *, anchored=False):

    def canonical(text):
        chars, positions = ([], [])
        for i, char in enumerate(text):
            for normalized in unicodedata.normalize("NFKC", char).casefold():
                if normalized.isalnum():
                    chars.append(normalized)
                    positions.append(i)
        return ("".join(chars), positions)

    target, positions = canonical(expected)
    observed, _ = canonical(heard)
    if not target or not observed or target == observed:
        return None
    end = len(observed)
    if not target.startswith(observed):
        if not anchored:
            return None
        matcher = SequenceMatcher(None, target, observed, autojunk=False)
        blocks = matcher.get_matching_blocks()[:-1]
        if not blocks:
            return None
        last = blocks[-1]
        anchor = observed[last.b : last.b + last.size]
        if (
            last.b + last.size != len(observed)
            or last.size < 6
            or target.count(anchor) != 1
            or (matcher.ratio() < 0.7)
            or (last.a + last.size >= len(target))
        ):
            return None
        end = last.a + last.size
    if end < max(5, len(target) * 0.55) or len(target) - end > 24:
        return None
    start = positions[end]
    while (
        start
        and expected[start].isascii()
        and expected[start].isalpha()
        and expected[start - 1].isascii()
        and expected[start - 1].isalpha()
    ):
        start -= 1
    suffix = expected[start:].strip()
    return suffix if len(suffix.split()) <= 5 and len(suffix) <= 48 else None


class SpeechChecker:
    def __init__(self, config):
        self.endpoint = config["endpoint"].rstrip("/") + "/transcribe"
        self.client = httpx.AsyncClient(
            timeout=config.get("timeout_ms", 1200) / 1000, trust_env=False
        )
        self.anchored = config.get("anchored_suffix", False)

    async def check(self, pcm, expected, language):
        started = time.monotonic()
        try:
            r = await self.client.post(
                self.endpoint,
                json={"pcm": base64.b64encode(pcm).decode(), "language": language},
            )
            r.raise_for_status()
            text = r.json()["text"]
            suffix = missing_suffix(expected, text, anchored=self.anchored)
            target, observed = (normalized_text(expected), normalized_text(text))
            return {
                "heard": text,
                "suffix": suffix,
                "repair_text": repair_phrase(expected, suffix),
                "matches": bool(normalized_text(expected))
                and normalized_text(expected) == normalized_text(text),
                "substantial_omission": bool(target)
                and len(observed) < len(target) * 0.8
                and (not observed.endswith(target[-4:])),
                "elapsed_ms": (time.monotonic() - started) * 1000,
            }
        except Exception as error:
            return {
                "suffix": None,
                "error": type(error).__name__,
                "elapsed_ms": (time.monotonic() - started) * 1000,
            }

    async def close(self):
        await self.client.aclose()
