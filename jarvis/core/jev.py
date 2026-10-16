from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path
import httpx2
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score, RetryPolicy
from jarvis.core.schemas import Decision, TranscriptUpdate

ROUTES = {
    "FAST": "Simple response supported by current evidence; let Venus speak directly.",
    "MEMORY": "Need this user's past preferences, facts or project history absent from current context.",
    "WORKER": "Requires precise calculation, registered tools, current external facts or complex reasoning.",
    "CLARIFY": "Missing essential information only the user can provide; ask one short question.",
    "CONTROL": "Manage an existing task or playback: refine, replace, cancel, status, stop speaking or resume.",
    "IGNORE": "No response needed: backchannel, other-directed speech or non-task sound.",
    "WAIT": "Incomplete prefix; additional imminent speech can resolve ambiguity. Must be bounded.",
}
PREFIX_ROUTES = {
    "WAIT": "Wait: prefix is incomplete, ambiguous, or does not justify useful read-only prefetch.",
    "PREFETCH_MEMORY": "Useful read-only personal-memory lookup is already clear from the prefix.",
    "PREFETCH_WORKER": "Start a provisional read-only worker on the already spoken goal; no delivery before final confirmation.",
}


def estimate_tokens(value) -> int:
    text = json.dumps(value, ensure_ascii=False)
    return sum((2 if ord(char) > 127 else 0.3 for char in text)).__ceil__()


def budget_state(state: dict, max_tokens: int) -> tuple[dict, list[dict]]:
    state = json.loads(json.dumps(state, ensure_ascii=False))
    removed = []
    for key in (
        "examples",
        "history",
        "memory_candidates",
        "skill_candidates",
        "played",
    ):
        while estimate_tokens(state) > max_tokens and state.get(key):
            index = 0 if key in ("history", "played") else -1
            item = state[key].pop(index)
            removed.append(
                {"field": key, "id": item.get("id") if isinstance(item, dict) else None}
            )
    if estimate_tokens(state) > max_tokens:
        raise ValueError("Mandatory decision context exceeds configured token estimate")
    return (state, removed)


def budget_decision(state, questions, max_tokens):
    state = json.loads(json.dumps(state, ensure_ascii=False))
    questions = json.loads(json.dumps(questions, ensure_ascii=False))
    removed = []

    def trim_options():
        if state.get("decision_kind") == "memory":
            retained = {str(row["id"]) for row in state.get("memory_candidates", [])}
            for key in list(questions):
                if key not in retained:
                    questions.pop(key)
        if "skill" in questions:
            retained = {row["id"] for row in state.get("skill_candidates", [])} | {
                "NONE"
            }
            questions["skill"]["criteria"] = {
                k: v for k, v in questions["skill"]["criteria"].items() if k in retained
            }

    trim_options()
    for key in (
        "examples",
        "history",
        "memory_candidates",
        "skill_candidates",
        "played",
    ):
        while estimate_tokens(
            {"state": state, "questions": questions}
        ) > max_tokens and state.get(key):
            item = state[key].pop(0 if key in {"history", "played"} else -1)
            removed.append(
                {"field": key, "id": item.get("id") if isinstance(item, dict) else None}
            )
            trim_options()
    estimate = estimate_tokens({"state": state, "questions": questions})
    if estimate > max_tokens:
        raise ValueError(
            "Mandatory state and choice instructions exceed the decision budget"
        )
    return (state, questions, removed, estimate)


