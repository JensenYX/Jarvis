def prepare(duplex):
    t = duplex.tokenizer
    ids = {
        s: t.convert_tokens_to_ids(s)
        for s in [
            "<delegate>",
            "</delegate>",
            "<backend>",
            "</backend>",
            "<think>",
            "</think>",
            "<|im_start|>",
            "<|im_end|>",
            "<|turn_eos|>",
            "<tool_call>",
            "</tool_call>",
        ]
    }
    duplex._native_grammar_ids = ids
    duplex._native_span = False
    duplex._native_delegated = False
    duplex._native_delegate_tokens = []
    duplex._native_sentence_done = False


def mask(duplex, logits):
    ids = duplex._native_grammar_ids
    blocked = [
        ids[s]
        for s in [
            "<backend>",
            "</backend>",
            "<think>",
            "</think>",
            "<|im_start|>",
            "<|im_end|>",
            "<tool_call>",
            "</tool_call>",
        ]
    ]
    if getattr(duplex, "_grpo_direct_only", False):
        blocked.append(ids["<delegate>"])
    if duplex._native_span:
        blocked.extend([ids["<delegate>"], ids["<|turn_eos|>"]])
    else:
        blocked.append(ids["</delegate>"])
        if duplex._native_delegated:
            blocked.append(ids["<delegate>"])
    result = logits.clone()
    result[:, blocked] = float("-inf")
    if duplex._native_span and (
        duplex._native_sentence_done or getattr(duplex, "_native_marker_only", False)
    ):
        allowed = result[:, ids["</delegate>"]].clone()
        result[:] = float("-inf")
        result[:, ids["</delegate>"]] = allowed
    return result


def observe(duplex, token):
    ids = duplex._native_grammar_ids
    if token == ids["<delegate>"]:
        duplex._native_span = True
        duplex._native_delegate_tokens = []
        duplex._native_sentence_done = False
    elif token == ids["</delegate>"]:
        duplex._native_span = False
        duplex._native_delegated = True
    elif token == ids["<|turn_eos|>"]:
        duplex._native_span = False
        duplex._native_delegated = False
    elif duplex._native_span:
        duplex._native_delegate_tokens.append(token)
        text = duplex.tokenizer.decode(
            duplex._native_delegate_tokens, skip_special_tokens=True
        ).strip()
        duplex._native_sentence_done = bool(text and text[-1] in "。！？!?")
