import re


def response_budget(text, base=30):
    if not text:
        return base
    chinese = len(re.findall("[\\u3400-\\u9fff]", text))
    words = len(re.findall("[A-Za-z0-9]+(?:[\\x27.-][A-Za-z0-9]+)*", text))
    return max(base, min(90, 10 + 1.5 * (words / 2.5 + chinese / 4.5)))
