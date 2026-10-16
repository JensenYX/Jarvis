from __future__ import annotations
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MESSAGE_BYTES = 1048576
Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern="^[A-Za-z0-9_.:-]+$")
]
GameObjectId = Annotated[str, Field(min_length=1, max_length=128)]
Label = Annotated[str, Field(max_length=160)]
Description = Annotated[str, Field(max_length=600)]
Counter = Annotated[int, Field(ge=0, le=2**53 - 1)]
Milliseconds = Annotated[float, Field(ge=0, le=1e16, allow_inf_nan=False)]
GameId = Literal["pvz_web",]
ActionKind = Literal[
    "PLANT", "COLLECT_SUN", "SHOVEL", "SELECT_SEED", "DESELECT_SEED", "WAIT"
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Seed(Strict):
    slot: Annotated[int, Field(ge=1, le=16)]
    seed_id: Identifier
    plant_type: Identifier
    name: Label
    cost: Annotated[int, Field(ge=0, le=10000)] | None = None
    ready: bool
    cooldown_ready: bool | None = None
    affordable: bool | None = None
    cooldown_ms: Milliseconds | None = None
    role: Literal["economy", "attack", "block", "instant", "other"] = "other"
    effect: Description = ""


class SeedChoice(Strict):
    plant_type: Identifier
    name: Label
    cost: Annotated[int, Field(ge=0, le=10000)] | None = None
    selected: bool
    role: Literal["economy", "attack", "block", "instant", "other"] = "other"
    effect: Description = ""


class Sun(Strict):
    entity_id: Identifier
    amount: Annotated[int, Field(ge=1, le=10000)]


AutoplayScope = Literal["continuous", "timed", "level", "task"]


class AutoplayState(Strict):
    autoplay_id: Identifier
    scope: AutoplayScope
    remaining_ms: Annotated[int, Field(ge=0, le=43200000)] | None = None


class Plant(Strict):
    entity_id: Identifier
    plant_type: Identifier
    name: Label
    row: Annotated[int, Field(ge=1, le=6)]
    col: Annotated[int, Field(ge=1, le=9)]
    layer: Annotated[int, Field(ge=0, le=4)] = 1
    hp: Annotated[int, Field(ge=0, le=1000000)] | None = None
    state: Label = ""
    role: Literal["economy", "attack", "block", "instant", "other"] = "other"
    x: Annotated[float, Field(ge=-500, le=3000, allow_inf_nan=False)] | None = None


class Zombie(Strict):
    entity_id: Identifier
    zombie_type: Identifier
    name: Label
    row: Annotated[int, Field(ge=1, le=6)]
    x: Annotated[float, Field(ge=-500, le=3000, allow_inf_nan=False)]
    zone: Literal["near", "middle", "far"]
    hp: Annotated[int, Field(ge=0, le=1000000)] | None = None
    state: Label = ""
    observed_speed: (
        Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] | None
    ) = None


class PvZView(Strict):
    game: Literal["pvz_web"]
    scene: Identifier
    rows: Annotated[int, Field(ge=1, le=6)]
    cols: Annotated[int, Field(ge=1, le=9)]
    sun: Annotated[int, Field(ge=0, le=1000000)] | None = None
    enhanced_telemetry: bool = False
    battle_outcome: Literal["unknown", "victory", "defeat"] = "unknown"
    seed_bank: Annotated[list[Seed], Field(max_length=16)] = []
    plants: Annotated[list[Plant], Field(max_length=256)] = []
    zombies: Annotated[list[Zombie], Field(max_length=256)] = []
    mowers: Annotated[list[bool | None], Field(max_length=6)] = []
    selected_seed: Identifier | None = None
    auto_collect_sun: bool = False
    normal_rules: bool = True
    level_name: Label = ""
    plantable_rows: Annotated[
        list[Annotated[int, Field(ge=1, le=6)]], Field(max_length=6)
    ] = []
    ui_stage: Literal[
        "unknown",
        "loading",
        "title",
        "menu",
        "select_cards",
        "preparing",
        "playing",
        "paused",
        "ended",
        "unsupported",
    ] = "unknown"
    tutorial_text: Description = ""
    card_choices: Annotated[list[SeedChoice], Field(max_length=64)] = []
    suns: Annotated[list[Sun], Field(max_length=128)] = []
    column_x: Annotated[
        list[Annotated[float, Field(ge=-500, le=3000, allow_inf_nan=False)]],
        Field(max_length=9),
    ] = []
    seed_selection_limit: Annotated[int, Field(ge=1, le=16)] = 10
    input_state: Literal["idle", "plant_selected", "shovel_selected", "held"] = "idle"

    @model_validator(mode="after")
    def public_fields(self):
        if not self.enhanced_telemetry:
            if any((p.hp is not None for p in self.plants)) or any(
                (z.hp is not None for z in self.zombies)
            ):
                raise ValueError("Exact health requires visible enhanced telemetry")
        ids = [p.entity_id for p in self.plants] + [z.entity_id for z in self.zombies]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate entity identity")
        if any((p.row > self.rows or p.col > self.cols for p in self.plants)) or any(
            (z.row > self.rows for z in self.zombies)
        ):
            raise ValueError("Entity outside supported board")
        if len(self.plantable_rows) != len(set(self.plantable_rows)) or any(
            (r > self.rows for r in self.plantable_rows)
        ):
            raise ValueError("Invalid plantable row list")
        return self


