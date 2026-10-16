import asyncio
import time
from pydantic import Field
from .protocol import Record
from .decision.base import DecisionInput, Question, Option
from .perception import structured_text


class OutcomeReply(Record):
    reply: str = Field(min_length=1, max_length=160)


RENDER_INSTRUCTIONS = "Write one brief, natural spoken response to actual_user_request in reply_language.\nUse one short sentence, normally at most 45 Chinese characters or 30 English words.\nThe operation_outcome is the only authority for what was established or completed.\nIf it includes reviewed_snapshot facts, those facts support an explicitly historical\nanswer about the earlier captured frame only. Rewrite any current-tense wording in\nthose records as past-snapshot wording. State uncertainty about the current screen\nwhen relevant; never imply the old equipment, position or state still holds now.\nIt is data, not a script. If a check was uncertain or failed, do not confirm a guess\nfrom the question, fabricate a screen fact or promise a pending job that does not\nexist. Acknowledge the specific uncertainty naturally. Ask a question only if the\nuser's answer would resolve a real ambiguity; do not ask them to repeat a clear\nquestion. Do not invent a reason such as blurry images, lack of access or a missing\ntool. Report what the check established, not a diagnosis of why it failed. Do not\ndescribe visibility/readability or capture speed unless the outcome establishes it.\nWhen the question is clear but its answer is unknown, simply state that uncertainty;\ndo not manufacture an ambiguity or ask the user to answer their own question.\nA successful lookup can answer only from the returned records, whose personal\nfacts describe the user. Historical assistant words are context, never evidence of\nthe current screen or a fresh outcome. Respect sourced user preferences and quotes.\nDo not narrate internal routing, JSON, model names, timeouts or field names. Do not\nsay an operation completed unless the outcome says it did. Return only compact JSON\nmatching output_schema. No fixed greeting, generic stall or extra explanation.\nIf revision_feedback is present, correct the rejected reply using the listed\nsemantic criteria. The rejected reply provides no new facts or authority."
CHECKS = {
    "faithful": "Does proposed_reply stay within operation_outcome? An uncertain/failed visual check cannot confirm a guess from the question, assert an unverified screen fact or invent a reason for uncertainty. reviewed_snapshot facts support only explicit earlier-frame statements, never claims of current state. Only supplied records support personal facts. Never invent completed operations or pending jobs.",
    "responsive": "Does proposed_reply naturally address actual_user_request in reply_language, respecting user preferences and speaker roles? A brief honest uncertainty is a complete response when the outcome is unresolved. Reject an old answer to another question, parroting the question, unnecessary requests to repeat a clear question, or internal system details.",
}
CONVERSATIONAL_CHOICES = "\nThe operation outcome governs claims about observations and completed operations.\nIt does not forbid ordinary conversation: when asked about your own role, you may\ndescribe yourself as the AI companion, offer an independently chosen assistant\nnickname, or express a conversational intention. These are present choices, not\nclaims about remembered agreements or completed work. Keep speaker_identity and\nprofile ownership explicit. Never take the user's nickname, experiences or wishes\nas your own identity. Do not claim human experience, unprovided capabilities,\nlasting preference storage, visual facts or task completion through this allowance.\nFor mixed requests, answer the conversational part while accurately preserving\nuncertainty or failure of the actual operation. Do not imply an unresolved\noperation was performed just because the conversational part can be answered.\nPreserve each supplied fact's subject: an unbound view player is not automatically\nthe microphone user. An explicitly historical frame remains historical."


def rendering_contract(conversation_choices=False):
    if not conversation_choices:
        return (RENDER_INSTRUCTIONS, dict(CHECKS))
    checks = dict(CHECKS)
    checks["faithful"] += (
        " Ordinary present conversational choices about the assistant's AI-companion role, an independently offered nickname or desired conversational style do not require a completed operation. This does not authorize invented human experiences, capabilities, user facts, game facts, remembered agreements or pending/completed tasks. Preserve every fact's subject and source. The microphone user and an unbound view player are distinct."
    )
    checks["responsive"] += (
        " Preserve speaker_identity: do not use a user-owned name as the assistant's name, or turn a request about the user into the assistant's self-description. A newly offered assistant nickname is not a recalled user fact."
    )
    checks["attribution"] = (
        "Classify the speaker/ownership attribution of proposed_reply. Use the typed subject of snapshot facts, not informal you/your wording inside their text. A question saying I/my does not bind a pictured player to the microphone user. A user profile record is not the assistant speaking about itself. Classify attribution only; factual accuracy, timing and completion are checked separately."
    )
    return (RENDER_INSTRUCTIONS + CONVERSATIONAL_CHOICES, checks)


