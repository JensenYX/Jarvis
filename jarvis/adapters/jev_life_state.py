import argparse
import hashlib
import json
from pathlib import Path
from jarvis.adapters.jev_omni import create_app
from jarvis.paths import model_path, weight
from jarvis.adapters.weights import load_weights
from jarvis.adapters.jev_omni_fast import Owner
from jarvis.omni.decision.base import UnsupportedCapability
from jarvis.omni.life_state import QUESTION, STATES


def factory(checkpoint):
    checkpoint = checkpoint.resolve()
    config = json.loads(checkpoint.with_suffix(".json").read_text())
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    if digest != config["sha256"]:
        raise ValueError("Adapter does not match its configuration")

    class FactOwner(Owner):
        def __init__(self, directory, variant):
            super().__init__(directory, variant, max_packed_tokens=12288)
            import torch
            from jarvis.adapters.gates.omni_policy import LoRALinear, load_adapter

            model = self.classifier.model
            _, decoder = self.helper._find_backbone(model)
            for name, module in list(decoder.named_modules()):
                if isinstance(module, torch.nn.Linear) and name.rsplit(".", 1)[-1] in {
                    "q_proj",
                    "v_proj",
                    "o_proj",
                }:
                    parent, attribute = name.rsplit(".", 1)
                    setattr(
                        decoder.get_submodule(parent),
                        attribute,
                        LoRALinear(module, config["rank"], config["rank"] * 2),
                    )
            load_adapter(model, load_weights(checkpoint))
            model.requires_grad_(False)
            model.eval()
            self.metrics.update(
                adapter_sha256=digest, purpose="Pixel-only HUD life state only"
            )

        def decide(self, request):
            if any(
                (
                    q.id != "life_state"
                    or q.primitive != "choice"
                    or q.option_ids() != STATES
                    or (q.instructions != QUESTION)
                    for q in request.questions
                )
            ):
                raise UnsupportedCapability(
                    "Dedicated fact adapter cannot serve general decisions"
                )
            result = super().decide(request)
            result.raw["adapter_sha256"] = digest
            return result

    return FactOwner


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--checkpoint", type=Path, default=weight("life"))
    p.add_argument("--port", type=int, default=9998)
    args = p.parse_args()
    import uvicorn

    uvicorn.run(
        create_app(model_path("jev-omni"), owner_factory=factory(args.checkpoint)),
        host="127.0.0.1",
        port=args.port,
    )
