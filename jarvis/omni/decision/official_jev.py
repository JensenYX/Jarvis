from __future__ import annotations
import asyncio
import os
import time
from .base import (
    DecisionBackend,
    DecisionInput,
    DecisionResult,
    DecisionProtocolError,
    UnsupportedCapability,
    normalize,
)


class OfficialTextJev(DecisionBackend):
    capabilities = {
        "choice": "native",
        "noul": "native",
        "score": "native",
        "image": "unsupported",
        "audio": "unsupported",
        "video": "unsupported",
    }

    def __init__(self, *, model="jev-latest", client=None, config=None):
        import httpx2
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

        self.model = model
        if client is not None:
            self.client = client
        elif (config or {}).get("jev", {}).get(
            "backend", os.getenv("JEV_BACKEND")
        ) == "local":
            from jarvis.core.local_jev import LocalJevClient

            self.client = LocalJevClient(
                (config or {})
                .get("jev", {})
                .get("endpoint", os.getenv("JEV_ENDPOINT", "http://127.0.0.1:8810"))
            )
        else:
            self.client = AsyncTypeSafeClient(
                api_key=os.environ["TYPESAFE_API_KEY"],
                model=model,
                retry=RetryPolicy(max_retries=0),
                timeout=30,
                http_client=httpx2.AsyncClient(
                    timeout=30, limits=httpx2.Limits(keepalive_expiry=60)
                ),
            )

    async def decide(self, request: DecisionInput, *, timeout_s=10):
        from typesafe_sdk import Choice, Noul, Score

        if request.images or request.audio:
            raise UnsupportedCapability(
                "Official text Jev requires observed textual evidence; it cannot review pixels"
            )
        constructors = {"choice": Choice, "noul": Noul, "score": Score}
        start = time.monotonic()
        async with asyncio.timeout(timeout_s):
            result = await self.client.system_one(
                state=request.state,
                model=self.model,
                questions={
                    q.id: constructors[q.primitive](**q.wire())
                    for q in request.questions
                },
            )
        raw = result.model_dump(mode="json")
        try:
            answers = tuple(
                (
                    normalize(
                        q,
                        raw["answers"][q.id],
                        confidence_kind="provider_distribution_statistic",
                    )
                    for q in request.questions
                )
            )
        except (ValueError, KeyError) as error:
            raise DecisionProtocolError(str(error), raw) from error
        return DecisionResult(
            backend="official_text_jev",
            model=raw["model"],
            revision="hosted",
            precision="provider_unspecified",
            input_hash=request.input_hash(),
            answers=answers,
            elapsed_ms=(time.monotonic() - start) * 1000,
            raw=raw,
        )

    async def close(self):
        await self.client.aclose()
