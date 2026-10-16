import re

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
NUMBER = "(?:\\d{1,3}(?:,\\d{3})+|\\d+)(?:\\.\\d+)?"
PHONE = "(?:\\+\\d{1,3}[ -]?)?(?:\\(\\d{3}\\)|\\d{3})[ -]\\d{3}[ -]\\d{4}|\\d{3}-\\d{4}"
TOKEN = re.compile(
    "(?<![\\w.])(?:(?P<phone>"
    + PHONE
    + ")|(?P<money>[+-]?\\$[+-]?"
    + NUMBER
    + "|[+-]?"
    + NUMBER
    + " dollars?)|(?P<clock>\\d{1,2}:\\d{2}(?:\\s*[APap][Mm])?)|(?P<number>[+-]?"
    + NUMBER
    + "))(?!\\w|\\.\\d)",
    re.I,
)
PROTECTED = re.compile(
    "`[^`]*`|https?://\\S+|\\b[^\\s@]+@[^\\s@]+\\b|\\b(?:v|version\\s+)\\d+(?:\\.\\d+)+\\b|\\b\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}\\b|\\b\\d{1,2}[-/]\\d{1,2}[-/]\\d{2,4}\\b|\\b\\d{1,2}/\\d{1,2}\\b|[+-]?\\d+(?:\\.\\d+)?[eE][+-]?\\d+|(?<!\\S)/\\S+",
    re.I,
)
DIGIT_CONTEXT = re.compile(
    "(?:\\b(?:phone|telephone|mobile|postal|zip|pin|verification|confirmation|account|card|order)(?:\\s+(?:number|code|id))?|\\bcode|\\bending(?:\\s+in)?)\\s*(?:is\\s*)?[:#]?\\s*$",
    re.I,
)


def cardinal(value):
    if value < 20:
        return ONES[value]
    if value < 100:
        return TENS[value // 10] + ("-" + ONES[value % 10] if value % 10 else "")
    if value < 1000:
        return (
            ONES[value // 100]
            + " hundred"
            + (" " + cardinal(value % 100) if value % 100 else "")
        )
    for size, label in [
        (10**12, "trillion"),
        (10**9, "billion"),
        (10**6, "million"),
        (1000, "thousand"),
    ]:
        if value >= size:
            return (
                cardinal(value // size)
                + " "
                + label
                + (" " + cardinal(value % size) if value % size else "")
            )
    raise ValueError("Unsupported number")


def digits(value):
    words = [ONES[int(c)] for c in value]
    widths = {4: [2, 2], 7: [3, 4], 10: [3, 3, 4]}.get(len(words), [len(words)])
    groups = []
    offset = 0
    for width in widths:
        groups.append(" ".join(words[offset : offset + width]))
        offset += width
    return ", ".join(groups)


def decimal_words(value):
    sign = (
        "minus " if value.startswith("-") else "plus " if value.startswith("+") else ""
    )
    value = value.lstrip("+-").replace(",", "")
    whole, sep, fraction = value.partition(".")
    head = (
        digits(whole)
        if len(whole) > 1 and whole.startswith("0") or len(whole) > 12
        else cardinal(int(whole))
    )
    return (
        sign
        + head
        + (" point " + " ".join((ONES[int(c)] for c in fraction)) if sep else "")
    )


def money(value):
    number = re.sub(" dollars?$", "", value.replace("$", ""), flags=re.I)
    if number.startswith(("+-", "-+", "++", "--")):
        return value
    sign = (
        "minus "
        if number.startswith("-")
        else "plus "
        if number.startswith("+")
        else ""
    )
    plain = number.lstrip("+-").replace(",", "")
    whole, sep, fraction = plain.partition(".")
    if len(whole) > 12 or (sep and len(fraction) != 2):
        return decimal_words(number) + " dollars"
    dollars = int(whole)
    text = sign + cardinal(dollars) + (" dollar" if dollars == 1 else " dollars")
    if sep and int(fraction):
        text += (
            " and "
            + cardinal(int(fraction))
            + (" cent" if int(fraction) == 1 else " cents")
        )
    return text


def render(text, language="en"):
    if not text or language != "en":
        return text
    protected = [m.span() for m in PROTECTED.finditer(text)]

    def replace(match):
        start, end = match.span()
        if any((start < b and end > a for a, b in protected)):
            return match.group()
        value = match.group()
        if match.lastgroup == "phone":
            numbers = re.findall("\\d+", value)
            return ("plus " if value.startswith("+") else "") + ", ".join(
                (" ".join((ONES[int(c)] for c in part)) for part in numbers)
            )
        if match.lastgroup == "money":
            return money(value)
        if match.lastgroup == "clock":
            clock = re.fullmatch("(\\d{1,2}):(\\d{2})(\\s*[APap][Mm])?", value)
            hour, minute = (int(clock[1]), int(clock[2]))
            if hour > 23 or minute > 59:
                return value
            if minute == 0:
                return cardinal(hour) + (
                    " " + clock[3].strip() if clock[3] else " o'clock"
                )
            minutes = "oh " + ONES[minute] if 0 < minute < 10 else cardinal(minute)
            return (
                cardinal(hour)
                + " "
                + minutes
                + (" " + clock[3].strip() if clock[3] else "")
            )
        raw = value.lstrip("+-").replace(",", "")
        if (
            "." not in raw
            and (not value.startswith(("+", "-")))
            and (
                len(raw) > 1
                and raw.startswith("0")
                or len(raw) >= 7
                or DIGIT_CONTEXT.search(text[max(0, start - 64) : start])
            )
        ):
            return digits(raw)
        return decimal_words(value)

    return TOKEN.sub(replace, text)


def integer_words(n, en=True):
    n = int(n)
    if en:
        small = [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
        ]
        if n < 0:
            return "minus " + integer_words(-n, en)
        if n < 20:
            return small[n]
        if n < 100:
            return [
                "",
                "",
                "twenty",
                "thirty",
                "forty",
                "fifty",
                "sixty",
                "seventy",
                "eighty",
                "ninety",
            ][n // 10] + (" " + small[n % 10] if n % 10 else "")
        return str(n)
    if n < 0:
        return "零下" + integer_words(-n, False)
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 100:
        return (
            (digits[n // 10] if n >= 20 else "")
            + "十"
            + (digits[n % 10] if n % 10 else "")
        )
    return str(n)
