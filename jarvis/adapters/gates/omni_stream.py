import torch
from collections import deque
from jarvis.adapters.gates.omni_policy import adapters


class CausalRouteWindow:
    def __init__(self, system, max_units):
        if type(max_units) is not int or max_units < 2:
            raise ValueError("A route window requires at least two complete units")
        self.system = system.detach().cpu().clone()
        self.units = deque(maxlen=max_units)
        self.end_sample = -1

    def append(self, inputs, terminator, end_sample):
        if end_sample <= self.end_sample:
            raise ValueError("Route window input must advance causally")
        if not len(inputs) or not len(terminator):
            raise ValueError("A route unit must contain input and a terminator")
        inputs = inputs.detach().cpu().clone()
        terminator = terminator.detach().cpu().clone()
        if len(self.units) == self.units.maxlen:
            self.units.popleft()
        prefix = torch.cat([self.system, *self.units, inputs])
        self.units.append(torch.cat([inputs, terminator]))
        self.end_sample = end_sample
        return prefix


class NativeRouteSession:
    def __init__(self, policy, *, adapter_enabled=True, max_tokens=4096):
        if max_tokens < 2:
            raise ValueError("Decision context capacity must be positive")
        self.policy = policy
        self.adapter_enabled = adapter_enabled
        self.max_tokens = max_tokens
        self.cache = None
        self.prefix = None
        self.end_sample = -1
        self.broken = False

    def step(self, prefix, end_sample):
        if self.broken:
            raise RuntimeError("Failed route session must be reset")
        if len(prefix) > self.max_tokens:
            raise ValueError(
                "Decision context capacity reached; explicit context reset required"
            )
        previous = 0 if self.prefix is None else len(self.prefix)
        if end_sample <= self.end_sample or len(prefix) <= previous:
            raise ValueError("Route input must advance causally")
        if self.prefix is not None and (
            not torch.equal(prefix[:previous], self.prefix)
        ):
            raise ValueError("Earlier causal input changed")
        model, policy = (self.policy.model, self.policy)
        model.llm.model.eval()
        embeds = prefix[previous:].to(
            device=model.device, dtype=model.llm.model.embed_tokens.weight.dtype
        )
        try:
            with torch.no_grad(), adapters(model, self.adapter_enabled):
                speak = model.llm.model.embed_tokens.weight[policy.speak].reshape(1, -1)
                combined = torch.cat([embeds, speak], dim=0).unsqueeze(0)
                try:
                    output = model.llm.model(
                        inputs_embeds=combined,
                        past_key_values=self.cache,
                        use_cache=True,
                        return_dict=True,
                    )
                    self.cache = output.past_key_values
                    if self.cache.get_seq_length() != len(prefix) + 1 or not hasattr(
                        self.cache, "crop"
                    ):
                        raise ValueError(
                            "Decision KV cannot bind/crop the speculative SPEAK"
                        )
                    probabilities = policy.project(
                        output.last_hidden_state[:, -2], output.last_hidden_state[:, -1]
                    ).exp()
                finally:
                    if self.cache is not None and hasattr(self.cache, "crop"):
                        self.cache.crop(len(prefix))
                if self.cache.get_seq_length() != len(prefix):
                    raise ValueError("Speculative decision contaminated later input")
                self.prefix = prefix.detach().cpu().clone()
                self.end_sample = end_sample
                return probabilities
        except Exception:
            self.cache = None
            self.broken = True
            raise


class FullPrefixRouteSession(NativeRouteSession):
    def step(self, prefix, end_sample):
        if self.broken:
            raise RuntimeError("Failed route session must be reset")
        if len(prefix) > self.max_tokens:
            raise ValueError(
                "Decision context capacity reached; explicit context reset required"
            )
        previous = 0 if self.prefix is None else len(self.prefix)
        if end_sample <= self.end_sample or len(prefix) <= previous:
            raise ValueError("Route input must advance causally")
        if self.prefix is not None and (
            not torch.equal(prefix[:previous], self.prefix)
        ):
            raise ValueError("Earlier causal input changed")
        try:
            self.policy.model.llm.model.eval()
            with torch.no_grad(), adapters(self.policy.model, self.adapter_enabled):
                probabilities = self.policy.forward([prefix]).exp()
            self.prefix = prefix.detach().cpu().clone()
            self.end_sample = end_sample
            return probabilities
        except Exception:
            self.broken = True
            raise
