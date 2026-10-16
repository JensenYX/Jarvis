import re

_NUMBER = "[+−-]?\\d+(?:\\.\\d+)?"
_PAIR = re.compile(
    "[（(\\[]\\s*"
    + _NUMBER
    + "\\s*[,，]\\s*"
    + _NUMBER
    + "(?:\\s*[,，]\\s*"
    + _NUMBER
    + ")?\\s*[）)\\]]"
)
_AXIS = re.compile(
    "(?<![a-zA-Z])(?:[xyzXYZ]\\s*(?:[=＝:：]\\s*)?"
    + _NUMBER
    + "|[+−-][xyzXYZ])(?:\\s*(?:方向|一带))?"
)


def spoken_text(text):
    text = _PAIR.sub("", text)
    text = _AXIS.sub("", text)
    text = re.sub("(?:坐标(?:是|为|在)?|位置在)\\s*(?=[，。；、]|$)", "", text)
    text = re.sub("[（(]\\s*[）)]", "", text)
    text = re.sub("\\s+", " ", text).strip()
    return re.sub("([，；、])\\s*([，。；、])", "\\2", text)
