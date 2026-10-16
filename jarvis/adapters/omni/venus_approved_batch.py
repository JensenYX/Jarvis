from jarvis.adapters.omni.venus_patches import approved_token


def approved_batch(duplex, budget):
    ids = []
    for index in range(budget - 1):
        token = approved_token(duplex, index)
        ids.append(token)
        if token in duplex.chunk_terminator_token_ids:
            break
    logits, hidden = duplex.decoder.feed(
        duplex.decoder.embed_tokens(ids), return_logits=True
    )
    return (ids, logits, hidden)
