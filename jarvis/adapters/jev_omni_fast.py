from __future__ import annotations
import argparse
import base64
import io
import json
import threading
import time
from jarvis.adapters.jev_omni import Owner as SequentialOwner, create_app
from jarvis.paths import model_path
from jarvis.omni.decision.base import Answer, DecisionResult, UnsupportedCapability


def common_prefix(sequences):
    limit = min(map(len, sequences)) - 1
    for i in range(limit):
        if any((row[i] != sequences[0][i] for row in sequences[1:])):
            return i
    return limit


def pack_layout(sequences):
    prefix = common_prefix(sequences)
    if prefix < 1:
        raise ValueError("No token prefix can be shared")
    tokens = list(sequences[0][:prefix])
    positions = list(range(prefix))
    branches = [0] * prefix
    ends = []
    for branch, row in enumerate(sequences, 1):
        suffix = row[prefix:]
        tokens.extend(suffix)
        positions.extend(range(prefix, len(row)))
        branches.extend([branch] * len(suffix))
        ends.append(len(tokens) - 1)
    return {
        "tokens": tokens,
        "positions": positions,
        "branches": branches,
        "ends": ends,
        "prefix_tokens": prefix,
    }


def branch_masks(torch, positions, branches, vision_blocks, sliding_window):
    q, k = (positions[:, None], positions[None, :])
    branch_allowed = (branches[None, :] == 0) | (branches[:, None] == branches[None, :])
    same_vision = (vision_blocks[:, None] >= 0) & (
        vision_blocks[:, None] == vision_blocks[None, :]
    )
    causal = q >= k
    full = branch_allowed & (causal | same_vision)
    sliding = branch_allowed & (causal & (k > q - sliding_window) | same_vision)
    return {
        "full_attention": full[None, None],
        "sliding_attention": sliding[None, None],
    }


