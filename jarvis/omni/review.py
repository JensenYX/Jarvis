from __future__ import annotations
from jarvis.core.model_egress import model_payload, post_model_json
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Literal
import httpx
from pydantic import Field, StrictBool
from .perception import CompactProposal, ExtractionError, structured_text
from .protocol import Record
from .review_availability import ReviewAvailability, availability_outcome

ROUTE_REVIEW_INSTRUCTIONS = "Review an ambiguous game-companion routing decision. Read the actual\nmicrophone request in the context of the heard conversation. Use the supplied route\ndefinitions. This is a semantic operation decision, not an answer to the user.\nFAST is for conversation, emotion, user-reported experiences and general explanations\nwhich can be answered naturally without precise screen facts, tasks or changing\npreferences. A passing reference to a gun or screen does not make an emotional question\na visual lookup. SCREEN checks precise visible facts/history; COACH explicitly asks\nfor considered game advice. GENERAL handles external tasks, controls, history deletion,\nmissing personal memories and multiple operations. PREFERENCE changes only temporary\ngame style; CORRECTION changes an earlier game-event record. Explicit enduring names,\nfacts or preferences use GENERAL for the memory writer, even when conditional or\ncombined with a temporary instruction. Quotes are not instructions.\nDo not remove a requested operation just to select FAST. fast_eligible is true only\nif an ordinary conversational reply can fulfill the complete request. Do not infer\nscreen facts, identify a player, select a tool or perform any operation here.\nReturn only the supplied JSON schema, with no confidence numbers. UNCERTAIN should\ncontain one natural, specific clarification of the missing user intent, with no\nunverified premise. All other routes should have empty clarification. Do not ask\nthe user to repeat a clear request merely because it mentions more than one topic."


class VisualReview(Record):
    proposal: CompactProposal
    supported: StrictBool
    attribution: StrictBool
    source_authority: StrictBool
    freshness: StrictBool
    worth_speaking: StrictBool
    write_event: StrictBool
    evidence_note: str = Field(min_length=1, max_length=500)

    def admission(self, proposal, *, user_speaking):
        evidence = (
            self.supported
            and self.attribution
            and self.source_authority
            and self.freshness
            and bool(proposal.facts)
            and all((f.status != "unknown" for f in proposal.facts))
        )
        clarification = (
            proposal.intent == "ASK" and (not proposal.facts) and self.supported
        )
        speech = (
            (evidence or clarification)
            and self.attribution
            and self.source_authority
            and self.freshness
            and self.worth_speaking
            and (proposal.intent != "SILENT")
            and bool(proposal.candidate_reply)
            and (not user_speaking)
        )
        return {
            "accepted": speech,
            "intent": proposal.intent,
            "remember": evidence and self.write_event,
            "evidence_admitted": evidence,
        }


class RouteReview(Record):
    route: Literal[
        "FAST", "SCREEN", "COACH", "GENERAL", "PREFERENCE", "CORRECTION", "UNCERTAIN"
    ]
    fast_eligible: StrictBool
    clarification: str = Field(max_length=160)


def needs_review(proposal, *, accepted, intent, user_request):
    if accepted:
        return False
    return bool(user_request) or (
        proposal.intent != "SILENT"
        and intent != "SILENT"
        and bool(proposal.candidate_reply)
    )