class JevController:
    def __init__(self, config, emit, fallback, client=None):
        import os

        self.config, self.emit, self.fallback = (config, emit, fallback)
        self.assets = Path(config["assets"])
        self.prompts = {
            name: (self.assets / "prompts" / f"jev_{name}.md").read_text()
            for name in ("route", "memory", "delivery")
        }
        transport_timeout = (
            max(
                config["jev"]["total_deadline_ms"],
                config["jev"].get("warmup_deadline_ms", 0),
            )
            / 1000
        )
        if client is not None:
            self.client = client
        elif config["jev"].get("backend") == "local":
            from jarvis.core.local_jev import LocalJevClient

            self.client = LocalJevClient(config["jev"]["endpoint"])
        else:
            self.client = AsyncTypeSafeClient(
                api_key=os.environ["TYPESAFE_API_KEY"],
                model=config["models"]["jev"],
                retry=RetryPolicy(max_retries=0),
                timeout=transport_timeout,
                http_client=httpx2.AsyncClient(
                    timeout=transport_timeout,
                    limits=httpx2.Limits(
                        keepalive_expiry=config["jev"].get("keepalive_s", 60)
                    ),
                ),
            )

    def prompt(self, name):
        return self.prompts[name]

    async def judge_typed(self, kind, state, questions, *, deadline_ms=None):
        cfg = self.config["jev"]
        state, questions, removed, estimate = budget_decision(
            state, questions, cfg["max_context_tokens"]
        )
        self.emit(
            "jev.started",
            decision_kind=kind,
            state=state,
            questions=questions,
            dropped_context=removed,
            context_token_estimate=estimate,
        )
        started = time.monotonic()
        constructors = {"choice": Choice, "noul": Noul, "score": Score}
        async with asyncio.timeout((deadline_ms or cfg["total_deadline_ms"]) / 1000):
            result = await self.client.system_one(
                state=state,
                model=self.config["models"]["jev"],
                questions={
                    key: constructors[q.get("type", "choice")](**q)
                    for key, q in questions.items()
                },
            )
        answers = {
            key: value.model_dump(mode="json") for key, value in result.answers.items()
        }
        self.emit(
            "jev.finished",
            decision_kind=kind,
            answers=answers,
            elapsed_ms=(time.monotonic() - started) * 1000,
            raw=result.model_dump(mode="json"),
        )
        return answers

    async def warm_connection(self):
        started = time.monotonic_ns()
        try:
            budget = self.config["jev"].get(
                "warmup_deadline_ms", self.config["jev"]["total_deadline_ms"]
            )
            async with asyncio.timeout(budget / 1000):
                for attempt in range(
                    2 if self.config["jev"].get("warmup_deadline_ms") else 1
                ):
                    try:
                        await self.client.models.list()
                        break
                    except Exception:
                        if attempt or not self.config["jev"].get("warmup_deadline_ms"):
                            raise
                        await asyncio.sleep(0.1)
            self.emit(
                "jev.connection.warmed",
                elapsed_ms=(time.monotonic_ns() - started) / 1000000.0,
            )
        except Exception as error:
            self.emit("jev.connection.warm_failed", error=type(error).__name__)

    async def judge(
        self,
        kind: str,
        state: dict,
        questions: dict,
        *,
        partial=False,
        applicable=None,
        threshold=None,
        allow_fallback=True,
        deadline_ms=None,
    ):
        cfg = self.config["jev"]
        state = {
            **state,
            "decision_kind": kind,
            "policy_version": self.config["policy_version"],
        }
        try:
            state, questions, removed, estimate = budget_decision(
                state, questions, cfg["max_context_tokens"]
            )
        except ValueError:
            self.emit(
                "jev.budget_exceeded",
                decision_kind=kind,
                context_token_estimate=estimate_tokens(
                    {"state": state, "questions": questions}
                ),
                max_context_tokens=cfg["max_context_tokens"],
                field_estimates={k: estimate_tokens(v) for k, v in state.items()},
            )
            raise
        if kind == "memory":
            retained = {
                str(candidate["id"]) for candidate in state.get("memory_candidates", [])
            }
            questions = {
                key: question for key, question in questions.items() if key in retained
            }
            if not questions:
                return ({}, {}, "jev")
        started = time.monotonic_ns()
        self.emit(
            "jev.started",
            decision_kind=kind,
            state=state,
            questions=questions,
            dropped_context=removed,
            context_token_estimate=estimate,
            context_token_estimate_scope="state plus choice instructions and options",
        )
        try:
            async with asyncio.timeout(
                (deadline_ms or cfg["total_deadline_ms"]) / 1000
            ):
                response = await self.client.system_one(
                    state=state,
                    questions={
                        key: Choice(**question) for key, question in questions.items()
                    },
                    model=self.config["models"]["jev"],
                )
            values = {key: response.choices[key].choice for key in questions}
            confidences = {key: response.choices[key].confidence for key in questions}
            relevant = applicable(values) if applicable else list(questions)
            accepted = all(
                (
                    confidences[key] >= (threshold or cfg["route_accept"])
                    for key in relevant
                )
            )
            self.emit(
                "jev.finished",
                decision_kind=kind,
                choices=values,
                confidences=confidences,
                elapsed_ms=(time.monotonic_ns() - started) / 1000000.0,
                accepted=accepted,
                raw=response.model_dump(mode="json"),
            )
            if accepted or not allow_fallback:
                return (values, confidences, "jev")
            if partial:
                return ({"route": "WAIT"}, {"route": 0.0}, "jev")
        except Exception as error:
            self.emit(
                "jev.timeout" if isinstance(error, TimeoutError) else "jev.error",
                decision_kind=kind,
                error=type(error).__name__,
                elapsed_ms=(time.monotonic_ns() - started) / 1000000.0,
            )
            if partial:
                return ({"route": "WAIT"}, {"route": 0.0}, "jev")
            if not allow_fallback:
                raise
        self.emit("fallback.started", decision_kind=kind)
        try:
            fallback_questions = questions
            defaults = {}
            if kind == "route":
                required = {"route"}
                if state.get("tasks") or any(
                    (
                        key == "STOP_SPEAKING"
                        for key in questions.get("control", {}).get("criteria", {})
                    )
                ):
                    if state.get("tasks") or state.get("playback_active"):
                        required.add("control")
                fallback_questions = {
                    key: question
                    for key, question in questions.items()
                    if key in required
                }
                defaults = {
                    key: "DISCARD" if key == "prefetch_resolution" else "NONE"
                    for key in questions
                    if key not in required
                }
                if any(
                    (
                        value not in questions[key]["criteria"]
                        for key, value in defaults.items()
                    )
                ):
                    raise ValueError("No safe inactive fallback choice")
            result = {
                **defaults,
                **await self.fallback.bounded_judgment(
                    state, fallback_questions, cfg["fallback_deadline_ms"]
                ),
            }
            self.emit("fallback.finished", decision_kind=kind, choices=result)
            return (result, {key: 1.0 for key in result}, "deepseek_fallback")
        except Exception as error:
            self.emit("fallback.failed", decision_kind=kind, error=type(error).__name__)
            raise

    async def relevance(self, kind, state, questions):
        cfg = self.config["jev"]
        state = {
            **state,
            "decision_kind": kind,
            "policy_version": self.config["policy_version"],
        }
        state, questions, removed, estimate = budget_decision(
            state, questions, cfg["max_context_tokens"]
        )
        started = time.monotonic_ns()
        self.emit(
            "jev.started",
            decision_kind=kind,
            state=state,
            questions=questions,
            dropped_context=removed,
            context_token_estimate=estimate,
        )
        try:
            async with asyncio.timeout(cfg["total_deadline_ms"] / 1000):
                response = await self.client.system_one(
                    state=state,
                    questions={
                        key: Noul(**question) for key, question in questions.items()
                    },
                    model=self.config["models"]["jev"],
                )
            probabilities = {key: response.answers[key].noul for key in questions}
            self.emit(
                "jev.finished",
                decision_kind=kind,
                probabilities=probabilities,
                elapsed_ms=(time.monotonic_ns() - started) / 1000000.0,
                raw=response.model_dump(mode="json"),
            )
            return probabilities
        except Exception as error:
            self.emit(
                "jev.timeout" if isinstance(error, TimeoutError) else "jev.error",
                decision_kind=kind,
                error=type(error).__name__,
                elapsed_ms=(time.monotonic_ns() - started) / 1000000.0,
            )
            raise

    async def route(
        self, transcript: TranscriptUpdate, epoch: int, context: dict
    ) -> Decision:
        partial = not transcript.is_final
        if not partial and self.config["jev"].get("interactive_routing"):
            from jarvis.core.interactive_route import route

            return await route(self, transcript, epoch, context)
        controls = {
            "NONE": "No control action applies.",
            "STOP_SPEAKING": "Stop current playback only; keep background tasks.",
        }
        tasks = context.get("tasks", [])
        for task in tasks:
            task_key = f"{task['task_id']}:{task['version']}"
            for action, explanation in {
                "STATUS": "Report progress",
                "REFINE": "Update constraints on the same goal",
                "REPLACE": "Replace with a different goal",
                "CANCEL": "Cancel this background task",
                "RESUME": "Present this task's result again using a new presentation",
            }.items():
                controls[f"{action}:{task_key}"] = (
                    f"{explanation} for task {task_key}; its objective and current status are in state.tasks."
                )
        skill_options = {"NONE": "No listed skill fits this input."}
        for skill in context.get("skill_candidates", []):
            skill_options[skill["id"]] = skill
        questions = {
            "route": {
                "instructions": self.prompt("route"),
                "criteria": PREFIX_ROUTES if partial else ROUTES,
            }
        }
        if not partial:
            questions.update(
                {
                    "control": {
                        "instructions": "If the current input controls an active task or playback, choose the combined action and exact task/version. Changing constraints on the same goal is REFINE. Stopping the old goal and requesting a different goal in its place is REPLACE. CANCEL applies when no replacement goal is requested. Describing background or saying to continue unchanged is NONE, not STATUS. Evaluate independently of the route answer. Otherwise NONE.",
                        "criteria": controls,
                    },
                    "skill": {
                        "instructions": "If a worker is needed for this input, which single skill is applicable? Otherwise NONE.",
                        "criteria": skill_options,
                    },
                    "missing_slot": {
                        "instructions": "If essential user-owned information is missing, which one slot should be clarified? World knowledge is not a missing user slot.",
                        "criteria": {
                            "NONE": "No essential user-owned information is missing.",
                            "TARGET": "Which existing task or object the user means.",
                            "CONSTRAINT": "An essential constraint or choice only the user knows.",
                            "GOAL": "What the user wants accomplished.",
                        },
                    },
                }
            )
            provisional = [t for t in tasks if t.get("provisional")]
            if provisional:
                questions["prefetch_resolution"] = {
                    "instructions": "Compare the complete final request against provisional task objectives and observations. REUSE only if all current constraints are covered; RESTART if useful work needs changing; DISCARD if no worker is needed.",
                    "criteria": {
                        "RESTART": "Final constraints require a fresh worker.",
                        "DISCARD": "The provisional task should not be used.",
                        **{
                            f"REUSE:{t['task_id']}:{t['version']}": f"Reuse provisional task {t['task_id']} version {t['version']}: {t['objective']}"
                            for t in provisional
                        },
                    },
                }
        state = {
            **context,
            "input": transcript.model_dump(),
            "event": "asr.partial" if partial else "asr.final",
        }

        def applicable(values):
            keys = ["route"]
            if values["route"] == "CONTROL":
                keys.append("control")
            if values["route"] == "WORKER" and "prefetch_resolution" in values:
                keys.append("prefetch_resolution")
            return keys

        try:
            values, confidences, origin = await self.judge(
                "route", state, questions, partial=partial, applicable=applicable
            )
            if (
                origin == "jev"
                and values["route"] == "CONTROL"
                and (confidences["control"] < self.config["jev"]["control_accept"])
            ):
                values = await self.fallback.bounded_judgment(
                    state, questions, self.config["jev"]["fallback_deadline_ms"]
                )
                origin = "deepseek_fallback"
            control = values.get("control", "NONE").split(":")
            return Decision(
                utterance_id=transcript.utterance_id,
                revision=transcript.revision,
                decision_epoch=epoch,
                route=values["route"],
                confidence=confidences.get("route", 0),
                control=control[0],
                target_task_id=control[1] if len(control) == 3 else None,
                target_version=int(control[2]) if len(control) == 3 else None,
                task_relation=control[0]
                if control[0] in ("REFINE", "REPLACE")
                else "NEW",
                skill_choice=values.get("skill", "NONE"),
                missing_slot=values.get("missing_slot", "NONE"),
                prefetch_resolution=values.get("prefetch_resolution", "DISCARD"),
                decision_origin=origin,
                raw={"choices": values, "confidences": confidences},
            )
        except Exception:
            return Decision(
                utterance_id=transcript.utterance_id,
                revision=transcript.revision,
                decision_epoch=epoch,
                route="WAIT" if partial else "CLARIFY",
                confidence=0,
                missing_slot="routing_unavailable",
                decision_origin="terminal_fallback",
            )

    async def delivery(self, result: dict, context: dict) -> str:
        if self.config["jev"].get("interactive_routing"):
            state = {
                "history": context.get("history", [])[-4:],
                "task": next(
                    (
                        t
                        for t in context.get("tasks", [])
                        if t["task_id"] == result["task_id"]
                    ),
                    None,
                ),
                "answer": result["spoken_answer"],
                "status": result["status"],
                "user_speaking": context.get("user_speaking", False),
                "playback_active": context.get("playback_active", False),
            }
            try:
                values, confidence, _ = await self.judge(
                    "delivery",
                    state,
                    {
                        "delivery": {
                            "instructions": "This is the current, noncancelled task result. Deliver its answer or follow-up question at a free conversational boundary. Unrelated intervening small talk does not cancel the user's request. Defer only when the user is speaking, another answer is playing, or the user explicitly asked to wait.",
                            "criteria": {
                                "DELIVER": "Give the ready answer or necessary follow-up now.",
                                "DEFER": "Keep it for a later boundary.",
                            },
                        }
                    },
                    allow_fallback=False,
                )
                return (
                    values["delivery"]
                    if confidence["delivery"] >= self.config["jev"]["route_accept"]
                    else "DEFER"
                )
            except Exception:
                return "DEFER"
        questions = {
            "delivery": {
                "instructions": self.prompt("delivery"),
                "criteria": {
                    "DELIVER": "Relevant result and appropriate conversational boundary.",
                    "DEFER": "Keep result for a later boundary, especially while the user speaks.",
                    "DISCARD": "Result no longer serves this conversation.",
                    "CLARIFY": "The user must supply an essential choice before proceeding.",
                },
            }
        }
        try:
            values, _, _ = await self.judge(
                "delivery", {**context, "result": result}, questions
            )
            return values["delivery"]
        except Exception:
            return "DEFER"

    async def select_memory(
        self, query: str, candidates: list[dict], context: dict
    ) -> list[dict]:
        if not candidates:
            return []
        questions = {
            str(candidate["id"]): {
                "instructions": self.prompt("memory")
                + f"\nJudge memory ID {candidate['id']} against the current request.",
                "criteria": {
                    "USE": "Relevant and supported.",
                    "IRRELEVANT": "Unrelated.",
                    "CONFLICT": "Contradicts more recent evidence.",
                    "INSUFFICIENT": "Does not support the needed fact.",
                },
            }
            for candidate in candidates
        }
        try:
            values, _, _ = await self.judge(
                "memory",
                {**context, "query": query, "memory_candidates": candidates},
                questions,
                applicable=lambda values: [
                    key for key, value in values.items() if value == "USE"
                ],
            )
            return [m for m in candidates if values.get(str(m["id"])) == "USE"][
                : self.config["memory"]["selected_k"]
            ]
        except Exception:
            return []

    async def close(self):
        await self.client.aclose()
