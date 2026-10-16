import re

_DIGITS = set(
    "zero oh one two three four five six seven eight nine ten double triple dash hyphen dot point".split()
)
_CJK = set("零〇一二三四五六七八九十幺两点杠")


def _tokens(text):
    return re.findall("[A-Za-z]+|\\d+|[\\u4e00-\\u9fff]", text)


def _identifier_token(token):
    return (
        token.isdigit()
        or token.lower() in _DIGITS
        or token in _CJK
        or (len(token) == 1 and token.isascii() and token.isalpha())
    )


def is_identifier_fragment(text):
    tokens = _tokens(text)
    return (
        bool(tokens)
        and len(tokens) <= 32
        and all((_identifier_token(token) for token in tokens))
    )


def needs_fragment_grace(text):
    tokens = _tokens(text)
    tail = []
    for token in reversed(tokens):
        if not _identifier_token(token):
            break
        tail.append(token)
    if len(tail) >= 2 or (tail and tail[0].isdigit() and (len(tail[0]) >= 2)):
        return True
    return bool(tail) and bool(
        re.search(
            "\\b(?:number|code|zip|digits|spelling)\\b|号码|邮编|数字", text, re.I
        )
    )


def fragment_binding(turns, decision, uid, unavailable, now, binding_timeout_s=20):
    grace = decision.get("identifier_grace")
    if grace is None:
        if not needs_fragment_grace(turns[uid]["final"].text):
            return (uid, 0.0)
        grace = {
            "root": uid,
            "limit": min(now + 6, decision["at"] + max(0, binding_timeout_s - 0.25)),
        }
        decision["identifier_grace"] = grace
    root = grace["root"]
    if root not in turns or root in unavailable:
        return (uid, 0.0)
    selected = root
    anchor = turns[root]
    until = max(anchor.get("final_at", decision["at"]), decision["at"]) + 1.4
    later = sorted(
        ((key, row) for key, row in turns.items() if row["start"] > anchor["start"]),
        key=lambda item: item[1]["start"],
    )
    for key, row in later:
        if (
            key in unavailable
            or row["start"] - anchor["final"].audio_end_sample > 2 * 16000
        ):
            break
        final = row.get("final")
        if final is None:
            return (selected, max(0.0, min(0.2, grace["limit"] - now)))
        if row.get("final_at", now) > grace["limit"]:
            break
        if not is_identifier_fragment(final.text):
            return (selected, 0.0)
        selected, anchor = (key, row)
        until = max(until, row.get("final_at", now) + 1.4)
    return (selected, max(0.0, min(until, grace["limit"]) - now))
