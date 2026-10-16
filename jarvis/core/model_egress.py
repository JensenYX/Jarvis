from __future__ import annotations
import base64
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit, parse_qsl, urlencode


class OutboundPathError(ValueError):
    pass


_WEB_URL = re.compile('https?://[^\\s<>"\\x27]+', re.I)
_PATH = re.compile(
    'file:(?:/{1,3}|\\\\)[^\\s<>"\\x27`]*|(?<![A-Za-z0-9])[A-Za-z]:[\\\\/][^\\s<>"\\x27`]*|\\\\{2,}[^\\s<>"\\x27`\\\\]+\\\\[^\\s<>"\\x27`]*|(?<![A-Za-z0-9._~%/#])/(?!/)[^\\s<>"\\x27`]*|(?<![A-Za-z0-9._~%/#])//[^\\s<>"\\x27`]+',
    re.I,
)
_ESCAPED_PATH_CHAR = re.compile("\\\\u(?:002[fF]|005[cC]|003[aA])")


def _decode_path_characters(value):
    value = _ESCAPED_PATH_CHAR.sub(lambda m: chr(int(m.group()[2:], 16)), value)
    return value.replace("\\/", "/")


def _media_data(value, key):
    if key == "url" and re.fullmatch(
        "data:(?:image/(?:jpeg|png|webp)|audio/(?:wav|mpeg));base64,[A-Za-z0-9+/=]+",
        value,
    ):
        base64.b64decode(value.split(",", 1)[1], validate=True)
        return True
    return False


class PathRedactor:
    def __init__(self):
        self.references = {}

    def _reference(self, path):
        reference = "local-path-" + hashlib.sha256(path.encode()).hexdigest()[:24]
        self.references[reference] = path
        return reference

    def _web_url(self, value):
        parts = urlsplit(value)
        if parts.hostname in {"localhost", "127.0.0.1", "::1"}:
            return self._reference(value)
        query = urlencode(
            [
                (key, self._text(item))
                for key, item in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return (
            urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    query,
                    self._text(parts.fragment),
                )
            )
            if parts.query or parts.fragment
            else value
        )

    def _text(self, value):
        try:
            embedded = json.loads(value)
        except (ValueError, TypeError):
            embedded = None
        if isinstance(embedded, (dict, list)):
            return json.dumps(self.clean(embedded), ensure_ascii=False)
        if isinstance(embedded, str):
            return json.dumps(self._text(embedded), ensure_ascii=False)
        decoded = _decode_path_characters(value)

        def token(match):
            raw = match.group()
            if re.match("https?://", raw, re.I):
                return raw
            candidate = raw
            for _ in range(3):
                changed = unquote(candidate)
                if changed == candidate:
                    break
                candidate = changed
            return (
                self._reference(raw)
                if candidate != raw and _PATH.search(candidate)
                else raw
            )

        decoded = re.sub('[^\\s<>"\\x27`]+', token, decoded)
        parts = []
        start = 0
        for match in _WEB_URL.finditer(decoded):
            parts.append(
                _PATH.sub(
                    lambda m: self._reference(m.group()), decoded[start : match.start()]
                )
            )
            parts.append(self._web_url(match.group()))
            start = match.end()
        parts.append(_PATH.sub(lambda m: self._reference(m.group()), decoded[start:]))
        return "".join(parts)

    def clean(self, value, *, key=None):
        if isinstance(value, dict):
            if (
                key == "input_audio"
                and set(value) == {"data", "format"}
                and (value["format"] in {"wav", "mp3"})
            ):
                data = base64.b64decode(value["data"], validate=True)
                valid = (
                    value["format"] == "wav"
                    and data[:4] == b"RIFF"
                    and (data[8:12] == b"WAVE")
                    or (
                        value["format"] == "mp3"
                        and (data[:3] == b"ID3" or data[:1] == b"\xff")
                    )
                )
                if not valid:
                    raise OutboundPathError("Invalid external audio data")
                return dict(value)
            return {self._text(k): self.clean(v, key=k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            return value if _media_data(value, key) else self._text(value)
        if value is None or isinstance(value, (int, float, bool)):
            return value
        raise OutboundPathError("Non-JSON value in external model payload")

    def restore(self, value):
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        if isinstance(value, str):
            try:
                embedded = json.loads(value)
            except (ValueError, TypeError):
                embedded = None
            if isinstance(embedded, (dict, list, str)):
                return json.dumps(self.restore(embedded), ensure_ascii=False)
            return re.sub(
                "local-path-[0-9a-f]{24}",
                lambda m: self.references.get(m.group(), m.group()),
                value,
            )
        return value


def model_payload(body):
    return PathRedactor().clean(body)


def assert_no_absolute_paths(body):
    inspector = PathRedactor()
    inspector.clean(body)
    if inspector.references:
        raise OutboundPathError("Absolute filesystem path in external model payload")


async def post_model_json(client, url, *, json, **kwargs):
    assert_no_absolute_paths(json)
    return await client.post(url, json=json, **kwargs)