class ActionFacts(Strict):
    seed_id: Identifier | None = None
    plant_type: Identifier | None = None
    row: Annotated[int, Field(ge=1, le=6)] | None = None
    col: Annotated[int, Field(ge=1, le=9)] | None = None
    cost: Annotated[int, Field(ge=-9, le=100000)] | None = None
    target_id: Identifier | None = None
    effect: Description = ""
    requires_human_continuation: bool = False


class Candidate(Strict):
    action_id: Identifier
    label: Label
    kind: ActionKind
    facts: ActionFacts
    dependencies: Annotated[list[Identifier], Field(max_length=24)] = []


class Capabilities(Strict):
    mode: Literal["observation_only", "guarded_control"] = "observation_only"
    actions: Annotated[list[ActionKind], Field(max_length=13)] = []
    native_input_modes: Annotated[
        list[Literal["keyboard_mouse", "pointer"]], Field(max_length=2)
    ] = []
    observation_fields: Annotated[list[Identifier], Field(max_length=64)] = []
    auto_continue: bool = False
    continuation_policy: Literal["not_applicable", "manual_after_first_action"] = (
        "not_applicable"
    )


class RecentEvent(Strict):
    kind: Literal[
        "select_seed",
        "plant",
        "shovel",
        "collect_sun",
        "entity_lost",
        "threat",
        "pause",
        "resume",
        "focus",
        "reclaim",
        "lifecycle",
        "unknown_change",
    ]
    actor: Literal["human", "jev", "engine", "unknown"]
    text: Description = ""
    row: Annotated[int, Field(ge=1, le=6)] | None = None
    col: Annotated[int, Field(ge=1, le=9)] | None = None


class Snapshot(Strict):
    snapshot_seq: Counter
    decision_id: Identifier
    control_epoch: Counter
    candidate_set_id: Identifier
    candidate_coverage: Literal["complete", "filtered_legal_subset", "manual_only"]
    total_legal_actions: Annotated[int, Field(ge=0, le=100000)]
    phase: Literal[
        "menu",
        "playing",
        "combat",
        "reward",
        "map",
        "rest",
        "paused",
        "ended",
        "unsupported",
    ]
    paused: bool
    focused: bool
    awaiting_action: bool
    observed_at_host_mono_ms: Milliseconds
    view: PvZView
    legal_actions: Annotated[list[Candidate], Field(max_length=64)]
    field_quality: Annotated[
        dict[Identifier, Literal["observed", "enhanced", "unknown", "unsupported"]],
        Field(max_length=64),
    ] = {}
    nonce: Identifier | None = None
    recent_events: Annotated[list[RecentEvent], Field(max_length=2)] = []
    autoplay: AutoplayState | None = None
    control_reason: Label = ""

    @model_validator(mode="after")
    def candidates_valid(self):
        ids = [a.action_id for a in self.legal_actions]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate candidate ID")
        if len(ids) > self.total_legal_actions:
            raise ValueError("Invalid candidate coverage")
        if (
            self.view.game == "pvz_web"
            and self.view.battle_outcome != "unknown"
            and (self.phase != "ended")
        ):
            raise ValueError("Battle outcome requires an ended phase")
        seed_window = (
            self.view.game == "pvz_web"
            and self.view.ui_stage == "select_cards"
            and (self.phase == "menu")
        )
        if self.awaiting_action and (
            self.paused
            or not self.focused
            or self.phase in {"ended", "unsupported", "paused"}
            or (self.phase == "menu" and (not seed_window))
        ):
            raise ValueError("No control window in an unavailable phase")
        if (
            self.view.game == "pvz_web"
            and self.awaiting_action
            and (self.view.auto_collect_sun or not self.view.normal_rules)
        ):
            raise ValueError("Assisted or modified rules are observation-only")
        if self.view.game != "pvz_web" and self.autoplay is not None:
            raise ValueError("Autoplay is PvZ-only")
        allowed = (
            {"SELECT_SEED", "DESELECT_SEED", "WAIT"}
            if seed_window
            else {"PLANT", "SHOVEL", "COLLECT_SUN", "WAIT"}
            if self.view.game == "pvz_web"
            else {
                "PLAY_CARD",
                "END_TURN",
                "CHOOSE_CARD_REWARD",
                "SKIP_CARD_REWARD",
                "CHOOSE_MAP_NODE",
                "CHOOSE_REST_OPTION",
                "USE_POTION",
                "WAIT",
            }
        )
        for action in self.legal_actions:
            if action.kind not in allowed:
                raise ValueError("Action belongs to another game")
            if action.kind == "PLANT" and any(
                (
                    getattr(action.facts, k) is None
                    for k in ["seed_id", "plant_type", "row", "col", "cost"]
                )
            ):
                raise ValueError("Incomplete planting candidate")
            if action.kind == "COLLECT_SUN" and (not action.facts.target_id):
                raise ValueError("Sun identity missing")
            if action.kind == "SHOVEL" and (
                not action.facts.target_id
                or not action.facts.row
                or (not action.facts.col)
            ):
                raise ValueError("Shovel target missing")
            if action.kind in {"SELECT_SEED", "DESELECT_SEED"} and (
                not action.facts.plant_type
            ):
                raise ValueError("Seed identity missing")
        return self


