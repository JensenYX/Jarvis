import asyncio
import time


def begin(runtime, utterance_id):
    if not hasattr(runtime, "memory_observer_results"):
        runtime.memory_observer_results = {}
    runtime.memory_observer_results[utterance_id] = {"status": "pending", "ids": []}
    runtime.memory_observer_pending.add(utterance_id)


def finish(runtime, utterance_id, status, ids=()):
    runtime.memory_observer_results[utterance_id] = {"status": status, "ids": list(ids)}
    runtime.memory_observer_pending.discard(utterance_id)
    runtime.memory_observer_done.add(utterance_id)


async def verified_ids(runtime, record, result):
    if result:
        return [result["id"]]
    if record.get("temporary"):
        return []
    profile = await runtime.memory.profile(runtime.user_id)
    return [
        row["id"]
        for row in profile
        if row["text"] == record["text"] and row["kind"] == record["kind"]
    ]


async def saved_for_source(runtime, source):
    uid = getattr(runtime, "native_source_observations", {}).get(source, source)
    receipt = getattr(runtime, "memory_observer_results", {}).get(uid)
    if not receipt or receipt["status"] != "saved" or (not receipt["ids"]):
        return False
    active = {row["id"] for row in await runtime.memory.profile(runtime.user_id)}
    return getattr(runtime, "memory_observer_results", {}).get(uid) is receipt and all(
        (identifier in active for identifier in receipt["ids"])
    )


async def confirmation(runtime, source, en, timeout_s=35):
    uid = getattr(runtime, "native_source_observations", {}).get(source, source)
    deadline = time.monotonic() + timeout_s
    receipt = None
    while True:
        receipt = getattr(runtime, "memory_observer_results", {}).get(uid)
        if receipt and receipt["status"] != "pending":
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.025)
    saved = await saved_for_source(runtime, source)
    runtime.emit(
        "memory.confirmation.resolved",
        source_utterance_id=source,
        observed_utterance_id=uid,
        saved=saved,
        status=receipt["status"] if receipt else "unobserved",
    )
    text = (
        ("I've saved those preferences." if en else "这些偏好已经记住了。")
        if saved
        else "I couldn't save all those preferences. Please try again."
        if en
        else "这些偏好还没全部保存，请再试一次。"
    )
    runtime.post("memory.confirm_ready", {"source": source, "text": text})