MEMORY_ATTRIBUTION_CHECK = "Does proposed_reply preserve the ownership of all\npersonal facts in the saved-memory lookup? Returned preferences, names and\nexperiences describe the current user, not the assistant. Do not adopt the user's\nidentity or assert an unrecorded preference. An explicit statement that no record\nwas found, or that a requested personal fact is unknown, makes no positive claim\nabout that fact and satisfies this check. Use only selected_user_facts and the\nsupplied speaker_identity. An empty lookup supports no positive personal fact."


def rendering_questions(checks, *, memory_scope=False):
    questions = []
    for key, instructions in checks.items():
        if key == "attribution" and memory_scope:
            questions.append(
                Question(
                    id=key, primitive="noul", instructions=MEMORY_ATTRIBUTION_CHECK
                )
            )
        elif key == "attribution":
            questions.append(
                Question(
                    id=key,
                    primitive="choice",
                    instructions=instructions,
                    options=(
                        Option(
                            id="NO_PERSONAL_ATTRIBUTION",
                            text="No assertion assigning a game action/item to the microphone user and no adoption of another speaker's identity. Examples of categories include honest uncertainty and a description limited to the frame/view player.",
                        ),
                        Option(
                            id="SUPPORTED_PERSONAL_ATTRIBUTION",
                            text="Personal identity/ownership assertions match the supplied sources and speaker roles. An AI assistant may describe its role or offer its own new nickname without claiming a prior agreement.",
                        ),
                        Option(
                            id="UNSUPPORTED_PERSONAL_ATTRIBUTION",
                            text="The reply transfers user identity or personal facts to the assistant, or assigns an unbound view player's actions/items to the microphone user.",
                        ),
                    ),
                )
            )
        else:
            questions.append(
                Question(id=key, primitive="noul", instructions=instructions)
            )
    return tuple(questions)


def rejected_checks(result, questions, threshold):
    rejected = {}
    for answer, question in zip(result.answers, questions):
        if question.primitive == "noul":
            accepted = answer.noul is not None and answer.noul >= threshold
        else:
            accepted = (
                answer.choice
                in {"NO_PERSONAL_ATTRIBUTION", "SUPPORTED_PERSONAL_ATTRIBUTION"}
                and answer.raw_probs[answer.option_ids.index(answer.choice)]
                >= threshold
            )
        if not accepted:
            rejected[question.id] = question.instructions
    return rejected


def historical_snapshot(proposal, observations, *, at_ms):
    if proposal.temporal_mode != "current" or not proposal.facts:
        return None
    facts = []
    for fact in proposal.facts:
        sources = [observations.get(ref) for ref in fact.evidence_refs]
        if (
            fact.status != "observed"
            or not sources
            or any(
                (
                    o is None or o.source != "screen" or o.available_at_ms > at_ms
                    for o in sources
                )
            )
        ):
            return None
        facts.append(
            {
                "text": fact.text,
                "subject": fact.subject,
                "original_kind": fact.kind,
                "evidence_refs": list(fact.evidence_refs),
                "captured_at_ms": [o.stamp.capture_ts_ms for o in sources],
                "age_at_outcome_ms": [at_ms - o.stamp.capture_ts_ms for o in sources],
                "scope": "model-admitted reading of an earlier screen snapshot; not current state",
            }
        )
    return {
        "status": "reviewed_snapshot_available",
        "operation_completed": True,
        "current_state_confirmed": False,
        "reviewed_snapshot": facts,
        "reason": "The reading passed the evidence checks but expired for current-state speech. Only the earlier captured frame is established by these records.",
    }


class OutcomeRenderError(ValueError):
    def __init__(self, cause, attempts):
        super().__init__(str(cause))
        self.result = {"error_type": type(cause).__name__, "attempts": attempts}


