from collections import defaultdict
import time
import copy
from jarvis.core.jev import estimate_tokens


def fit_state(state, questions, budget):
    state = copy.deepcopy(state)
    if estimate_tokens({"state": state, "questions": questions}) <= budget:
        return state
    game = state["game"]
    game["recent"] = game.get("recent", [])[-1:]
    for lane in game["lanes"]:
        groups = lane["zombies"]
        counts = {}
        for group in groups:
            counts[group["name"]] = counts.get(group["name"], 0) + group["count"]
        lane["visible_types"] = counts
        lane["zombies"] = sorted(groups, key=lambda z: z["x"])[:2]
        for z in lane["zombies"]:
            z.pop("passed_cols", None)
    game["zombie_coverage"] = (
        "counts plus nearest two visible groups per lane; exact total in enemy_count"
    )
    if estimate_tokens({"state": state, "questions": questions}) <= budget:
        return state
    for lane in game["lanes"]:
        lane["zombies"] = lane["zombies"][:1]
        lane.pop("visible_types", None)
    game["zombie_coverage"] = (
        "nearest visible enemy group plus total count in each lane"
    )
    game["recent"] = []
    return state


def public_state(snapshot):
    v = snapshot.view
    columns = v.column_x

    def plant_x(p):
        return (
            p.x
            if p.x is not None
            else columns[p.col - 1]
            if len(columns) >= p.col
            else None
        )

    lanes = []
    for row in v.plantable_rows or range(1, v.rows + 1):
        plants = [p for p in v.plants if p.row == row]
        zombies = [z for z in v.zombies if z.row == row]
        shooters = [
            p
            for p in plants
            if p.role == "attack"
            and p.plant_type != "oChomper"
            and (plant_x(p) is not None)
        ]
        closest = min(zombies, key=lambda z: z.x, default=None)
        grouped = {}
        for z in sorted(zombies, key=lambda z: z.x):
            key = (z.name, z.zone, z.state)
            if key not in grouped:
                grouped[key] = {
                    "name": z.name,
                    "x": round(z.x),
                    "zone": z.zone,
                    "state": z.state,
                    "speed_px_s": z.observed_speed,
                    "count": 0,
                    "passed_cols": [i + 1 for i, x in enumerate(columns) if x > z.x],
                    "shooters_covering_nearest_in_group": sum(
                        (plant_x(p) < z.x for p in shooters)
                    ),
                }
            grouped[key]["count"] += 1
        lanes.append(
            {
                "row": row,
                "plants": [
                    {
                        "name": name,
                        "cols": [
                            p.col
                            for p in plants
                            if p.name == name and p.state == status
                        ],
                        "state": status,
                    }
                    for name, status in dict.fromkeys(
                        ((p.name, p.state) for p in plants)
                    )
                ],
                "zombies": list(grouped.values()),
                "shooters_covering_threat": sum(
                    (plant_x(p) < closest.x for p in shooters)
                )
                if closest
                else 0,
                "coverage_definition": "Ranged plants left of the closest visible enemy; cannot shoot backwards.",
                "closest_enemy_to_rear_seconds": min(
                    (
                        (z.x - columns[0]) / z.observed_speed
                        for z in zombies
                        if columns and z.observed_speed
                    ),
                    default=None,
                ),
                "enemy_count": len(zombies),
            }
        )
    return {
        "sun": v.sun,
        "stage": v.ui_stage,
        "battle_outcome": v.battle_outcome,
        "tutorial": v.tutorial_text,
        "lanes": lanes,
        "zombie_coverage": "nearest visible representative per type/zone/state, with counts",
        "economy_plants": sum((p.role == "economy" for p in v.plants)),
        "ranged_attackers": sum(
            (p.role == "attack" and p.plant_type != "oChomper" for p in v.plants)
        ),
        "cards": [
            {
                "slot": s.slot,
                "name": s.name,
                "cost": s.cost,
                "ready": s.ready,
                "cooldown_ready": s.cooldown_ready,
                "effect": s.effect,
            }
            for s in v.seed_bank
        ],
        "column_x": columns,
        "suns": [s.amount for s in v.suns],
        "recent": [e.model_dump(exclude_none=True) for e in snapshot.recent_events],
    }


def target_options(snapshot):
    options = {"NONE": "No planting target; reassess next observation"}
    targets = {}
    for seed in snapshot.view.seed_bank:
        for row in snapshot.view.plantable_rows or range(1, snapshot.view.rows + 1):
            key = f"S{seed.slot}R{row}"
            targets[key] = {
                "seed_id": seed.seed_id,
                "plant_type": seed.plant_type,
                "name": seed.name,
                "row": row,
                "cost": seed.cost,
            }
            options[key] = f"{seed.name}→{row}路，需{seed.cost}阳光"
    return (options, targets)