class Owner(SequentialOwner):
    def __init__(self, directory, variant="unified", *, max_packed_tokens=6144):
        if variant != "unified":
            raise ValueError(
                "The packed path is validated only for the unified release"
            )
        super().__init__(directory, variant)
        self.max_packed_tokens = max_packed_tokens
        config = self.classifier.model.config.text_config
        if config.use_bidirectional_attention != "vision" or set(
            config.layer_types
        ) != {"full_attention", "sliding_attention"}:
            raise ValueError("Unvalidated Gemma attention configuration")
        if config.num_kv_shared_layers:
            raise ValueError("KV sharing requires separate validation")
        self.metrics["execution"] = "independent_question_branches_shared_prefix_v1"
        self.metrics["max_packed_tokens"] = max_packed_tokens

    def prepare(self, request, images):
        classifier, torch = (self.classifier, self.torch)
        rendered, options = ([], [])
        for q in request.questions:
            if len(q.option_ids()) < 2:
                raise UnsupportedCapability("Jev-Omni requires at least two options")
            opts = (
                [
                    "False: the proposition is not supported",
                    "True: the proposition is supported",
                ]
                if q.primitive == "noul"
                else [f"{o.id}: {o.text}" for o in q.options]
            )
            options.append(opts)
            content = [{"type": "image", "image": image} for image in images]
            content.append(
                {
                    "type": "text",
                    "text": self.helper._prompt(
                        json.dumps(request.state, ensure_ascii=False),
                        q.instructions,
                        opts,
                    ),
                }
            )
            messages = [{"role": "user", "content": content}]
            rendered.append(
                classifier.processor.apply_chat_template(
                    messages,
                    add_generation_prompt=True,
                    tokenize=False,
                    enable_thinking=False,
                )
            )
            if len(rendered) == 1:
                first = classifier.processor.apply_chat_template(
                    messages,
                    add_generation_prompt=True,
                    tokenize=True,
                    return_dict=True,
                    return_tensors="pt",
                    enable_thinking=False,
                )
        raw_ids = classifier.processor.tokenizer(
            rendered, add_special_tokens=False, padding=False
        )["input_ids"]
        raw_prefix = common_prefix(raw_ids)
        tail = raw_ids[0][raw_prefix:]
        expanded = first["input_ids"][0].tolist()
        if expanded[-len(tail) :] != tail:
            raise ValueError(
                "Processor and text tokenizer disagree at the shared suffix"
            )
        prefix = expanded[: -len(tail)]
        media_prefix = len(prefix)
        if first["mm_token_type_ids"][0, media_prefix:].count_nonzero().item():
            raise ValueError("Media appeared inside a question branch")
        sequences = [prefix + row[raw_prefix:] for row in raw_ids]
        layout = pack_layout(sequences)
        first_types = first["mm_token_type_ids"][0].tolist()
        media_types = first_types[: layout["prefix_tokens"]] + [0] * (
            len(layout["tokens"]) - layout["prefix_tokens"]
        )
        if any(first_types[layout["prefix_tokens"] :]):
            raise ValueError("The shared prefix does not contain all media tokens")
        inputs = {
            k: v
            for k, v in first.items()
            if k not in {"input_ids", "attention_mask", "mm_token_type_ids"}
        }
        inputs.update(
            input_ids=torch.tensor([layout["tokens"]]),
            mm_token_type_ids=torch.tensor([media_types]),
            position_ids=torch.tensor([layout["positions"]]),
        )
        return (inputs, layout, options, sequences)

    def decide(self, request):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Jev state belongs to its owner thread")
        if request.audio:
            result = super().decide(request)
            result.raw["execution"] = "sequential_audio"
            return result
        from PIL import Image
        from transformers.models.gemma4_unified.modeling_gemma4_unified import (
            get_block_sequence_ids_for_mask,
        )

        if len(request.images) > 16:
            raise UnsupportedCapability("At most 16 causal frames")
        started = time.perf_counter()
        torch, classifier = (self.torch, self.classifier)
        images = []
        for image in request.images:
            binary = base64.b64decode(image.split(",", 1)[1], validate=True)
            if len(binary) > 4000000:
                raise ValueError("Image exceeds byte limit")
            picture = Image.open(io.BytesIO(binary))
            if picture.width * picture.height > 2073600:
                raise ValueError("Image exceeds pixel limit")
            images.append(picture.convert("RGB"))
        decoded = time.perf_counter()
        inputs, layout, options, sequences = self.prepare(request, images)
        prepared = time.perf_counter()
        if len(layout["tokens"]) > self.max_packed_tokens:
            result = super().decide(request)
            return result.model_copy(
                update={
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "raw": {
                        **result.raw,
                        "execution": "sequential_token_budget",
                        "packed_tokens": len(layout["tokens"]),
                    },
                }
            )
        inputs = {
            k: v.to("cuda", dtype=torch.bfloat16)
            if torch.is_floating_point(v)
            else v.to("cuda")
            for k, v in inputs.items()
        }
        branches = torch.tensor(layout["branches"], device="cuda")
        blocks = get_block_sequence_ids_for_mask(
            inputs["mm_token_type_ids"], torch.device("cuda")
        )[0]
        inputs["attention_mask"] = branch_masks(
            torch,
            inputs["position_ids"][0],
            branches,
            blocks,
            classifier.model.config.text_config.sliding_window,
        )
        torch.cuda.synchronize()
        masked = time.perf_counter()
        torch.cuda.reset_peak_memory_stats()
        classifier._capture.clear()
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
            outputs = classifier.model.model(
                **inputs, use_cache=False, return_dict=True
            )
            features = outputs.last_hidden_state[0, layout["ends"]].float()
            torch.cuda.synchronize()
            forwarded = time.perf_counter()
            logits = classifier.head(
                features, torch.tensor([len(o) for o in options], device="cuda")
            )
            probabilities = logits.softmax(-1).float().cpu().tolist()
            raw_logits = logits.float().cpu().tolist()
        answers = []
        for q, opts, probs, values in zip(
            request.questions, options, probabilities, raw_logits
        ):
            probs, values = (probs[: len(opts)], values[: len(opts)])
            best = max(range(len(probs)), key=probs.__getitem__)
            ids = q.option_ids()
            answers.append(
                Answer(
                    question_id=q.id,
                    primitive=q.primitive,
                    implementation="native" if q.primitive == "choice" else "emulated",
                    option_ids=ids,
                    raw_probs=probs,
                    raw_logits=values,
                    choice=ids[best] if q.primitive == "choice" else None,
                    expected_score=sum((i * p for i, p in enumerate(probs)))
                    if q.primitive == "score"
                    else None,
                    noul=probs[1] if q.primitive == "noul" else None,
                    native_confidence=max(probs),
                    confidence_kind="maximum_option_probability",
                    raw={
                        "prediction_index": best,
                        "options": opts,
                        "probabilities": probs,
                        "mapping": "false_true"
                        if q.primitive == "noul"
                        else "zero_based_rubric"
                        if q.primitive == "score"
                        else "ordered_option_ids",
                    },
                )
            )
        finished = time.perf_counter()
        return DecisionResult(
            backend="jev_omni",
            model="akhilaaa3/Jev-Omni",
            revision=self.revision,
            precision=self.precision,
            input_hash=request.input_hash(),
            answers=tuple(answers),
            elapsed_ms=(finished - started) * 1000,
            raw={
                "variant": self.variant,
                "owner_serialized": True,
                "execution": "shared_prefix_packed",
                "model_metrics": self.metrics,
                "peak_bytes": torch.cuda.max_memory_allocated(),
                "image_count": len(images),
                "tokens": {
                    "shared_prefix": layout["prefix_tokens"],
                    "packed": len(layout["tokens"]),
                    "sequential_total": sum(map(len, sequences)),
                    "per_question": list(map(len, sequences)),
                },
                "profile_ms": {
                    "decode_images": (decoded - started) * 1000,
                    "processor_tokenizer": (prepared - decoded) * 1000,
                    "transfer_and_masks": (masked - prepared) * 1000,
                    "decoder": (forwarded - masked) * 1000,
                    "head_and_serialization": (finished - forwarded) * 1000,
                },
            },
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8814)
    parser.add_argument("--max-packed-tokens", type=int, default=6144)
    args = parser.parse_args()
    import uvicorn

    def factory(directory, variant):
        return Owner(directory, variant, max_packed_tokens=args.max_packed_tokens)

    uvicorn.run(
        create_app(model_path("jev-omni"), owner_factory=factory),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
