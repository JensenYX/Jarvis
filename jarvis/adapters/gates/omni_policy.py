from contextlib import contextmanager
import math
import torch
from torch import nn
from torch.nn import functional as F

ACTIONS = ("WAIT", "DIRECT", "DELEGATE")


class LoRALinear(nn.Module):
    def __init__(self, base, rank, alpha):
        super().__init__()
        self.base = base
        self.scale = alpha / rank
        self.enabled = True
        self.a = nn.Parameter(
            torch.empty(
                rank, base.in_features, device=base.weight.device, dtype=torch.float32
            )
        )
        self.b = nn.Parameter(
            torch.zeros(
                base.out_features, rank, device=base.weight.device, dtype=torch.float32
            )
        )
        nn.init.kaiming_uniform_(self.a, a=math.sqrt(5))

    @property
    def weight(self):
        return self.base.weight

    @property
    def bias(self):
        return self.base.bias

    def forward(self, x):
        out = self.base(x)
        if self.enabled:
            out = out + (F.linear(F.linear(x.float(), self.a), self.b) * self.scale).to(
                out.dtype
            )
        return out


def attach_lora(model, rank=8, alpha=16, targets=("q_proj", "v_proj", "o_proj")):
    found = []
    for name, module in list(model.llm.named_modules()):
        if isinstance(module, nn.Linear) and name.split(".")[-1] in targets:
            parent, attr = name.rsplit(".", 1)
            setattr(
                model.llm.get_submodule(parent), attr, LoRALinear(module, rank, alpha)
            )
            found.append(name)
    if not found:
        raise ValueError("No native LLM projections matched")
    return found


@contextmanager
def adapters(model, enabled):
    layers = [m for m in model.modules() if isinstance(m, LoRALinear)]
    previous = [m.enabled for m in layers]
    try:
        for m in layers:
            m.enabled = enabled
        yield
    finally:
        for m, value in zip(layers, previous):
            m.enabled = value


def adapter_state(model):
    return {
        n: p.detach().cpu().clone()
        for n, p in model.named_parameters()
        if n.endswith((".a", ".b"))
    }


def load_adapter(model, state):
    expected = adapter_state(model)
    if set(state) != set(expected) or any(
        (state[k].shape != expected[k].shape for k in state)
    ):
        raise ValueError("Adapter parameters do not exactly match this Omni policy")
    model.load_state_dict(state, strict=False)


class RoutePolicy:
    def __init__(self, model, tokenizer):
        self.model = model
        tokens = ["<|listen|>", "<|speak|>", "<delegate>"]
        self.listen, self.speak, self.delegate = [
            tokenizer.convert_tokens_to_ids(s) for s in tokens
        ]
        if len({self.listen, self.speak, self.delegate}) != 3:
            raise ValueError("Missing native control token")
        excluded = set(tokenizer.all_special_ids)
        excluded.update(
            (
                tokenizer.convert_tokens_to_ids(s)
                for s in [
                    "<unit>",
                    "</unit>",
                    "<delegate>",
                    "</delegate>",
                    "<backend>",
                    "</backend>",
                    "<think>",
                    "</think>",
                ]
            )
        )
        self.excluded = sorted(excluded)

    def forward(self, prefixes, *, grad=False):
        if not prefixes or any((len(p) == 0 for p in prefixes)):
            raise ValueError("Empty routing prefix")
        model = self.model
        weight = model.llm.model.embed_tokens.weight
        lengths = [len(p) for p in prefixes]
        x = torch.zeros(
            len(prefixes),
            max(lengths) + 1,
            weight.shape[1],
            device=model.device,
            dtype=weight.dtype,
        )
        mask = torch.zeros(x.shape[:2], device=model.device, dtype=torch.long)
        for i, (prefix, length) in enumerate(zip(prefixes, lengths)):
            x[i, :length] = prefix.to(device=model.device, dtype=weight.dtype)
            x[i, length] = weight[self.speak].detach()
            mask[i, : length + 1] = 1
        if grad:
            x.requires_grad_(True)
        h = model.llm.model(
            inputs_embeds=x, attention_mask=mask, use_cache=False, return_dict=True
        ).last_hidden_state
        rows = torch.arange(len(prefixes), device=model.device)
        ends = torch.tensor(lengths, device=model.device)
        return self.project(h[rows, ends - 1], h[rows, ends])

    def project(self, before_speak, after_speak):
        head = self.model.llm.lm_head.weight
        first = (
            F.linear(before_speak, head[[self.listen, self.speak]])
            .float()
            .log_softmax(-1)
        )
        following = F.linear(after_speak, head).float()
        delegate = following[:, self.delegate].clone()
        following[:, self.excluded] = -torch.inf
        second = torch.stack([following.logsumexp(-1), delegate], -1).log_softmax(-1)
        return torch.stack(
            [first[:, 0], first[:, 1] + second[:, 0], first[:, 1] + second[:, 1]], -1
        )
