import asyncio
import time


class ActorQueue(asyncio.Queue):
    def __init__(self, capacity, failure):
        super().__init__(maxsize=capacity)
        self.failure = failure

    def put_nowait(self, item):
        try:
            return super().put_nowait(item)
        except asyncio.QueueFull:
            self.failure.set()
            raise


def prune_playback(runtime):
    now = time.monotonic()
    completed = []
    for identifier, response in runtime.presentations.items():
        if not response.get("history_recorded") or response.get("speech_check_pending"):
            continue
        response.setdefault("retired_at", now)
        chunks = [
            runtime.chunks[key] for key in response["chunks"] if key in runtime.chunks
        ]
        if response.get("generated_done") and all(
            (chunk["played"] == chunk["samples"] for chunk in chunks)
        ):
            for chunk in chunks:
                chunk["pcm"] = ""
        if (
            identifier not in {runtime.response_id, runtime.paused_response}
            and now - response["retired_at"] >= 60
        ):
            completed.append(identifier)
    for identifier in completed[:-64]:
        response = runtime.presentations.pop(identifier)
        for key in response["chunks"]:
            runtime.chunks.pop(key, None)
    del runtime.played[:-128]
    del runtime.history[:-128]
    if (
        len(runtime.presentations) > 256
        or len(runtime.chunks) > 4096
        or sum((len(c.get("pcm", "")) for c in runtime.chunks.values())) > 64000000
    ):
        runtime.capacity_exceeded.set()