class Event(Strict):
    event_id: Identifier
    event_seq: Counter
    kind: Literal[
        "human_input",
        "select_seed",
        "plant",
        "shovel",
        "collect_sun",
        "entity_lost",
        "threat",
        "pause",
        "resume",
        "focus",
        "reclaim",
        "lifecycle",
        "unknown_change",
    ]
    actor: Literal["human", "jev", "engine", "unknown"]
    evidence: Literal["native_hook", "own_command", "state_diff"]
    observed_at_host_mono_ms: Milliseconds
    snapshot_seq: Counter
    control_epoch: Counter
    entity_id: Identifier | None = None
    command_id: Identifier | None = None
    row: Annotated[int, Field(ge=1, le=6)] | None = None
    col: Annotated[int, Field(ge=1, le=9)] | None = None
    text: Description = ""

    @model_validator(mode="after")
    def evidence_matches(self):
        if self.actor == "human" and self.evidence != "native_hook":
            raise ValueError("Diff is not human action evidence")
        if self.actor == "jev" and (
            self.evidence != "own_command" or not self.command_id
        ):
            raise ValueError("Own action needs command identity")
        return self


class Hello(Strict):
    build: Label
    adapter_revision: Identifier
    capabilities: Capabilities


class Anchor(Strict):
    snapshot_seq: Counter
    decision_id: Identifier
    control_epoch: Counter
    candidate_set_id: Identifier


class ActionFocus(Strict):
    kinds: Annotated[list[ActionKind], Field(max_length=5)] = []
    plant_types: Annotated[list[Identifier], Field(max_length=16)] = []
    rows: Annotated[list[Annotated[int, Field(ge=1, le=6)]], Field(max_length=6)] = []
    cols: Annotated[list[Annotated[int, Field(ge=1, le=9)]], Field(max_length=9)] = []


class Observe(Strict):
    nonce: Identifier
    focus: ActionFocus | None = None


class Arm(Strict):
    source_event_id: Identifier
    anchor: Anchor
    allowed_kinds: Annotated[list[ActionKind], Field(min_length=1, max_length=8)]
    ttl_ms: Annotated[int, Field(ge=1, le=15000)]


class StartAutoplay(Strict):
    source_event_id: Identifier
    anchor: Anchor
    scope: AutoplayScope
    duration_ms: Annotated[int, Field(ge=1000, le=43200000)] | None = None
    allowed_kinds: Annotated[list[ActionKind], Field(min_length=1, max_length=5)] = [
        "PLANT",
        "COLLECT_SUN",
    ]

    @model_validator(mode="after")
    def timed_scope(self):
        if (self.scope == "timed") != (self.duration_ms is not None):
            raise ValueError("Only timed autoplay has a duration")
        return self