def candidates(snapshot, limit=40):
    collections = [a for a in snapshot.legal_actions if a.kind == "COLLECT_SUN"]
    wait = [a for a in snapshot.legal_actions if a.kind == "WAIT"]
    groups = defaultdict(list)
    for action in snapshot.legal_actions:
        if action.kind == "PLANT":
            groups[action.facts.seed_id, action.facts.row].append(action)
    plants = [
        group[i]
        for i in range(max(map(len, groups.values()), default=0))
        for group in groups.values()
        if i < len(group)
    ]
    return (
        collections[:3]
        + wait[:1]
        + plants[: max(0, limit - len(collections[:3]) - len(wait[:1]))]
    )


async def decide(
    controller,
    snapshot,
    *,
    request="",
    proactive=False,
    autoplay=False,
    prior=None,
    limit=40,
):
    blocked = getattr(controller, "pvz_story_blocked", set())
    if blocked:
        snapshot = snapshot.model_copy(
            update={
                "view": snapshot.view.model_copy(
                    update={
                        "seed_bank": [
                            s
                            for s in snapshot.view.seed_bank
                            if s.plant_type not in blocked
                        ]
                    }
                ),
                "legal_actions": [
                    a
                    for a in snapshot.legal_actions
                    if a.kind != "PLANT" or a.facts.plant_type not in blocked
                ],
            }
        )
        if prior and prior.get("plant_type") in blocked:
            prior = None
    reserve = getattr(controller, "pvz_story_reserve", 0)
    if reserve:
        spendable = max(0, (snapshot.view.sun or 0) - reserve)
        snapshot = snapshot.model_copy(
            update={
                "legal_actions": [
                    a
                    for a in snapshot.legal_actions
                    if a.kind != "PLANT" or (a.facts.cost or 0) <= spendable
                ]
            }
        )
    actions = candidates(snapshot, limit)
    if not actions:
        return (None, prior)
    if not snapshot.view.seed_bank:
        collected = await collect_choice(
            controller, snapshot, request=request, autoplay=autoplay
        )
        return (collected or next((a for a in actions if a.kind == "WAIT"), None), None)
    collected = await collect_choice(
        controller, snapshot, request=request, autoplay=autoplay
    )
    if collected:
        return (collected, prior)
    state = {
        "request": request[:600]
        or ("Optional useful hint" if proactive else "Choose the next useful play"),
        "game": public_state(snapshot),
        "ongoing_autoplay": autoplay,
        "previous_target": {
            k: v for k, v in (prior or {}).items() if not k.startswith("_")
        }
        or None,
        "protected_sun": reserve,
        "spendable_sun": max(0, snapshot.view.sun - reserve)
        if snapshot.view.sun is not None
        else None,
        "information": "Only current visible state. No future spawns or hidden health. Columns grow to the right; zombies walk left.",
    }
    _, lookup = target_options(snapshot)
    plan_key = [
        [(z.entity_id, z.row, z.zone) for z in snapshot.view.zombies],
        [(p.entity_id, p.row, p.col, p.state) for p in snapshot.view.plants],
    ]
    reuse = (
        prior
        and prior.get("_request") == request
        and (prior.get("_until", 0) > time.monotonic())
        and (prior.get("_key") == plan_key)
        and any((s.seed_id == prior["seed_id"] for s in snapshot.view.seed_bank))
    )
    if reuse:
        target = prior
    else:
        state["planning_goal"] = (
            "Choose a useful next investment for survival, damage and sustainable income. "
        )
        state["planning_goal"] += (
            "No enemies yet still permits economic preparation. Approaching uncovered enemies make damage urgent. "
        )
        state["planning_goal"] += (
            "Cost matters, but a cheap blocker alone cannot kill enemies. A target may need sun saved first. "
        )
        state["planning_goal"] += (
            "Only spendable_sun can fund a target; protected_sun is unavailable until the user releases it. "
        )
        state["planning_goal"] += (
            "Judge the whole lawn, current defense, contact time and plant effects, not hidden waves or a fixed build order."
        )
        plan_questions = {}
        for seed in snapshot.view.seed_bank:
            plan_questions["value_" + str(seed.slot)] = {
                "type": "score",
                "instructions": f"Rate {seed.name} as the next investment for planning_goal, including its best lane and opportunity cost. It may need sun saved first.",
                "criteria": [
                    "Inappropriate or counterproductive now",
                    "Optional; other needs should come first",
                    "Useful next investment",
                    "Strong priority for the current lawn",
                ],
            }
            plan_questions["lane_" + str(seed.slot)] = {
                "type": "choice",
                "instructions": f"Assuming the next target plant is {seed.name}, which lane benefits most? Compare exposed threats and existing defense. Respect the user request, firing direction and preparation time.",
                "criteria": {
                    str(row): f"第{row}路"
                    for row in snapshot.view.plantable_rows
                    or range(1, snapshot.view.rows + 1)
                },
            }
        plan_state = fit_state(
            state, plan_questions, controller.config["jev"]["max_context_tokens"] - 80
        )
        plan = await controller.judge_typed(
            "pvz_plan", plan_state, plan_questions, deadline_ms=2500
        )
        seed = max(
            snapshot.view.seed_bank,
            key=lambda s: plan["value_" + str(s.slot)]["score"],
            default=None,
        )
        slot = str(seed.slot) if seed else None
        target = (
            lookup.get("S" + slot + "R" + plan["lane_" + slot]["choice"])
            if seed and plan["value_" + slot]["score"] >= 1
            else None
        )
        if target:
            target = {
                **target,
                "_key": plan_key,
                "_until": time.monotonic() + 5,
                "_request": request,
            }
    state["current_planting_objective"] = (
        {k: v for k, v in target.items() if not k.startswith("_")} if target else None
    )
    state.pop("planning_goal", None)
    state["sun_needed_for_objective"] = (
        max(0, target["cost"] - state["spendable_sun"])
        if target
        and target["cost"] is not None
        and (state["spendable_sun"] is not None)
        else None
    )
    state["previous_target"] = {
        k: v for k, v in (prior or {}).items() if not k.startswith("_")
    } or None
    focused = snapshot.legal_actions if target else actions
    actions = [
        a
        for a in focused
        if a.kind in {"WAIT", "COLLECT_SUN"}
        or (
            a.kind == "PLANT"
            and target
            and (a.facts.seed_id == target["seed_id"])
            and (a.facts.row == target["row"])
        )
    ]
    groups = {}
    for action in actions:
        key = (
            "COLLECT"
            if action.kind == "COLLECT_SUN"
            else "WAIT"
            if action.kind == "WAIT"
            else "PLANT_"
            + str(
                next(
                    (
                        s.slot
                        for s in snapshot.view.seed_bank
                        if s.seed_id == action.facts.seed_id
                    )
                )
            )
        )
        groups.setdefault(key, []).append(action)
    if set(groups) == {"WAIT"}:
        return (None if proactive else groups["WAIT"][0], target)
    options = {"SILENT": "No useful hint"} if proactive else {}
    for key, group in groups.items():
        options[key] = (
            {
                "operation": "点击收取当前可见阳光",
                "effect": "增加阳光，不花费阳光、不消耗卡片、不触发植物冷却；正常动画到账后可用于种植。",
            }
            if key == "COLLECT"
            else {
                "operation": "什么都不做，暂时等候",
                "effect": "僵尸继续移动，已掉落的阳光不会自己到账。",
            }
            if key == "WAIT"
            else {
                "operation": "种植"
                + next(
                    (
                        s.name
                        for s in snapshot.view.seed_bank
                        if s.seed_id == group[0].facts.seed_id
                    )
                ),
                "effect": "花费卡片阳光，开始该卡冷却。",
            }
        )
    questions = {
        "action": {
            "type": "choice",
            "instructions": "Select the most useful LEGAL action now for the request and whole lawn. Choose among kinds of useful play, not among redundant cells. Consider developing economy, killing visible enemies, defending the house, and collecting resources. WAIT is useful when saving for a needed card or cooling down, but unnecessary waiting also delays the defense. Collecting a visible sun adds resources without spending. Waiting is not collection: dropped sun must be clicked to increase the balance. Collection advances a saving target and does not reset plant cooldowns. Use exact relative positions: a blocker at a column already passed by a zombie will not stop it; shooters face right. A mine that is arming cannot stop an enemy yet. Follow actual tutorial text. Work toward current_planting_objective. WAIT if no current listed action advances that objective; a new plan can then be chosen. Choose only supplied actions; obey explicit named plant/lane restrictions. No future wave assumptions.",
            "criteria": options,
        },
        "insufficient": {
            "type": "noul",
            "instructions": "Is essential information missing to choose ANY reasonable listed action for this request? Several good cells, waiting for sun/cooldown, no enemies yet, or uncertainty about an optimal strategy are NOT missing information.",
        },
    }
    for key, group in groups.items():
        if key == "WAIT":
            continue
        label = options[key]["operation"]

        def placement(a):
            if a.kind != "PLANT":
                return a.label
            row, col = (a.facts.row, a.facts.col)
            columns = snapshot.view.column_x
            if not columns:
                return a.label
            x = columns[col - 1]
            zs = [z for z in snapshot.view.zombies if z.row == row]
            ahead = [z for z in zs if z.x >= x]
            blast = [
                z
                for z in snapshot.view.zombies
                if abs(z.row - row) <= 1 and abs(z.x - x) <= 115
            ]
            return {
                "cell": a.label,
                "visible_enemies_in_explosion": len(blast)
                if a.facts.plant_type == "oCherryBomb"
                else None,
                "blast_rows": sorted({z.row for z in blast})
                if a.facts.plant_type == "oCherryBomb"
                else None,
                "closest_ahead_gap_px": min(
                    (round(z.x - x) for z in ahead), default=None
                ),
                "estimated_contact_seconds": min(
                    (
                        round((z.x - x) / z.observed_speed, 1)
                        for z in ahead
                        if z.observed_speed
                    ),
                    default=None,
                ),
                "enemies_already_past_cell": sum((z.x < x for z in zs)),
            }

        questions["where_" + key] = {
            "type": "choice",
            "instructions": f"Assuming the next operation is {label}, select its most useful supplied target/cell. Use lane pressure, existing defense and exact x/column relationships. A blocker/mine must be ahead of the approaching zombie (lower x); a shooter faces right. Economy plants do not deal damage. A mine needs time to arm. Larger positive gap gives a ranged attacker more firing time; mines need enough contact time to finish arming. Never put a mine beyond every current enemy: it cannot attack backwards. Cherry bombs affect adjacent lanes too: use visible_enemies_in_explosion and blast_rows to compare actual coverage, not distance to one zombie. Obey any explicit plant/lane constraint. This question does not decide whether to perform this operation.",
            "criteria": {a.action_id: placement(a) for a in group},
        }
    budget = controller.config["jev"]["max_context_tokens"] - 60
    state = fit_state(state, questions, budget)
    while (
        estimate_tokens({"state": state, "questions": questions}) > budget
        and len(actions) > 1
    ):
        index = next(
            (i for i in range(len(actions) - 1, -1, -1) if actions[i].kind == "PLANT"),
            None,
        )
        if index is None:
            break
        action = actions.pop(index)
        key = next((k for k, g in groups.items() if action in g))
        groups[key].remove(action)
        questions["where_" + key]["criteria"].pop(action.action_id)
        if not groups[key]:
            groups.pop(key)
            options.pop(key)
            questions.pop("where_" + key)
    controller.emit(
        "remote.candidate_budget",
        supplied=len(actions),
        total=snapshot.total_legal_actions,
        coverage="filtered_legal_subset",
        estimated_tokens=estimate_tokens({"state": state, "questions": questions}),
    )
    answers = await controller.judge_typed(
        "pvz_tactics", state, questions, deadline_ms=2000
    )
    chosen = answers["action"]["choice"]
    confidence = answers["action"]["confidence"]
    missing = answers["insufficient"]["noul"]
    controller.emit(
        "remote.tactics.selected",
        choice=chosen,
        choice_confidence=confidence,
        missing_information_probability=missing,
        target=target,
    )
    if chosen == "SILENT" or missing >= 0.8:
        return (None, None)
    action = (
        groups["WAIT"][0]
        if chosen == "WAIT"
        else next(
            (
                a
                for a in groups[chosen]
                if a.action_id == answers["where_" + chosen]["choice"]
            )
        )
    )
    return (action, target)


async def collect_choice(
    controller, snapshot, *, request, autoplay=True, current_speech=""
):
    suns = [a for a in snapshot.legal_actions if a.kind == "COLLECT_SUN"]
    if not suns:
        return None
    take = await controller.judge_typed(
        "pvz_collect",
        {
            "authorized_request": request,
            "current_speech": current_speech,
            "sun": snapshot.view.sun,
            "visible_suns": [a.label for a in suns],
            "effect": "Normal click adds visible sun after animation; zero cost, no seed cooldown, no planting.",
            "autoplay": autoplay,
        },
        {
            "collect": {
                "type": "noul",
                "instructions": "Should the companion collect one of these visible suns now under authorized_request? Respect explicit restrictions. General ongoing play allows collection, including while the human chats. If current_speech asks to stop/reclaim or not collect, answer NO even if it is not yet final. Collection supports saving resources; it neither plants nor spends. An exact different one-step request does not permit collection.",
            }
        },
        deadline_ms=1500,
    )
    return suns[0] if take["collect"]["noul"] >= 0.6 else None
