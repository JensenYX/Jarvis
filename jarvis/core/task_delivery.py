import re


def short_title(task, result, language="zh"):
    for evidence in result.evidence:
        if evidence.get("tool") == "calculate":
            return "the calculation" if language == "en" else "刚才那道数学题"
        if evidence.get("tool") == "get_weather":
            city = evidence.get("observation", {}).get("location", {}).get("name")
            if city:
                return f"Weather in {city}" if language == "en" else f"{city}天气"
    title = result.artifacts[0].get("title", "") if result.artifacts else ""
    title = re.split("[（(·|：:—]|\\s+-\\s+", title)[0].strip()
    if title:
        if language != "en" and re.search("[\\u3400-\\u9fff]", title):
            title = re.split("\\s+[A-Za-z]", title)[0]
        return title[:24]
    text = task.objective.split("\n")[0]
    text = re.sub(
        "^(?:然后|再|请|能不能|你能|帮我|给我|please|could you|can you|help me|and then|then|also|\\s)+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub("[吗吧呢？?。.!！]+$", "", text)
    return text[:24]


def announcement(task, result, language="zh", contextual=False):
    title = short_title(task, result, language)
    en = language == "en"
    prefix = (
        (f"Item {task.request_index}, " if en else f"第{task.request_index}项，")
        if task.request_count > 1
        else ""
    )
    if result.status == "completed" and result.artifacts:
        return prefix + (
            f"{title} is ready. Open it from the result card."
            if en
            else f"{title}做好了，可以在结果卡片打开。"
        )
    answer = result.spoken_answer.strip()
    if contextual and result.status == "completed" and title and (title not in answer):
        weather = next(
            (
                e.get("observation", {}).get("location", {}).get("name")
                for e in result.evidence
                if e.get("tool") == "get_weather"
            ),
            None,
        )
        identified = bool(
            weather
            and (
                weather in answer or re.search("天气|气温|\\bweather\\b", answer, re.I)
            )
        )
        if any((e.get("tool") == "calculate" for e in result.evidence)) and re.search(
            "数学|计算|算式|calculation|arithmetic", answer, re.I
        ):
            identified = True
        if not identified and (
            not any(
                (
                    word in title and word in answer
                    for word in ("晚饭", "今晚", "新闻", "weather", "news", "dinner")
                )
            )
        ):
            prefix += f"For {title}, " if en else title + "，"
    elif result.status != "completed" and title and (title not in answer):
        prefix += f"About {title}: " if en else f"关于{title}，"
    return prefix + answer
