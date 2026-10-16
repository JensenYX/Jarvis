from __future__ import annotations
import time
import httpx
from .base import DecisionBackend, DecisionInput, DecisionResult, UnsupportedCapability


class JevOmniBackend(DecisionBackend):
    capabilities = {
        "choice": "native",
        "noul": "emulated",
        "score": "emulated",
        "image": "native",
        "audio": "native_separate_request",
        "video": "emulated_causal_stills",
    }

    def __init__(self, endpoint, *, priority="user"):
        self.client = httpx.AsyncClient(base_url=endpoint, timeout=60, trust_env=False)
        self.priority = priority

    async def decide(self, request: DecisionInput, *, timeout_s=10):
        if len(request.images) > 16 or any(
            (len(q.option_ids()) < 2 for q in request.questions)
        ):
            raise UnsupportedCapability(
                "Jev-Omni requires 2–256 options and at most 16 causal frames"
            )
        start = time.monotonic()
        response = await self.client.post(
            "/decide",
            json=request.model_dump(mode="json"),
            timeout=timeout_s,
            headers={"X-Inference-Priority": self.priority},
        )
        response.raise_for_status()
        result = DecisionResult.model_validate(response.json())
        if result.backend != "jev_omni" or result.model not in {
            "akhilaaa3/Jev-Omni",
            "Jev-Omni/unified",
            "Jev-Omni/legacy",
        }:
            raise ValueError("A Jev decision endpoint returned another model")
        if result.input_hash != request.input_hash() or [
            a.question_id for a in result.answers
        ] != [q.id for q in request.questions]:
            raise ValueError("Decision is bound to different input/questions")
        if any(
            (
                a.primitive != q.primitive or a.option_ids != q.option_ids()
                for a, q in zip(result.answers, request.questions)
            )
        ):
            raise ValueError("Decision changed a primitive or option order")
        return result.model_copy(
            update={
                "elapsed_ms": (time.monotonic() - start) * 1000,
                "raw": {**result.raw, "server_elapsed_ms": result.elapsed_ms},
            }
        )

    async def close(self):
        await self.client.aclose()
