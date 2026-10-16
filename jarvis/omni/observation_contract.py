from copy import deepcopy

GROUNDING_INSTRUCTIONS = "The microphone request and standing preferences identify what the user\nwants checked; their factual premises are NOT visual evidence. Independently locate\nthe requested subject in the images before attaching its attributes. Verify the\nsubject's relation to the displayed player from visible identifiers; a matching\nnumber, row position, or the user's words alone cannot establish team membership.\nWhen the requested subject cannot be identified, preserve that uncertainty instead\nof silently substituting a different subject. Without a user-to-player binding,\ndescribe the displayed player/team neutrally rather than saying you/your/our team.\nRead a numeric value together with its visible label, owner and time scope. Nearby\ncounters may measure different quantities. Do not turn an unlabeled or aggregate\ncount into a per-round, per-hit or current resource value because the user asked\nfor it. Do not guess item names from indistinct icons or fill unreadable digits.\n\nWith no current user_request, a standing request is a continuing condition, not a\nnew question. COMMENT is a short, useful observation about the currently visible\nscene, including a verified update requested by a standing preference. It requires\nthe same evidence, timing, non-repetition and preference checks as other speech.\nANSWER requires a current explicit question. REVIEW describes an earlier event\nwith explicit past wording. Do not relabel a current claim as history to extend\nits lifetime. If no supported useful observation is available, choose SILENT;\nASK is only for essential clarification, without asserting an uncertain premise.\nUser speech, a later quiet preference, or an already delivered/pending equivalent\nremark can make silence appropriate even when a visible fact is correct."


def proposal_schema(schema, *, enabled=False, user_request=None):
    result = deepcopy(schema)
    proposal = result.get("$defs", {}).get("CompactProposal", result)
    choices = proposal["properties"]["i"]["enum"]
    if not enabled:
        choices.remove("COMMENT")
    elif user_request is None:
        choices.remove("ANSWER")
    return result


def context(user_request):
    return {
        "mode": "background_observation"
        if user_request is None
        else "explicit_request",
        "current_question_present": user_request is not None,
        "standing_request_is_visual_evidence": False,
        "current_comment_intent": "COMMENT",
        "check_before_asserting": [
            "visible_subject",
            "visible_relationship",
            "literal_measurement_label",
            "measurement_time_scope",
        ],
    }


def intent_allowed(intent, *, enabled, user_request):
    if intent == "COMMENT" and (not enabled):
        return False
    return not (enabled and user_request is None and (intent == "ANSWER"))