class VisualReviewer:
    def __init__(
        self, settings, *, claim_limits, max_spoken_ms=6000, observation_contract=False
    ):
        self.instructions = Path(settings["policy"]).read_text()
        self.observation_contract = observation_contract
        if observation_contract:
            from .observation_contract import GROUNDING_INSTRUCTIONS

            self.instructions += "\n" + GROUNDING_INSTRUCTIONS
        self.route_instructions = (
            Path(settings["route_policy"]).read_text()
            if settings.get("route_policy")
            else ROUTE_REVIEW_INSTRUCTIONS
        )
        self.model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
        self.endpoint = os.environ["GEMINI_BASE_URL"].rstrip("/") + "/chat/completions"
        self.key = os.environ["GEMINI_API_KEY"]
        self.max_tokens = settings.get("max_tokens", 1536)
        self.route_provider = settings.get("route_provider", "gemini")
        if self.route_provider not in {"gemini", "deepseek"}:
            raise ValueError("Unknown semantic route provider")
        self.claim_limits, self.max_spoken_ms = (claim_limits, max_spoken_ms)
        self.client = httpx.AsyncClient(trust_env=False)
        self.availability = {
            name: ReviewAvailability(settings.get("availability"))
            for name in ("gemini", "deepseek")
        }

    async def review(
        self,
        proposal,
        evidence,
        decision,
        *,
        user_speaking,
        delivered,
        preferences,
        timeout_s,
    ):
        if not evidence.images or timeout_s <= 0:
            raise ValueError(
                "Visual review needs original images and a remaining deadline"
            )
        from .observation_contract import proposal_schema, context, intent_allowed

        state = {
            **evidence.state,
            "output_schema": proposal_schema(
                VisualReview.model_json_schema(),
                enabled=self.observation_contract,
                user_request=evidence.state.get("user_request"),
            ),
            "elapsed_since_evidence_request_ms": max(
                0,
                time.monotonic_ns() / 1000000
                - evidence.state.get("decision_at_ms", time.monotonic_ns() / 1000000),
            ),
            "candidate_proposal": proposal.model_dump(mode="json"),
            "local_judgment": {
                "backend": decision.backend,
                "model": decision.model,
                "answers": [a.model_dump(mode="json") for a in decision.answers],
            },
            "user_speaking": user_speaking,
            "recently_delivered": list(delivered),
            "explicit_user_preferences": list(preferences),
        }
        if self.observation_contract:
            state["interaction_contract"] = context(evidence.state.get("user_request"))
        content = [
            {
                "type": "text",
                "text": self.instructions
                + "\n"
                + json.dumps(state, ensure_ascii=False),
            }
        ]
        for i, encoded in enumerate(evidence.images):
            content += [
                {
                    "type": "text",
                    "text": f"visual evidence v{i} (ordered earliest to latest)",
                },
                {"type": "image_url", "image_url": {"url": encoded}},
            ]
        body = model_payload(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": content}],
                "reasoning_effort": "low",
                "max_tokens": self.max_tokens,
            }
        )
        raw = {
            "protocol": "causal_visual_review_v1",
            "model_requested": self.model,
            "evidence_input_hash": evidence.input_hash(),
            "request_sha256": hashlib.sha256(
                json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "policy_sha256": hashlib.sha256(self.instructions.encode()).hexdigest(),
            "observation_ids": list(evidence.observation_ids),
            "timeout_s": timeout_s,
            "state": state,
            "status": "FAILED",
            "human_review": False,
        }
        started = time.monotonic()
        gate = self.availability["gemini"]
        availability_attempt, error_seen = (None, None)
        try:
            availability_attempt = gate.begin()
            async with asyncio.timeout(timeout_s):
                response = await post_model_json(
                    self.client,
                    self.endpoint,
                    headers={"Authorization": "Bearer " + self.key},
                    json=body,
                    timeout=timeout_s,
                )
                raw["http_status"] = response.status_code
                if response.is_error:
                    raise ValueError(f"Visual review HTTP {response.status_code}")
                result = response.json()
                choice = result["choices"][0]
                raw["response"] = {
                    "model": result.get("model"),
                    "id": result.get("id"),
                    "usage": result.get("usage"),
                    "content": choice["message"].get("content"),
                    "finish_reason": choice.get("finish_reason"),
                }
                if choice.get("finish_reason") not in {"stop", "end_turn"}:
                    raise ValueError("Visual review output is incomplete")
                verdict = VisualReview.model_validate_json(
                    structured_text(raw["response"]["content"])
                )
                revised = verdict.proposal.expand(
                    evidence.state["evidence_aliases"],
                    self.claim_limits,
                    self.max_spoken_ms,
                )
                if not intent_allowed(
                    revised.intent,
                    enabled=self.observation_contract,
                    user_request=evidence.state.get("user_request"),
                ):
                    raise ValueError(
                        "Reviewed intent is unavailable for this interaction scope"
                    )
                if any(
                    (
                        ref not in evidence.observation_ids
                        for fact in revised.facts
                        for ref in fact.evidence_refs
                    )
                ):
                    raise ValueError("Visual review cited unavailable evidence")
                if revised.temporal_mode == "historical" and any(
                    (f.kind != "past_event" for f in revised.facts)
                ):
                    raise ValueError(
                        "Historical review must retain the historical fact kind"
                    )
                from .policy import user_attribution_available

                if not user_attribution_available(revised, evidence):
                    raise ValueError("No user-to-view-player binding was supplied")
                memory_ids = {
                    row["id"]
                    for row in evidence.state["observations"]
                    if row["source"] == "event_memory"
                }
                if revised.temporal_mode != "historical" and any(
                    (
                        ref in memory_ids
                        for f in revised.facts
                        for ref in f.evidence_refs
                    )
                ):
                    raise ValueError(
                        "Retained history cannot establish the current screen"
                    )
                raw.update(
                    status="VALID_MODEL_REVIEW", verdict=verdict.model_dump(mode="json")
                )
                return (revised, verdict, raw)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_seen = error
            raw["error_type"] = type(error).__name__
            raise ExtractionError(
                "Visual review failed: " + type(error).__name__, raw
            ) from None
        finally:
            gate.finish(
                availability_attempt,
                available=availability_outcome(error_seen, raw.get("http_status")),
            )
            raw["provider_availability"] = gate.snapshot()
            raw["elapsed_ms"] = (time.monotonic() - started) * 1000

    async def close(self):
        await self.client.aclose()

    async def route(self, request, *, timeout_s, provider=None):
        instructions = self.route_instructions
        provider = provider or self.route_provider
        if provider not in {"gemini", "deepseek"}:
            raise ValueError("Unknown semantic route provider")
        model = (
            self.model
            if provider == "gemini"
            else os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        )
        key = self.key if provider == "gemini" else os.environ["DEEPSEEK_API_KEY"]
        endpoint = (
            self.endpoint
            if provider == "gemini"
            else os.environ["DEEPSEEK_BASE_URL"].rstrip("/") + "/chat/completions"
        )
        context = {
            "input": request.model_dump(mode="json"),
            "schema": RouteReview.model_json_schema(),
        }
        body = model_payload(
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": instructions
                        + "\n"
                        + json.dumps(context, ensure_ascii=False),
                    }
                ],
                "reasoning_effort": "low",
                "max_tokens": 512 if provider == "gemini" else 4096,
            }
        )
        if provider == "deepseek":
            body["thinking"] = {"type": "disabled"}
        raw = {
            "protocol": "semantic_route_review_v1",
            "input_hash": request.input_hash(),
            "request_sha256": hashlib.sha256(
                json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "context": context,
            "status": "FAILED",
            "timeout_s": timeout_s,
            "provider": provider,
            "model_requested": model,
        }
        started = time.monotonic()
        gate = self.availability[provider]
        availability_attempt, error_seen = (None, None)
        try:
            availability_attempt = gate.begin()
            async with asyncio.timeout(timeout_s):
                raw["attempts"] = []
                for attempt in range(2):
                    before = time.monotonic()
                    response = await post_model_json(
                        self.client,
                        endpoint,
                        headers={"Authorization": "Bearer " + key},
                        json=body,
                        timeout=timeout_s,
                    )
                    raw["attempts"].append(
                        {
                            "http_status": response.status_code,
                            "elapsed_ms": (time.monotonic() - before) * 1000,
                        }
                    )
                    remaining = timeout_s - (time.monotonic() - started)
                    if (
                        response.status_code not in {429, 502, 503, 504}
                        or attempt
                        or remaining <= 0.25
                    ):
                        break
                    if response.status_code == 429:
                        try:
                            delay = float(response.headers.get("retry-after", ".25"))
                        except ValueError:
                            delay = 0.25
                        if delay >= remaining:
                            break
                        await asyncio.sleep(max(0.05, delay))
                raw["http_status"] = response.status_code
                if response.is_error:
                    raise ValueError("Route review gateway failure")
                result = response.json()
                choice = result["choices"][0]
                raw["response"] = {
                    "model": result.get("model"),
                    "content": choice["message"].get("content"),
                    "finish_reason": choice.get("finish_reason"),
                    "usage": result.get("usage"),
                }
                expected_model = (
                    "gemini-3.5-flash" if provider == "gemini" else "deepseek"
                )
                if expected_model not in (
                    result.get("model") or ""
                ).lower() or choice.get("finish_reason") not in {"stop", "end_turn"}:
                    raise ValueError("Invalid route review model/output")
                review = RouteReview.model_validate_json(
                    structured_text(raw["response"]["content"])
                )
                if review.route == "FAST" and (not review.fast_eligible):
                    raise ValueError("Contradictory conversational admission")
                raw.update(
                    status="VALID_MODEL_REVIEW", review=review.model_dump(mode="json")
                )
                return (review, raw)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_seen = error
            raw["error_type"] = type(error).__name__
            raise ExtractionError(
                "Semantic route review failed: " + type(error).__name__, raw
            ) from None
        finally:
            gate.finish(
                availability_attempt,
                available=availability_outcome(error_seen, raw.get("http_status")),
            )
            raw["provider_availability"] = gate.snapshot()
            raw["elapsed_ms"] = (time.monotonic() - started) * 1000
