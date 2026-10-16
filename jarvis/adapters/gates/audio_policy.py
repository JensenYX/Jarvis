import contextlib, math
import torch
from torch import nn
from torch.nn import functional as F

PROMPT = "Streaming Omni Conversation. Answer simple questions directly. For current information, files, calculations requested with tools, game state, game strategy, game actions, or changes to tasks, delegate to the harness. Ask for missing essential details. Ignore speech addressed to other people and background noise. Keep listening until there is enough information. The harness receives the original request."
GAME_CONTEXT = {
    "none": "",
    "pvz": "\nCurrent game: Plants vs Zombies. Connected. The user controls the game. Use the harness for live state, strategy or actions.",
    "voxel": "\nCurrent game: VoxeLibre (Minecraft-like). Connected. The user controls the game. Use the harness for live state, inventory, strategy or actions.",
}


class LoRALinear(nn.Module):
    def __init__(self, base, rank=8, alpha=16):
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
            owner = model.llm.get_submodule(parent)
            setattr(owner, attr, LoRALinear(module, rank, alpha))
            found.append(name)
    return found


@contextlib.contextmanager
def adapters(model, enabled):
    layers = [m for m in model.modules() if isinstance(m, LoRALinear)]
    before = [m.enabled for m in layers]
    for m in layers:
        m.enabled = enabled
    try:
        yield
    finally:
        for m, v in zip(layers, before):
            m.enabled = v


def adapter_state(model):
    return {
        n: p.detach().cpu()
        for n, p in model.named_parameters()
        if n.endswith((".a", ".b"))
    }


class RoutePolicy:
    def __init__(self, model, tokenizer, temperature=1.0):
        self.model = model
        self.t = tokenizer
        self.temperature = temperature
        self.listen = tokenizer.convert_tokens_to_ids("<|listen|>")
        self.speak = tokenizer.convert_tokens_to_ids("<|speak|>")
        self.delegate = tokenizer.convert_tokens_to_ids("<delegate>")
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

    def forward(self, prefixes, grad=False):
        device = self.model.device
        dtype = self.model.llm.model.embed_tokens.weight.dtype
        lengths = [len(p) + 1 for p in prefixes]
        size = max(lengths)
        bs = len(prefixes)
        x = torch.zeros(
            bs, size, self.model.config.hidden_size, device=device, dtype=dtype
        )
        mask = torch.zeros(bs, size, device=device, dtype=torch.long)
        for i, p in enumerate(prefixes):
            l = len(p)
            x[i, :l] = p.to(device=device, dtype=dtype)
            x[i, l] = self.model.llm.model.embed_tokens.weight[self.speak].detach()
            mask[i, : l + 1] = 1
        if grad:
            x.requires_grad_(True)
        h = self.model.llm.model(
            inputs_embeds=x, attention_mask=mask, use_cache=False, return_dict=True
        ).last_hidden_state
        indices = torch.tensor(
            [[i, l - 2] for i, l in enumerate(lengths)], device=device
        )
        h0 = h[indices[:, 0], indices[:, 1]]
        h1 = h[indices[:, 0], indices[:, 1] + 1]
        weight = self.model.llm.lm_head.weight
        z0 = F.linear(h0, weight[[self.listen, self.speak]]).float() / self.temperature
        z1 = F.linear(h1, weight).float() / self.temperature
        selected = z1[:, self.delegate].clone()
        public = z1.clone()
        public[:, self.excluded] = float("-inf")
        zgate = torch.stack([torch.logsumexp(public, dim=-1), selected], dim=-1)
        l0 = F.log_softmax(z0, dim=-1)
        l1 = F.log_softmax(zgate, dim=-1)
        logprobs = torch.stack(
            [l0[:, 0], l0[:, 1] + l1[:, 0], l0[:, 1] + l1[:, 1]], dim=-1
        )
        return (logprobs, {"first_logits": z0, "second_logits": zgate})
