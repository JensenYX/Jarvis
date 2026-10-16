import asyncio
import os
import time
import json
import httpx


class ReadOnlyReasoning:
    def __init__(self, worker, config, emit):
        self.worker = worker
        self.config = config
        self.emit = emit
        self.retry_primary_at = 0.0

    async def complete(
        self,
        messages,
        *,
        json_mode=False,
        max_tokens=None,
        reasoning_effort=None,
        latency_sensitive=False,
        planning=False,
    ):
        enabled = self.config.get("voxel", {}).get(
            "readonly_fallback"
        ) == "gemini" and bool(os.getenv("GEMINI_API_KEY"))
        options = self.config.get("voxel", {})
        if (
            latency_sensitive
            and options.get("response_deadline_s")
            or (planning and options.get("planning_deadline_s"))
        ):
            return await self.hedged(
                messages,
                json_mode=json_mode,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                deadline=options.get("planning_deadline_s", 12)
                if planning
                else options.get("response_deadline_s", 5),
                hedge=options.get("reasoning_hedge_s", 1.2),
                fallback=enabled,
            )
        if not enabled or time.monotonic() >= self.retry_primary_at:
            started = time.monotonic()
            self.emit(
                "voxel.reasoning.started",
                provider="deepseek",
                model=getattr(self.worker, "model", None),
                read_only=True,
            )
            try:
                async with asyncio.timeout(
                    20 if planning else 6 if latency_sensitive else 14
                ):
                    result = await self.worker.complete(
                        messages,
                        json_mode=json_mode,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                self.emit(
                    "voxel.reasoning.completed",
                    provider="deepseek",
                    model=getattr(self.worker, "model", None),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    finish_reason=result.get("choices", [{}])[0].get("finish_reason"),
                    usage=result.get("usage"),
                    read_only=True,
                )
                return result
            except (httpx.HTTPStatusError, httpx.TransportError, TimeoutError) as error:
                status = (
                    error.response.status_code
                    if isinstance(error, httpx.HTTPStatusError)
                    else None
                )
                if not enabled or (
                    status is not None and status not in {429, 500, 502, 503, 504}
                ):
                    raise
                self.retry_primary_at = time.monotonic() + (
                    2 if isinstance(error, TimeoutError) else 15
                )
                self.emit(
                    "voxel.reasoning.primary_unavailable",
                    reason=type(error).__name__,
                    status_code=status,
                )
        model = self.config["models"]["gemini"]
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": max(1000, max_tokens or 1800),
            "reasoning_effort": "minimal"
            if reasoning_effort in {None, "none"}
            else "low",
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        self.emit("voxel.reasoning.fallback", model=model, read_only=True)
        url = (
            os.environ.get(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta/openai",
            ).rstrip("/")
            + "/chat/completions"
        )
        async with asyncio.timeout(8 if latency_sensitive else 15):
            for attempt in range(2):
                response = await self.worker.client.post(
                    url,
                    headers={
                        "Authorization": "Bearer " + os.environ["GEMINI_API_KEY"],
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                if attempt == 0 and response.status_code in {429, 500, 502, 503, 504}:
                    self.emit(
                        "voxel.reasoning.retry",
                        status_code=response.status_code,
                        model=model,
                    )
                    await asyncio.sleep(0.25)
                    continue
                response.raise_for_status()
                result = response.json()
                self.emit(
                    "voxel.reasoning.completed",
                    provider="gemini",
                    model=model,
                    finish_reason=result.get("choices", [{}])[0].get("finish_reason"),
                    usage=result.get("usage"),
                    read_only=True,
                )
                return result

    async def hedged(
        self,
        messages,
        *,
        json_mode,
        max_tokens,
        reasoning_effort,
        deadline,
        hedge,
        fallback,
    ):
        started = time.monotonic()
        primary_failed = asyncio.Event()

        def checked(result):
            choices = result.get("choices") or []
            if not choices:
                raise ValueError("Empty reasoning result")
            c = choices[0]
            text = c.get("message", {}).get("content")
            if (
                c.get("finish_reason") != "stop"
                or not isinstance(text, str)
                or (not text.strip())
            ):
                raise ValueError("Incomplete reasoning result")
            if json_mode:
                json.loads(text)
            return result

        async def primary():
            self.emit(
                "voxel.reasoning.started",
                provider="deepseek",
                model=getattr(self.worker, "model", None),
                read_only=True,
            )
            try:
                result = checked(
                    await self.worker.complete(
                        messages,
                        json_mode=json_mode,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                )
                self.emit(
                    "voxel.reasoning.completed",
                    provider="deepseek",
                    model=getattr(self.worker, "model", None),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    finish_reason="stop",
                    usage=result.get("usage"),
                    read_only=True,
                )
                return result
            except asyncio.CancelledError:
                raise
            except Exception:
                primary_failed.set()
                raise

        async def secondary():
            try:
                await asyncio.wait_for(primary_failed.wait(), max(0, hedge))
            except TimeoutError:
                pass
            model = self.config["models"]["gemini"]
            self.emit(
                "voxel.reasoning.fallback", model=model, read_only=True, hedged=True
            )
            body = {
                "model": model,
                "messages": messages,
                "stream": False,
                "max_tokens": max(600, max_tokens or 1000),
                "reasoning_effort": "minimal"
                if reasoning_effort in {None, "none"}
                else "low",
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            url = (
                os.environ.get(
                    "GEMINI_BASE_URL",
                    "https://generativelanguage.googleapis.com/v1beta/openai",
                ).rstrip("/")
                + "/chat/completions"
            )
            response = await self.worker.client.post(
                url,
                headers={
                    "Authorization": "Bearer " + os.environ["GEMINI_API_KEY"],
                    "Content-Type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            result = checked(response.json())
            self.emit(
                "voxel.reasoning.completed",
                provider="gemini",
                model=model,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                finish_reason="stop",
                usage=result.get("usage"),
                read_only=True,
            )
            return result

        jobs = {asyncio.create_task(primary())}
        if fallback:
            jobs.add(asyncio.create_task(secondary()))
        pending = set(jobs)
        last_error = None
        try:
            async with asyncio.timeout(deadline):
                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    for job in done:
                        try:
                            return job.result()
                        except httpx.HTTPStatusError as error:
                            if error.response.status_code not in {
                                429,
                                500,
                                502,
                                503,
                                504,
                            }:
                                raise
                            last_error = error
                        except (
                            httpx.TransportError,
                            TimeoutError,
                            ValueError,
                        ) as error:
                            last_error = error
                raise last_error or RuntimeError("No reasoning result")
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
