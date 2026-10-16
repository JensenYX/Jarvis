import time

import httpx

from .base import DecisionBackend, DecisionResult, UnsupportedCapability


class GemmaBaseBackend(DecisionBackend):
    capabilities = {
        "choice": "emulated",
        "noul": "emulated",
        "score": "emulated",
        "image": "native",
        "audio": "unsupported",
        "video": "causal_stills",
    }

    def __init__(self, endpoint):
        self.client = httpx.AsyncClient(base_url=endpoint, timeout=10, trust_env=False)

    async def decide(self, request, *, timeout_s=10):
        if request.audio or any(
            not 2 <= len(q.option_ids()) <= 62 for q in request.questions
        ):
            raise UnsupportedCapability("Gemma supports text, images and 2–62 options")
        start = time.monotonic()
        response = await self.client.post(
            "/decide", json=request.model_dump(mode="json"), timeout=timeout_s
        )
        response.raise_for_status()
        result = DecisionResult.model_validate(response.json())
        if result.backend != "gemma_base" or result.input_hash != request.input_hash():
            raise ValueError("Visual intent response does not match its request")
        if len(result.answers) != len(request.questions):
            raise ValueError("Visual intent response is incomplete")
        for answer, question in zip(result.answers, request.questions):
            if (
                answer.question_id != question.id
                or answer.primitive != question.primitive
                or answer.option_ids != question.option_ids()
            ):
                raise ValueError("Visual intent response changed the available options")
        return result.model_copy(
            update={"elapsed_ms": (time.monotonic() - start) * 1000}
        )

    async def close(self):
        await self.client.aclose()
