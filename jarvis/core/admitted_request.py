from jarvis.core.structured import complete


async def decompose(runtime, text):
    data = await complete(
        runtime.worker,
        [
            {
                "role": "system",
                "content": 'Split independent operations into 2 to 5 contiguous verbatim spans. Return JSON {"requests":["span",...]}. Keep each task with all its constraints. Preserve order and at least 85 percent of the input. Do not rewrite or duplicate words.',
            },
            {"role": "user", "content": text},
        ],
        limit=1400,
    )
    parts = data.get("requests")
    if not isinstance(parts, list) or not 2 <= len(parts) <= 5:
        raise ValueError("Expected two to five request spans")
    cursor = covered = 0
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            raise ValueError("Request spans must contain text")
        start = text.find(part, cursor)
        if start < cursor:
            raise ValueError(
                "Request spans must preserve the original wording and order"
            )
        cursor = start + len(part)
        covered += len(part)
    if covered < len(text) * 0.85:
        raise ValueError("Request spans omit part of the user request")
    return parts
