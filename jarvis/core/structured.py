import asyncio
import json


async def complete(worker, messages, *, limit=1800, timeout_s=18, validate=None):
    async with asyncio.timeout(timeout_s):
        response = await worker.complete(
            messages,
            json_mode=True,
            max_tokens=limit,
            request_timeout_s=timeout_s,
            temperature=0,
        )
    choice = response["choices"][0]
    if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
        raise ValueError("Expected a complete structured response")
    content = choice["message"].get("content", "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    if validate is not None:
        validate(value)
    return value
