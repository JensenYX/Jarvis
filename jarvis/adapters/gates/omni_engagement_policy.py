import torch
from torch.nn import functional as F
from jarvis.adapters.gates.omni_policy import RoutePolicy, adapters
from jarvis.adapters.gates.omni_stream import NativeRouteSession

ACTIONS = ("WAIT", "DIRECT", "DELEGATE_REQUEST", "DELEGATE_SCENE")


class EngagementPolicy(RoutePolicy):
    def __init__(self, model, tokenizer):
        super().__init__(model, tokenizer)
        tokens = [
            tokenizer.encode(word, add_special_tokens=False)
            for word in ("request", "scene")
        ]
        if any((len(value) != 1 for value in tokens)) or tokens[0] == tokens[1]:
            raise ValueError(
                "Delegate origin must use distinct single native vocabulary tokens"
            )
        self.origins = [value[0] for value in tokens]

    def project_engagement(self, before_speak, after_speak, after_delegate):
        native = super().project(before_speak, after_speak)
        origin = (
            F.linear(after_delegate, self.model.llm.lm_head.weight[self.origins])
            .float()
            .log_softmax(-1)
        )
        return torch.cat([native[:, :2], native[:, 2:3] + origin], -1)

    def forward(self, prefixes, *, grad=False):
        if not prefixes or any((len(p) == 0 for p in prefixes)):
            raise ValueError("Empty engagement prefix")
        model = self.model
        weight = model.llm.model.embed_tokens.weight
        lengths = [len(p) for p in prefixes]
        x = torch.zeros(
            len(prefixes),
            max(lengths) + 2,
            weight.shape[1],
            device=model.device,
            dtype=weight.dtype,
        )
        mask = torch.zeros(x.shape[:2], device=model.device, dtype=torch.long)
        for i, (prefix, length) in enumerate(zip(prefixes, lengths)):
            x[i, :length] = prefix.to(device=model.device, dtype=weight.dtype)
            x[i, length : length + 2] = weight[[self.speak, self.delegate]].detach()
            mask[i, : length + 2] = 1
        if grad:
            x.requires_grad_(True)
        hidden = model.llm.model(
            inputs_embeds=x, attention_mask=mask, use_cache=False, return_dict=True
        ).last_hidden_state
        rows = torch.arange(len(prefixes), device=model.device)
        ends = torch.tensor(lengths, device=model.device)
        return self.project_engagement(
            hidden[rows, ends - 1], hidden[rows, ends], hidden[rows, ends + 1]
        )


class EngagementSession(NativeRouteSession):
    def step(self, prefix, end_sample):
        if self.broken:
            raise RuntimeError("Failed engagement session must be reset")
        if len(prefix) > self.max_tokens:
            raise ValueError("Engagement context capacity reached")
        previous = 0 if self.prefix is None else len(self.prefix)
        if end_sample <= self.end_sample or len(prefix) <= previous:
            raise ValueError("Engagement input must advance causally")
        if self.prefix is not None and (
            not torch.equal(prefix[:previous], self.prefix)
        ):
            raise ValueError("Earlier causal input changed")
        model, policy = (self.policy.model, self.policy)
        model.llm.model.eval()
        weight = model.llm.model.embed_tokens.weight
        embeds = prefix[previous:].to(device=model.device, dtype=weight.dtype)
        try:
            with torch.no_grad(), adapters(model, self.adapter_enabled):
                hypothetical = weight[[policy.speak, policy.delegate]]
                try:
                    output = model.llm.model(
                        inputs_embeds=torch.cat([embeds, hypothetical]).unsqueeze(0),
                        past_key_values=self.cache,
                        use_cache=True,
                        return_dict=True,
                    )
                    self.cache = output.past_key_values
                    if self.cache.get_seq_length() != len(prefix) + 2 or not hasattr(
                        self.cache, "crop"
                    ):
                        raise ValueError(
                            "Engagement KV cannot bind/crop native delegate origin"
                        )
                    h = output.last_hidden_state
                    probabilities = policy.project_engagement(
                        h[:, -3], h[:, -2], h[:, -1]
                    ).exp()
                finally:
                    if self.cache is not None and hasattr(self.cache, "crop"):
                        self.cache.crop(len(prefix))
                if self.cache.get_seq_length() != len(prefix):
                    raise ValueError("Hypothetical delegate contaminated later input")
                self.prefix = prefix.detach().cpu().clone()
                self.end_sample = end_sample
                return probabilities
        except Exception:
            self.cache = None
            self.broken = True
            raise