class AutoplayResult(Strict):
    source_event_id: Identifier
    status: Literal["STARTED", "REJECTED"]
    autoplay_id: Identifier | None = None
    remaining_ms: Annotated[int, Field(ge=0, le=43200000)] | None = None
    reason: Label = ""


class ArmAutoplay(Arm):
    autoplay_id: Identifier


class GrantResult(Strict):
    source_event_id: Identifier
    status: Literal["ARMED", "REJECTED"]
    grant_id: Identifier | None = None
    reason: Label = ""
    remaining_ms: Annotated[int, Field(ge=0, le=15000)] = 0


class Submit(Strict):
    command_id: Identifier
    grant_id: Identifier
    action_id: Identifier
    anchor: Anchor


class ActionResult(Strict):
    command_id: Identifier
    status: Literal[
        "REJECTED",
        "ACCEPTED",
        "CONFIRMED",
        "CONFIRMED_REQUIRES_HUMAN_CONTINUATION",
        "UNKNOWN",
    ]
    action_id: Identifier | None = None
    reason: Label = ""
    confirmed_event_id: Identifier | None = None


class Revoke(Strict):
    reason: Label


class DiscardStep(Strict):
    grant_id: Identifier


class RevokeAck(Strict):
    control_epoch: Counter


class StatusRequest(Strict):
    command_id: Identifier


class Heartbeat(Strict):
    nonce: Identifier


class Error(Strict):
    code: Identifier
    detail: Description = ""


PAYLOADS = {
    "hello": Hello,
    "hello_ack": Capabilities,
    "observe": Observe,
    "snapshot": Snapshot,
    "event": Event,
    "heartbeat": Heartbeat,
    "arm_once": Arm,
    "grant_result": GrantResult,
    "submit_once": Submit,
    "action_result": ActionResult,
    "revoke": Revoke,
    "revoke_ack": RevokeAck,
    "get_action_status": StatusRequest,
    "action_status": ActionResult,
    "error": Error,
    "start_autoplay": StartAutoplay,
    "autoplay_result": AutoplayResult,
    "arm_autoplay_step": ArmAutoplay,
    "discard_step": DiscardStep,
}


class Envelope(Strict):
    protocol_version: Literal[1] = 1
    type: Literal[
        "hello",
        "hello_ack",
        "observe",
        "snapshot",
        "event",
        "heartbeat",
        "arm_once",
        "grant_result",
        "submit_once",
        "action_result",
        "revoke",
        "revoke_ack",
        "get_action_status",
        "action_status",
        "error",
        "start_autoplay",
        "autoplay_result",
        "arm_autoplay_step",
        "discard_step",
    ]
    message_id: Identifier
    session_id: Identifier
    game_id: GameId
    game_instance_id: Identifier
    bridge_epoch: Identifier
    active_game_epoch: Counter
    payload: dict

    @model_validator(mode="after")
    def validate_payload(self):
        model = PAYLOADS[self.type].model_validate(self.payload)
        if self.type == "snapshot" and model.view.game != self.game_id:
            raise ValueError("Game identity mismatch")
        if self.game_id != "pvz_web" and (
            self.type
            in {
                "start_autoplay",
                "autoplay_result",
                "arm_autoplay_step",
                "discard_step",
            }
            or (self.type == "hello" and model.capabilities.auto_continue)
            or (self.type == "hello_ack" and model.auto_continue)
        ):
            raise ValueError("Autoplay is PvZ-only")
        self.payload = model.model_dump(mode="json")
        if self.type == "observe" and self.payload.get("focus") is None:
            self.payload.pop("focus", None)
        return self

    def body(self):
        return PAYLOADS[self.type].model_validate(self.payload)


def decode(raw: str | bytes) -> Envelope:
    if len(raw.encode() if isinstance(raw, str) else raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Message exceeds protocol limit")
    return Envelope.model_validate_json(raw)


def schema():
    result = Envelope.model_json_schema()
    result["$id"] = "https://local.invalid/schemas/game_bridge_v1.json"
    result["title"] = "Game bridge v1, with per-message payload validation"
    result["$defs"] = result.get("$defs", {})
    clauses = []
    for name, model in PAYLOADS.items():
        payload_schema = model.model_json_schema(ref_template="#/$defs/{model}")
        result["$defs"].update(payload_schema.pop("$defs", {}))
        clauses.append(
            {
                "if": {"properties": {"type": {"const": name}}},
                "then": {"properties": {"payload": payload_schema}},
            }
        )
    result["allOf"] = clauses
    return result
