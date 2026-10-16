from __future__ import annotations
from jarvis.omni.protocol import EvidenceClaim, Observation


class BeliefState:
    def __init__(self, *, max_claims=256, max_observations=512):
        self.observations: dict[str, Observation] = {}
        self.claims: dict[str, EvidenceClaim] = {}
        self.invalidated: dict[str, str] = {}
        self.revision = 0
        self.max_claims, self.max_observations = (max_claims, max_observations)

    def prune(self, *, at_ms, pinned=()):
        pinned = set(pinned)

        def remove(key):
            self.claims.pop(key)
            self.invalidated.pop(key, None)

        for key, claim in tuple(self.claims.items()):
            if key not in pinned and (
                key in self.invalidated
                or (claim.valid_until_ms is not None and claim.valid_until_ms < at_ms)
            ):
                remove(key)

        def references():
            return {
                ref for claim in self.claims.values() for ref in claim.evidence_refs
            }

        for key in tuple(self.claims):
            if (
                len(self.claims) <= self.max_claims
                and len(references()) <= self.max_observations
            ):
                break
            if key not in pinned:
                remove(key)
        keep = references()
        self.observations = {
            key: value for key, value in self.observations.items() if key in keep
        }
        self.revision += 1

    def observe(self, observation):
        if observation.observation_id in self.observations:
            raise ValueError("Observation IDs are immutable")
        self.observations[observation.observation_id] = observation
        self.revision += 1

    def propose(self, claim: EvidenceClaim):
        if claim.claim_id in self.claims:
            raise ValueError("Claim IDs are immutable; append a correction")
        if claim.status != "unknown" and (not claim.evidence_refs):
            raise ValueError("A factual claim requires evidence")
        if any((ref not in self.observations for ref in claim.evidence_refs)):
            raise ValueError("Unknown evidence reference")
        if any((ref not in self.claims for ref in claim.contradicts)):
            raise ValueError("Unknown contradicted claim")
        self.claims[claim.claim_id] = claim
        self.revision += 1

    def invalidate(self, condition, *, game_session_id=None):
        for key, claim in self.claims.items():
            sources = [self.observations[ref] for ref in claim.evidence_refs]
            if condition in claim.invalidation_conditions and (
                game_session_id is None
                or any((o.stamp.game_session_id == game_session_id for o in sources))
            ):
                self.invalidated[key] = condition
        self.revision += 1

    def eligible(self, claim_id, *, at_ms, game_session_id=None, scene_epoch=None):
        claim = self.claims.get(claim_id)
        if (
            claim is None
            or claim_id in self.invalidated
            or claim.status in {"unknown", "contradicted"}
        ):
            return False
        if claim.valid_until_ms is not None and at_ms > claim.valid_until_ms:
            return False
        for ref in claim.evidence_refs:
            obs = self.observations[ref]
            if obs.source == "event_memory" and claim.temporal_mode != "historical":
                return False
            if obs.available_at_ms > at_ms or obs.stamp.capture_ts_ms > at_ms:
                return False
            if game_session_id is not None and (
                obs.stamp.game_session_id != game_session_id
                or (
                    claim.temporal_mode == "current"
                    and obs.stamp.scene_epoch != scene_epoch
                )
            ):
                return False
        return True
