async def resolve(controller, current_words, offer):
    judgment = await controller.judge_typed(
        "voxel_offer_reference_needed",
        {"current_words": current_words, "previous_offer": offer.get("request", "")},
        {
            "reference": {
                "type": "choice",
                "instructions": "Does the current utterance itself identify the requested game result/product? A named product such as make ONE wooden sword, even if similar to an older offer for THREE swords, is SELF_CONTAINED. The current count always supersedes an offered count. Only use NEEDS_REFERENCE for genuine ellipsis such as do that, make one of those, continue, or craft it. Do not treat similarity to an old offer as a missing reference.",
                "criteria": {
                    "SELF_CONTAINED": "Current words identify the requested result; preserve those words exactly",
                    "NEEDS_REFERENCE": "An omitted result or product must be supplied by the prior offer",
                },
            }
        },
        deadline_ms=1800,
    )
    selected = judgment["reference"]
    if selected["choice"] == "SELF_CONTAINED" and selected["confidence"] >= 0.8:
        return (current_words, judgment)
    return (
        "当前唯一执行要求："
        + current_words
        + "\n先前提议（只补当前缺失的物品或目标；当前明确的数量、限制和改动覆盖先前提议）："
        + offer["request"],
        judgment,
    )