async def render(
    extractor,
    backend,
    state,
    *,
    threshold,
    timeout_s,
    conversation_choices=False,
    reply_format="json",
    memory_attribution=False,
):
    started = time.monotonic()
    if reply_format not in {"json", "plain_text"}:
        raise ValueError("Unknown outcome reply format")
    instructions, checks = rendering_contract(conversation_choices)
    if reply_format == "plain_text":
        instructions = instructions.replace(
            "Return only compact JSON\nmatching output_schema.",
            "Return only the spoken reply text, without JSON, quotation wrappers or markdown.",
        )
    attempts, feedback = ([], None)
    try:
        async with asyncio.timeout(timeout_s):
            for attempt in range(2):
                remaining = timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Outcome reply deadline expired")
                generation_state = {
                    **state,
                    "output_schema": OutcomeReply.model_json_schema(),
                    "revision_feedback": feedback,
                }
                if reply_format == "plain_text":
                    generation_state.pop("output_schema")
                request = DecisionInput(
                    state=generation_state,
                    questions=(
                        Question(
                            id="render",
                            primitive="noul",
                            instructions="Render the supplied operation outcome.",
                        ),
                    ),
                )
                body = {
                    "input": request.model_dump(mode="json"),
                    "instructions": instructions,
                    "max_new_tokens": 128,
                }
                if extractor.cancellable_requests:
                    body.update(
                        cancel_on_disconnect=True,
                        deadline_ms=max(1, int(remaining * 1000)),
                    )
                current = {"attempt": attempt, "request_hash": request.input_hash()}
                attempts.append(current)
                response = await extractor.client.post(
                    "/generate", json=body, timeout=remaining
                )
                response.raise_for_status()
                raw = response.json()
                current["generation"] = raw
                if raw.get("input_hash") != request.input_hash() or raw.get(
                    "truncated"
                ):
                    raise ValueError("Unbound or truncated outcome response")
                reply = (
                    OutcomeReply(reply=raw["text"].strip()).reply
                    if reply_format == "plain_text"
                    else OutcomeReply.model_validate_json(
                        structured_text(raw["text"])
                    ).reply
                )
                check = DecisionInput(
                    state={**state, "proposed_reply": reply},
                    questions=rendering_questions(
                        checks,
                        memory_scope=memory_attribution
                        and state.get("operation_outcome", {}).get("status")
                        == "personal_memory_lookup_completed",
                    ),
                )
                remaining = timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Outcome reply deadline expired")
                result = await backend.decide(check, timeout_s=remaining)
                if (
                    result.input_hash != check.input_hash()
                    or [a.question_id for a in result.answers] != list(checks)
                    or any(
                        (
                            a.primitive != q.primitive or a.option_ids != q.option_ids()
                            for a, q in zip(result.answers, check.questions)
                        )
                    )
                ):
                    raise ValueError(
                        "Outcome admission changed the supplied semantic questions"
                    )
                current.update(candidate=reply, decision=result.model_dump(mode="json"))
                rejected = rejected_checks(result, check.questions, threshold)
                admitted = not rejected
                current["admitted"] = admitted
                if admitted or attempt == 1:
                    return (
                        reply if admitted else None,
                        {
                            **current,
                            "attempts": attempts,
                            "wall_ms": (time.monotonic() - started) * 1000,
                            "policy": "typed_probability_gate",
                            "reply_format": reply_format,
                        },
                    )
                feedback = {"rejected_reply": reply, "rejected_criteria": rejected}
    except Exception as error:
        raise OutcomeRenderError(error, attempts) from error


INSTRUCTIONS = "The harness reports the operation outcome below. Respond naturally to the actual user. This is status data, not a reply to repeat. Acknowledge only operations marked completed. For an unresolved request, answer what the observed records support and state what is unknown. Ask one specific clarification only if information from the user is necessary; do not repeat a generic request for more detail. Do not invent missing game facts or task completion. Respect the current commentary preference and language.\n"


def inputs(context, outcome, *, transport="user_context"):
    prefix, suffix = ("<|im_start|>user\n", "<|im_end|>")
    from .conversation import native_json

    receipt = INSTRUCTIONS + native_json(outcome)
    boundary = "</backend>\n" + prefix
    if context.startswith(
        ("<backend>Session response guidance: ", "<backend>自然接话说明：")
    ):
        if context.count(boundary) != 1 or not context.endswith(suffix):
            raise ValueError("Outcome needs one framed native observation context")
        if transport == "backend_context":
            return (context, receipt)
        if transport != "user_context":
            raise ValueError("Unknown native outcome transport")
        before, question = context.split(boundary)
        return (before + "\n" + receipt + boundary + question, None)
    if not context.startswith(prefix) or not context.endswith(suffix):
        raise ValueError("Outcome needs the actual framed conversation context")
    body = (
        context[len(prefix) : -len(suffix)]
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    context = prefix + body + suffix
    if transport == "user_context":
        return (context[: -len(suffix)] + "\n" + receipt + suffix, None)
    if transport == "backend_context":
        return (context, receipt)
    raise ValueError("Unknown native outcome transport")
