def complete_text_condition(duplex, results, native_convert):
    if not getattr(duplex, "_harness_tts_text_end", False) or not any(
        (row[2] for row in results)
    ):
        return native_convert(duplex, results)
    open_id = duplex.tokenizer.convert_tokens_to_ids("<delegate>")
    close_id = duplex.tokenizer.convert_tokens_to_ids("</delegate>")
    skip = getattr(duplex, "_delegate_tts_skip", False)
    kept = [row[0] for row in results]
    if open_id is not None and (skip or open_id in kept):
        kept = []
        for row in results:
            if row[0] == open_id:
                skip = True
            elif row[0] == close_id:
                skip = False
            elif not skip:
                kept.append(row[0])
    condition = native_convert(duplex, results)
    if condition is None:
        return None
    if condition.shape[1] != len(kept) + 1:
        raise ValueError("Native TTS conditioning positions changed")
    import torch

    text = condition[:, :-1]
    if kept and kept[-1] == duplex.turn_eos_token_id:
        text = text[:, :-1]
    end = duplex.model.tts.emb_text(
        torch.tensor(
            [duplex.model.tts.config.text_eos_token_id],
            dtype=torch.long,
            device=duplex.device,
        )
    ).unsqueeze(0)
    return torch.cat([text, end, condition[:, -1:]], dim=1)
