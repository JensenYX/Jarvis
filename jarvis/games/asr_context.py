import re
from difflib import SequenceMatcher


def glossary_echo(text, context):
    normalize = lambda value: "".join((c for c in value.casefold() if c.isalnum()))
    heard = normalize(text)
    source = normalize(context)
    if len(heard) < 48:
        return False
    terms = {
        normalize(part.rsplit("：", 1)[-1]) for part in re.split("[、；;,。]", context)
    }
    if sum((len(term) >= 2 and term in heard for term in terms)) < 8:
        return False
    if heard in source:
        return True
    instruction = normalize("按实际听到的语音转录，不要续写")
    if instruction not in source or not heard.endswith(instruction):
        return False
    match = SequenceMatcher(None, heard, source, autojunk=False).find_longest_match()
    return match.size >= 48 and match.size / len(heard) >= 0.8
