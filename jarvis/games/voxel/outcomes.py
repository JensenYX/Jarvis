from __future__ import annotations
import copy
import json
import math
import time
from .survival_intent import context

EVIDENCE_KINDS = {
    "inventory",
    "use",
    "place",
    "dig",
    "defeat",
    "position",
    "observation",
}
EFFECTS = {"USE", "PLACE", "JUMP_PLACE", "DIG", "DIG_DOWN", "CRAFT", "ATTACK"}


def facts(snapshot):
    return {
        r["item_id"]: r
        for r in snapshot["view"].get("survival", {}).get("item_facts", [])
    }


def approach_history(task, game):
    from collections import Counter

    counts = Counter(((r["kind"], r.get("item_id")) for r in task.get("effects", [])))
    baseline = task.get("baseline_inventory", {})
    return {
        "performed": [
            {"kind": k, "item_id": i, "count": n} for (k, i), n in counts.items()
        ],
        "net_inventory_gains": {
            i: n - baseline.get(i, 0)
            for i, n in game["inventory"].items()
            if n > baseline.get(i, 0)
        },
        "visited_ground_cells": len(task.get("progress", {}).get("cells", [])),
        "start_position": task.get("start_position"),
        "current_position": game["player"].get("position"),
    }


def observations(snapshot):
    game = context(snapshot)
    out = {}
    for prefix, rows in [
        ("node", game["nodes"]),
        ("drop", game["drops"]),
        ("entity", game["entities"]),
    ]:
        for i, row in enumerate(rows):
            out[f"{prefix}:{i}"] = {**row, "evidence": prefix}
    return out


def planning_view(snapshot):
    game = context(snapshot)
    result = {
        k: game[k]
        for k in (
            "phase",
            "player",
            "inventory",
            "focus",
            "workbench",
            "in_water",
            "grounded",
            "dry_land",
        )
    }
    carried = set(game["inventory"])
    result["item_facts"] = {
        i: {
            k: v
            for k, v in r.items()
            if k
            in {"name", "edible", "food_points", "food_risk", "plant", "possible_drops"}
            or (k == "usage" and i in carried)
        }
        for i, r in facts(snapshot).items()
    }
    result["routes"] = game["navigation"]
    result["standing_surfaces"] = (
        snapshot["view"].get("construction", {}).get("terrain", [])[:24]
    )
    return result


def compact_game(snapshot):
    game = context(snapshot)
    registry = facts(snapshot)
    game["tools"] = [
        {k: r[k] for k in ("item_id", "slot", "damage", "tool") if k in r}
        for r in game["tools"]
    ]
    game.pop("target_states", None)
    game.pop("path", None)
    game["item_facts"] = {
        i: {
            k: r[k]
            for k in (
                "name",
                "groups",
                "possible_drops",
                "plant",
                "edible",
                "food_points",
                "food_risk",
                "usage",
            )
            if k in r
        }
        for i, r in registry.items()
    }
    return game


def evidence(task, snapshot):
    from .spatial import geometry

    game = context(snapshot)
    registry = facts(snapshot)
    rows = []
    consumed = {}
    for row in task.get("effects", []):
        before, after = (row.get("count_before"), row.get("count_after"))
        if (
            row["kind"] in {"USE", "PLACE"}
            and row.get("item_id")
            and (type(before) is int)
            and (type(after) is int)
            and (0 <= after < before)
        ):
            item = row["item_id"]
            consumed[item] = consumed.get(item, 0) + before - after
    for item in dict.fromkeys([*game["inventory"], *consumed]):
        count = game["inventory"].get(item, 0)
        f = registry.get(item, {})
        rows.append(
            {
                "kind": "inventory",
                "item_id": item,
                "count": count,
                "gained": count - task.get("baseline_inventory", {}).get(item, 0),
                "consumed": consumed.get(item, 0),
                "edible": f.get("edible", False),
                "name": f.get("name", item),
            }
        )
    aggregates = {}
    for row in task.get("effects", [])[-96:]:
        signature = (
            row["kind"],
            row.get("item_id"),
            row.get("node_after"),
            row.get("above_after"),
            row.get("interaction"),
        )
        aggregates[signature] = aggregates.get(signature, 0) + 1
    for (kind, item, node, above, interaction), count in aggregates.items():
        if kind in {"USE", "PLACE", "DIG"}:
            rows.append(
                {
                    "kind": kind.lower(),
                    "item_id": item,
                    "node_after": node,
                    "above_after": above,
                    "interaction": interaction,
                    "confirmed_distinct_effects": count,
                    "summary_of_receipts": True,
                }
            )
    retained = []
    for c in (task.get("outcome") or {}).get("success_checks", []):
        items = set(c.get("item_ids") or [])
        relevant = [
            r for r in task.get("effects", []) if not items or r.get("item_id") in items
        ]
        for r in relevant[-24:]:
            if r not in retained:
                retained.append(r)
    if not retained:
        retained = task.get("effects", [])[-12:]
    for row in retained[-48:]:
        kind = {
            "USE": "use",
            "PLACE": "place",
            "DIG": "dig",
            "ATTACK": "defeat" if row.get("defeated") else "attack",
            "CRAFT": "craft",
        }.get(row["kind"], row["kind"].lower())
        rows.append({**row, "kind": kind})
    placed = [
        r
        for r in task.get("effects", [])
        if r["kind"] == "USE"
        and r.get("above_before") == "air"
        and (r.get("above_after") not in {None, "air"})
    ]
    for row in placed:
        rows.append({**row, "kind": "place", "placed_item_id": row["above_after"]})
    by_item = {}
    for row in placed:
        by_item[row["item_id"]] = by_item.get(row["item_id"], 0) + 1
    for item, count in by_item.items():
        rows.append(
            {
                "kind": "place",
                "item_id": item,
                "confirmed_distinct_effects": count,
                "summary_of_receipts": True,
            }
        )
    for target in dict.fromkeys(task.get("defeated", [])):
        hit = next(
            (
                r
                for r in reversed(task.get("effects", []))
                if r["kind"] == "ATTACK" and r.get("target_id") == target
            ),
            {},
        )
        rows.append(
            {
                "kind": "defeat",
                "target_id": target,
                "item_id": hit.get("entity_kind"),
                "entity_kind": hit.get("entity_kind"),
                "weapon_item_id": hit.get("weapon_item_id"),
                "weapon_evidence": hit.get("weapon_evidence"),
                "defeated": True,
                "native_death_confirmed": True,
                "fact": "This creature was killed by our native attack; death was confirmed by the game.",
            }
        )
    rows.append(
        {
            "kind": "position",
            "current": game["player"]["position"],
            "start": task["start_position"],
            "grounded": game["grounded"],
            "dry_land": game["dry_land"],
            "in_water": game["in_water"],
            "observed_geometry": geometry(snapshot, task),
            "breath": game["player"].get("breath"),
            "head_medium": game["player"].get("head_medium"),
        }
    )
    for row in game["nodes"]:
        f = registry.get(row["item_id"], {})
        rows.append(
            {
                "kind": "observation",
                "item_id": row["item_id"],
                "name": f.get("name", row.get("name")),
                "position": row["position"],
                "plant": f.get("plant"),
            }
        )
    return {str(i): r for i, r in enumerate(rows)}


def matching_evidence(check, rows):
    kinds = check["evidence"]
    items = set(check.get("item_ids") or [])
    mixed = len(kinds) > 1

    def matches(row):
        if row["kind"] not in kinds:
            return False
        if (
            not items
            or (mixed and row["kind"] == "defeat")
            or row.get("item_id") in items
        ):
            return True
        return bool(
            row["kind"] == "defeat"
            and (row.get("defeated") or row.get("native_death_confirmed"))
            and (row.get("weapon_item_id") in items)
            and (
                (row.get("weapon_evidence") or {}).get("kind")
                == "native_validated_attack_snapshot"
            )
        )

    return {key: row for key, row in rows.items() if matches(row)}


def has_evidence(check, rows):
    kinds = check["evidence"]
    matches = list(matching_evidence(check, rows).values())
    minimum = check.get("minimum", 1)
    if type(minimum) is not int or minimum < 1:
        return False
    if "inventory" in kinds:
        field = {"stock": "count", "gained": "gained", "consumed": "consumed"}.get(
            check.get("inventory_mode", "stock")
        )
        if field is None:
            return False
        if (
            sum((max(0, r.get(field, 0)) for r in matches if r["kind"] == "inventory"))
            >= minimum
        ):
            return True
    effects = [r for r in matches if r.get("summary_of_receipts")]
    totals = {}
    for r in effects:
        totals[r["kind"]] = totals.get(r["kind"], 0) + r["confirmed_distinct_effects"]
    if max(totals.values(), default=0) >= minimum:
        return True
    observations = {
        tuple((r["position"][k] for k in ("x", "y", "z")))
        for r in matches
        if r["kind"] == "observation" and r.get("position")
    }
    if len(observations) >= minimum:
        return True
    defeated = {
        r["target_id"]
        for r in matches
        if r["kind"] == "defeat"
        and r.get("target_id")
        and (r.get("defeated") or r.get("native_death_confirmed"))
    }
    if len(defeated) >= minimum:
        return True
    return minimum == 1 and any(
        (
            not r.get("summary_of_receipts")
            and r["kind"] not in {"inventory", "defeat"}
            for r in matches
        )
    )


async def counted_scope(mode, check, rows, snapshot):
    if (
        check.get("item_ids")
        or check.get("minimum", 1) <= 1
        or "position" in check["evidence"]
    ):
        return rows
    registry = facts(snapshot)
    groups = {}
    for key, row in rows.items():
        item = row.get("item_id")
        if item:
            groups.setdefault(item, {})[key] = row
    if not groups:
        return {}
    candidates = {
        str(i): {
            "item_id": item,
            "registered_facts": registry.get(item, {}),
            "evidence": evidence,
        }
        for i, (item, evidence) in enumerate(groups.items())
    }
    answers = await mode.r.controller.judge_typed(
        "voxel_outcome_count_scope",
        {"condition": check, "candidates": candidates},
        {
            key: {
                "type": "noul",
                "instructions": "Does candidate "
                + key
                + " belong to the requested FINAL result/category in condition? Judge item/effect relevance only, not whether enough units exist. Preparation, tools, unrelated inventory and terrain preparation do not count toward a final product. For an open category, every matching observed item may qualify; do not restrict it to one arbitrary product. The program counts qualifying native quantities separately.",
            }
            for key in candidates
        },
        deadline_ms=2200,
    )
    selected = {
        key: row
        for i, candidate in candidates.items()
        if answers[i]["noul"] >= 0.8
        for key, row in candidate["evidence"].items()
    }
    mode.r.emit(
        "voxel.outcome.count_scope",
        condition=check,
        answers=answers,
        selected_evidence=list(selected),
        candidate_items={i: c["item_id"] for i, c in candidates.items()},
    )
    return selected


def validate_plan(value, known_items, prior=None):
    if prior:
        value["objective"] = prior["objective"]
        value["success_checks"] = copy.deepcopy(prior["success_checks"])
        known_items = (
            set(known_items)
            | {i for c in prior["success_checks"] for i in c.get("item_ids", [])}
            | set(prior.get("focus_items", []))
        )
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("objective"), str)
        or (not value["objective"].strip())
    ):
        raise ValueError("Missing user outcome")
    if not isinstance(value.get("strategy"), str) or len(value["strategy"]) > 1400:
        raise ValueError("Invalid outcome strategy")
    checks = value.get("success_checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 6:
        raise ValueError("Need observable success checks")
    for c in checks:
        if (
            not isinstance(c, dict)
            or not isinstance(c.get("description"), str)
            or (not isinstance(c.get("evidence"), list))
            or (not c["evidence"])
            or (not set(c["evidence"]) <= EVIDENCE_KINDS)
        ):
            raise ValueError(
                "Each success check needs description:string and evidence:array of lowercase strings from "
                + ",".join(sorted(EVIDENCE_KINDS))
            )
        if c.get("item_ids") and (not set(c["item_ids"]) <= known_items):
            raise ValueError(
                "Unknown success item "
                + str(set(c["item_ids"]) - known_items)
                + "; use item_ids=[] for an unnamed category and describe that category in description"
            )
        if type(c.get("minimum", 1)) is not int or not 1 <= c.get("minimum", 1) <= 2048:
            raise ValueError("Invalid evidence minimum")
        if c.get("inventory_mode", "stock") not in {"stock", "gained", "consumed"}:
            raise ValueError("inventory_mode must be stock, gained or consumed")
    for field in ("craft_products", "focus_items"):
        if (
            not isinstance(value.get(field, []), list)
            or len(value.get(field, [])) > 12
            or (not set(value.get(field, [])) <= known_items)
        ):
            raise ValueError("Unknown planned native product")
    if (
        not isinstance(value.get("milestones", []), list)
        or len(value.get("milestones", [])) > 8
    ):
        raise ValueError("Invalid milestones")
    if prior:
        value["objective"] = prior["objective"]
        value["success_checks"] = copy.deepcopy(prior["success_checks"])
    return value


async def relevant_products(mode, task, snapshot):
    groups = {}
    for r in mode.bridge.recipe_catalog.values():
        groups.setdefault(r["item_id"].split(":")[0], {})[r["item_id"]] = (
            r.get("name_zh") or r["name"].split("\n")[0]
        )
    categories = {k: ", ".join(list(v.values())[:4]) for k, v in groups.items()}
    options = {"NONE": "No crafted product needed for this outcome", **categories}
    data = {
        "request": task["request"],
        "current_strategy": (task.get("outcome") or {}).get("strategy"),
        "inventory": context(snapshot)["inventory"],
        "native_item_uses": {
            k: v.get("usage")
            for k, v in facts(snapshot).items()
            if k in context(snapshot)["inventory"]
        },
    }
    question = {
        "type": "choice",
        "instructions": "Choose the native recipe category potentially needed for a useful tool/ingredient for the requested outcome. Select by the actual request and owned items, not a default survival routine. NONE if carried items suffice. This is retrieval, not execution.",
        "criteria": options,
    }
    data["observed_resources"] = [
        {k: r.get(k) for k in ("item_id", "name", "position")}
        for r in context(snapshot)["nodes"][:12]
    ]
    questions = {
        "primary": question,
        "tool": {
            "type": "choice",
            "instructions": "Which recipe family contains a tool that may be needed to complete this outcome but is NOT owned? Read native_item_uses for prerequisites. Existing occupied farmland is not free space for new crops; seeds need enough empty farmland or a hoe. Mining stone needs a pick. Choose NONE when no tool preparation is needed. This only retrieves recipes, it does not force crafting.",
            "criteria": options,
        },
    }
    import asyncio

    answers = await asyncio.gather(
        *(
            mode.r.controller.judge_typed(
                "voxel_outcome_recipe_retrieval_" + key,
                data,
                {key: q},
                deadline_ms=2300,
            )
            for key, q in questions.items()
        )
    )
    a = {k: v for answer in answers for k, v in answer.items()}
    products = {}
    for v in a.values():
        selected = [v["choice"]]
        if v.get("confidence", 0) < 0.65:
            selected.extend(
                (
                    k
                    for k, p in sorted(
                        v.get("probabilities", {}).items(), key=lambda x: -x[1]
                    )[:3]
                    if p >= 0.1
                )
            )
        for category in selected:
            products.update(groups.get(category, {}))
    carried = set(context(snapshot)["inventory"])
    carried.update((task.get("outcome") or {}).get("craft_products", []))
    for r in mode.bridge.recipe_catalog.values():
        if r["item_id"] in carried:
            products[r["item_id"]] = r.get("name_zh") or r["name"]
    for r in snapshot["view"].get("survival", {}).get("recipes", []):
        products[r["item_id"]] = r["name"]
    if len(products) > 24:
        from .judgment import bounded_choice

        selected = await asyncio.gather(
            *(
                bounded_choice(
                    mode.r.controller,
                    "voxel_plan_product_" + role,
                    {
                        "request": task["request"],
                        "current_strategy": (task.get("outcome") or {}).get("strategy"),
                        "inventory": context(snapshot)["inventory"],
                        "native_item_uses": data["native_item_uses"],
                    },
                    instruction,
                    {"NONE": "No new crafted product needed", **products},
                    deadline_ms=2000,
                )
                for role, instruction in [
                    (
                        "result",
                        "Which exact craftable end-product, if any, is useful for the current requested outcome? Choose a recipe PRODUCT, not a source to gather. Ordinary food search with no specified processing has no required recipe. A described ingredient recipe may require a food product. NONE if no crafting is useful.",
                    ),
                    (
                        "tool",
                        "Which exact new tool or facility is needed for the current outcome but is not already available? Prefer a basic locally craftable tool. Do not add routine workbenches or unrelated tools. NONE if owned tools are enough.",
                    ),
                ]
            )
        )
        ids = {
            r["action"]["choice"] for r in selected if r["action"]["choice"] in products
        }
        for r in selected:
            ids.update(
                (
                    k
                    for k, p in r["action"].get("probabilities", {}).items()
                    if k in products and p >= 0.15
                )
            )
        ids.update((task.get("outcome") or {}).get("craft_products", []))
        if task.get("resolved_product"):
            ids.add(task["resolved_product"]["item_id"])
        products = {i: products[i] for i in ids if i in products}
    return products


async def plan(mode, task, snapshot, previous=None, reason="new_request"):
    if not mode.bridge.recipe_catalog:
        await mode.bridge.load_recipes()
    catalog = mode.bridge.recipe_catalog
    products = await relevant_products(mode, task, snapshot)
    registry = {**getattr(mode.operations, "native_item_memory", {}), **facts(snapshot)}
    prior = task.get("outcome")
    data = {
        "current_request": task["request"],
        "retained_outcome": prior,
        "baseline_inventory": task.get("baseline_inventory"),
        "game": planning_view(snapshot),
        "observations": observations(snapshot),
        "memory": mode.memory.context(),
        "previous_task_referent": {
            k: previous.get(k) for k in ("request", "pending_question", "outcome")
        }
        if previous
        else None,
        "confirmed_effects": task.get("effects", [])[-16:],
        "recent_attempts": task.get("attempts", [])[-8:],
        "permitted_means": {
            k: task.get("scope", {}).get(k) for k in ("alter", "creatures", "consume")
        },
        "reason_for_planning": reason,
        "native_products": products,
        "craft_requirements": (
            snapshot["view"].get("survival", {}).get("task") or {}
        ).get("craft_requirements", []),
        "known_facilities": list(getattr(mode.operations, "facilities", {}).values())[
            -8:
        ],
    }
    data["approach_results"] = approach_history(task, context(snapshot))
    if prior:
        data["retained_outcome"] = {
            k: prior[k]
            for k in (
                "objective",
                "success_checks",
                "strategy",
                "milestones",
                "focus_items",
                "craft_products",
            )
            if k in prior
        }
    for key in ("nodes", "tools", "navigation", "travel", "shore"):
        data["game"].pop(key, None)
    recipe_facts = {}
    for r in catalog.values():
        if r["item_id"] not in products:
            continue
        ingredients = [i for i in r["ingredients"] if i]
        entry = {"count": r["count"], "grid": r["grid"], "ingredients": ingredients}
        variants = recipe_facts.setdefault(r["item_id"], [])
        if entry not in variants and len(variants) < 3:
            variants.append(entry)
    data["native_recipes"] = recipe_facts
    memory = getattr(mode.operations, "observation_memory", {})
    remembered = {}
    for r in memory.values():
        if (
            r["item_id"] not in remembered
            or r["last_observed"] > remembered[r["item_id"]]["last_observed"]
        ):
            remembered[r["item_id"]] = r
    data["previously_seen_resources"] = [
        {
            **{k: v for k, v in r.items() if k not in {"last_observed", "last_seen"}},
            "seconds_since_observed": round(
                max(0, time.time() - r["last_observed"]), 1
            ),
            "native_facts": {
                k: v
                for k, v in getattr(mode.operations, "native_item_memory", {})
                .get(r["item_id"], {})
                .items()
                if k in {"name", "possible_drops", "edible", "plant"}
            },
        }
        for r in remembered.values()
    ][-24:]
    instructions = 'You plan an open-ended VoxeLibre game companion task. Return JSON only: {objective:string,success_checks:[{description:string,evidence:[inventory|use|place|dig|defeat|position|observation],item_ids:[],minimum:integer,inventory_mode:"stock"|"gained"|"consumed"}],strategy:string,milestones:[string],focus_items:[native item IDs],craft_products:[native product IDs],target_ref:string|null,question:string|null}. Use concise Chinese objective/strategy/milestones/checks. strategy is at most 90 Chinese characters, milestones 2-4 short phrases. No coordinates, axis values or enum names in objective/strategy/milestones; use relative directions and landmarks. Capture the actual full user outcome including counts, restrictions and sequence. Every success_checks entry is an OBJECT with description, evidence, item_ids. evidence is an array of lowercase enum strings, e.g. ["use"] or ["inventory","use"], NEVER an object, prose, a node name, or a joined "use|place" string. minimum is the final requested count (default1), using real received inventory units or distinct successful effects. inventory_mode=gained when the user requests collecting additional units, stock when existing stock already satisfies them. inventory_mode=consumed counts units consumed by confirmed native USE/PLACE receipts, even when none remain. It is not remaining stock, a net decrease, or proof of a particular world result. For planting/eating prefer use evidence with the actual result; if separately checking consumed units, never require stock of the consumed item. If the human names a CATEGORY rather than an exact item (e.g. food, wood), use item_ids=[] and describe the category in description. The evidence includes registered edible/groups/name for future discovered items. Do NOT enumerate guessed item IDs or constrain an unnamed food goal to bread. Only put final USER outcomes in success_checks; intermediate optional preparations belong in milestones, not completion requirements. A named new request replaces previous work; previous_task_referent only resolves pronouns or elliptical replies. Current_request, retained_outcome and memory preferences govern your decisions, never a compulsory crafting table or resource routine. A strategy is flexible intent for the next few useful actions, not a hardcoded sequence or proof of completion. Success checks describe the USER result, not preparation: escaping a hole is not obtaining food; materials are not a finished shelter; a tilled field is not planted seeds. Require actual inventory for obtaining items; native use evidence for eating/planting; native place/dig evidence for world edits; defeat evidence for killing; grounded dry position for reaching shore. Use observation only for goals that merely ask to observe. Never require eating for a request only to gather, nor count an attack as a kill. The native actions are DIG, DIG_DOWN (remove our own scaffold underfoot onto its verified support exactly one level below), PLACE ordinary blocks, JUMP_PLACE (real jump and place one carried ordinary block underfoot to gain one level when clear overhead), CRAFT registered recipes/dependencies, SELECT/EQUIP any inventory slot, USE a carried item on an exposed block face or eat one food via normal client input, ATTACK visible creatures, LOOK, MOVE/JUMP_MOVE/SWIM. Farming uses registered seed on_place callbacks: soil group>=2 is farmland, hoe group tools turn cultivatable ground with air above into farmland, crops need light, water helps. Dry unplanted farmland may revert to dirt; observe it again and plant promptly instead of preparing excessive unused ground. Use the real item facts/drops/maturity, inventory and recipe requirements; plants are food only when their harvest yields an edible item or a feasible recipe. A shovel is NOT a hoe and cannot make farmland. If no hoe is owned and insufficient empty farmland exists, choose to craft a registered hoe, or choose to harvest mature crops freeing enough existing farmland. Do not claim a tool is available merely because it has a recipe. A concrete task permits selecting a nearby place and routine prerequisites unless explicitly forbidden. Do not tell the human to find the place or collect materials you can obtain. Choose direct available routes and use owned resources first. native_recipes gives exact ingredient counts per output batch; use those facts instead of remembered recipes. A raw ingredient cannot be made from unrelated wood/stone. Include ONLY currently useful end-products in craft_products (at most 3). A workbench is needed only by a 3x3 recipe. Its placement is an intermediate step if carried and needed; do not craft another if a usable one is known. Names must be exact native_products or game.item_facts keys; use focus_items ONLY for current relevant observed targets/tools/resources, not guesses about unseen items. Missing food in inventory does not itself call for crafting bread before finding simpler food. For positional tasks the final condition must describe the requested destination, not merely a height increase. A hilltop needs a grounded arrival on terrain with an observed crest, not a pillar or no available moves. Missing visibility requires continued observation. Exploratory requests may use a bounded useful survey; moving to new ground is not an unrequested extra action. Use game.routes and standing_surfaces for actual step access. Keep the floor of a step; dig only blocking body/head cells then walk onto it. Native harvest=false means breaking yields NO collectible materials; do not propose obtaining stone without a suitable owned or craftable pick. target_ref may only reference observations and guides approach; null means no currently visible target, continue useful observation/exploration. Nearby threatened survival interrupts work then resumes the root outcome. A route loop needs a different approach or newly exposed obstacle, not more turning. Respect permitted_means: when creatures=false, do not propose attacking neutral animals; ordinary find-food requests can seek crops/fruit/drops. Unknown distant resources are unknown, not unavailable forever. When a target is not yet visible keep searching with a concrete direction, re-evaluate upon discovery. question is null unless an actual human preference/restriction is needed; insufficient tools/materials/unknown location alone are your planning work. On replanning preserve the exact retained_outcome objective and success_checks. Explain what changed and the next approach in strategy without claiming actions already done. Use approach_results to distinguish successful intermediate actions from productive results: repeated harvesting with no desired inventory gain is evidence to compare alternatives, not proof of progress. Choose exploration/new opportunities when the current source has not yielded the outcome. Do not repeatedly return to a low-yield resource just because it remains visible.'
    last_error = None
    for attempt in range(2):
        try:
            reply = await mode.reasoning.complete(
                [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
                ],
                json_mode=True,
                max_tokens=1800,
                reasoning_effort="none",
                planning=True,
            )
            choice = reply["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Incomplete outcome plan")
            proposal = json.loads(choice["message"]["content"])
            mode.r.emit("voxel.outcome.proposed", proposal=proposal)
            species = {r["kind"] for r in context(snapshot)["entities"]}
            known = set(products) | set(registry) | species
            if isinstance(proposal.get("focus_items"), list):
                omitted = [i for i in proposal["focus_items"] if i not in known]
                if omitted:
                    mode.r.emit("voxel.outcome.unobserved_hints", items=omitted)
                proposal["focus_items"] = [
                    i for i in proposal["focus_items"] if i in known
                ]
            value = validate_plan(
                proposal, set(products) | set(registry) | species, prior
            )
            if any((p not in products for p in value.get("craft_products", []))):
                raise ValueError("Product has no native recipe")
            verification_state = {
                "current_request": task["request"],
                "objective": value["objective"],
                "success_checks": value["success_checks"],
                "strategy": value["strategy"],
                "craft_products": {
                    i: products.get(i) for i in value.get("craft_products", [])
                },
                "inventory": context(snapshot)["inventory"],
                "restrictions": task.get("constraints", []),
                "prior_outcome": prior and prior["objective"],
                "contract_meaning": "This is a proposed future test, NOT a claim already completed. use evidence contains the held item, exact target, before/after node IDs, count_before/count_after and native success; minimum is a hard lower bound. inventory_mode stock requires remaining units; gained requires additional collected units; consumed requires units used by confirmed native USE/PLACE. A description requiring consumption paired with stock/gained contradicts that test. Later a separate judgment checks actual receipts against descriptions, and a whole-request completion check runs independently.",
            }
            verification_state["tool_and_item_uses"] = {
                i: {
                    k: r[k]
                    for k in ("name", "usage", "possible_drops", "edible", "plant")
                    if k in r
                }
                for i, r in registry.items()
                if i
                in set(value.get("focus_items", []))
                | set(context(snapshot)["inventory"])
                and (r.get("usage") or r.get("edible") or r.get("plant"))
            }
            import asyncio

            check, mechanics, coverage = await asyncio.gather(
                mode.r.controller.judge_typed(
                    "voxel_outcome_plan_review",
                    {
                        k: v
                        for k, v in verification_state.items()
                        if k != "tool_and_item_uses"
                    },
                    {
                        "conflict": {
                            "type": "noul",
                            "instructions": "Does the proposed objective/strategy contradict an EXPLICIT requirement in the current_request or persistent restrictions, or does a success check use an evidence/count mode that contradicts its description? Inventory stock means remaining units, gained means newly acquired units, consumed means confirmed units used. Requiring remaining stock to prove consumption is a contradiction. Judge actual conflicts, wrong item/count or unrelated goals. A NEW request to eat overrides the previous temporary do-not-eat request; do not invent old restrictions. Routine means and choosing a sensible location are allowed. Missing details or several good alternatives are not a conflict.",
                        },
                        "product_mismatch": {
                            "type": "noul",
                            "instructions": "Does strategy promise to craft a missing tool/product but craft_products contains a DIFFERENT product (e.g. says make hoe but lists axe), or omits the missing product entirely? Existing owned tools require no crafting. A shovel makes a path, NOT farmland; a missing hoe for new farmland must be in craft_products unless the plan instead harvests mature crops to free enough existing farmland. Native recipe ingredients are automatic dependencies; only promised end-products need to be listed.",
                        },
                    },
                    deadline_ms=2600,
                ),
                mode.r.controller.judge_typed(
                    "voxel_plan_mechanics",
                    {
                        "strategy": value["strategy"],
                        "current_inventory": context(snapshot)["inventory"],
                        "actual_mining": [
                            {
                                k: r.get(k)
                                for k in ("item_id", "diggable", "dig_ms", "harvest")
                            }
                            for r in context(snapshot)["nodes"]
                        ],
                        "native_item_uses": verification_state["tool_and_item_uses"],
                        "native_recipes": {
                            i: recipe_facts.get(i)
                            for i in value.get("craft_products", [])
                        },
                    },
                    {
                        "contradiction": {
                            "type": "noul",
                            "instructions": "Does the proposed strategy contradict actual_mining, native item uses/drops or registered recipes? actual_mining.harvest=false means the currently held tool cannot obtain drops, even when diggable=true. A promise to obtain stone using that tool without preparing a suitable pick is a contradiction. Crafting consumes recipe ingredients: the ingredient on_place/usage documentation does not limit what recipes can use it. Raw ingredients being inedible does not prohibit making an edible product through a registered recipe. A tool used for an effect its native usage cannot provide is a contradiction. Judge concrete contradictions, not an uncertain route or an unmentioned feature.",
                        }
                    },
                    deadline_ms=1700,
                ),
                mode.r.controller.judge_typed(
                    "voxel_outcome_goal_coverage",
                    {
                        "user_request": task["request"],
                        "future_success_condition": "；".join(
                            (c["description"] for c in value["success_checks"])
                        ),
                        "registered_yields": {
                            i: r.get("possible_drops")
                            for i, r in registry.items()
                            if r.get("possible_drops")
                            and i in value.get("focus_items", [])
                        },
                    },
                    {
                        "coverage": {
                            "type": "choice",
                            "instructions": "Would the stated future_success_condition, if established by real game evidence, satisfy the final requested outcome? Harvesting a source produces its registered drop: pods yield beans, mature crops yield food/seeds, logs may yield their items. Acquiring that harvested product in inventory satisfies a request to collect that source. Tools, height and travel are intermediate means, not extra end results. A food category means any actually edible item. Preparation alone is insufficient.",
                            "criteria": {
                                "COVERS": "Condition covers requested result",
                                "MISSING": "An explicit requested result is missing",
                                "UNCLEAR": "Ambiguous",
                            },
                        },
                        "extra_final": {
                            "type": "noul",
                            "instructions": "Do the final success conditions add an end result absent from CURRENT user_request? Read its actual verbs: 找食物吃/找吃的并吃下 may include eating, whereas 找食物收进背包不要吃 excludes eating. Do not treat every food request as collection-only. Harvesting does not request replanting; crafting does not request placing unless stated. Intermediate preparations cannot become extra final requirements.",
                        },
                    },
                    deadline_ms=2200,
                )
                if not prior
                else asyncio.sleep(
                    0,
                    result={
                        "coverage": {"choice": "COVERS", "confidence": 1},
                        "extra_final": {"noul": 0},
                        "retained_verified_contract": True,
                    },
                ),
            )
            if mechanics["contradiction"]["noul"] >= 0.6:
                raise ValueError(
                    "Strategy contradicts native mechanics; use the registered item capabilities: "
                    + json.dumps(mechanics)
                )
            if 0.35 <= coverage["extra_final"]["noul"] <= 0.85:
                from .judgment import choose as check_extra

                resolved = await check_extra(
                    mode.reasoning,
                    "Does this plan add an unrequested FINAL deliverable? Bounded movement and surveying are ordinary interpretations of explore; preparation does not add a deliverable unless required as a separate final result. Keep restrictions exact.",
                    {"request": task["request"], "conditions": value["success_checks"]},
                    {
                        "OK": "No extra final deliverable",
                        "EXTRA": "Contains an unrequested final deliverable",
                    },
                )
                coverage["extra_final"]["noul"] = (
                    1 if resolved["choice"] == "EXTRA" else 0
                )
            if coverage["extra_final"]["noul"] >= 0.5:
                raise ValueError(
                    "Success conditions add a final action the user did not request. Remove that extra end result; retain only the requested outcome."
                )
            check.update(coverage)
            if (
                check["conflict"]["noul"] >= 0.5
                or check["product_mismatch"]["noul"] >= 0.7
            ):
                raise ValueError(
                    "Outcome contract needs revision: " + json.dumps(check)
                )
            if (
                check["coverage"]["choice"] != "COVERS"
                or check["coverage"]["confidence"] < 0.65
            ):
                from .judgment import choose as verify_coverage

                proof = await verify_coverage(
                    mode.reasoning,
                    "Verify coverage of the requested FINAL outcome. Harvesting a source gives its native registered drop (e.g. pods to beans); that product in inventory completes collecting the source. Climbing/tool preparation are means, not additional unmentioned results. APPROVE a complete result condition; REVISE only if an explicit desired final outcome/count is absent.",
                    {
                        "request": task["request"],
                        "success_conditions": value["success_checks"],
                        "source_drops": {
                            i: r.get("possible_drops")
                            for i, r in registry.items()
                            if r.get("possible_drops")
                            and i in value.get("focus_items", [])
                        },
                    },
                    {
                        "APPROVE": "All requested final outcomes are covered",
                        "REVISE": "An explicit requested final result is absent",
                    },
                )
                if proof["choice"] != "APPROVE":
                    raise ValueError(
                        "Outcome missing explicit result: " + proof["reason"]
                    )
                check["coverage_review"] = proof
            value["review"] = check
            observed = observations(snapshot)
            target_ref = value.get("target_ref")
            target = observed.get(target_ref) or next(
                (
                    r
                    for r in observed.values()
                    if target_ref and r.get("target_id") == target_ref
                ),
                None,
            )
            if not target:
                target = getattr(mode.operations, "entity_memory", {}).get(
                    value.get("target_ref")
                )
            if not target:
                target = next(
                    (
                        r
                        for r in memory.values()
                        if target_ref
                        and r.get("target_id") == target_ref
                        and (
                            str(r["position"])
                            not in getattr(mode.operations, "removed_resources", set())
                        )
                    ),
                    None,
                )
            value["target"] = copy.deepcopy(target) if target else None
            mode.r.emit("voxel.outcome.planned", plan=value, reason=reason)
            return value
        except (ValueError, KeyError, TypeError) as error:
            last_error = error
            data["rejected_proposal_reason"] = str(error)
            mode.r.emit("voxel.outcome.plan_retry", reason=str(error))
        except Exception as error:
            import asyncio
            import httpx

            if not isinstance(error, (TimeoutError, httpx.HTTPError)):
                raise
            last_error = error
            mode.r.emit(
                "voxel.outcome.service_retry",
                reason=type(error).__name__,
                attempt=attempt + 1,
            )
            if attempt < 1:
                await asyncio.sleep(0.3)
    raise last_error


def recipe_scope(mode, outcome):
    products = set(outcome.get("craft_products", []))
    seen = set()
    ids = []
    for r in mode.bridge.recipe_catalog.values():
        if r["item_id"] in products:
            signature = (
                r["item_id"],
                tuple(sorted((i for i in r["ingredients"] if i))),
            )
            if signature not in seen:
                seen.add(signature)
                ids.append(r["recipe_id"])
    return ids[:24]


async def permissions(mode, request, previous=None):
    a = await mode.r.controller.judge_typed(
        "voxel_outcome_means",
        {
            "current_request": request,
            "previous_request": previous.get("request") if previous else None,
        },
        {
            "terrain_restricted": {
                "type": "noul",
                "instructions": "Does the CURRENT request explicitly forbid digging/placing/altering ordinary terrain, or specifically require only picking up already dropped items? Only walking / do not break blocks / only use existing inventory is YES. “只收集到背包里/暂时别吃/先不管房子去找食物” limits the final use of collected items; it does NOT forbid normal harvesting or necessary access. A delegated result without an explicit terrain restriction is NO.",
            },
            "consume": {
                "type": "noul",
                "instructions": "Does the CURRENT request ask to actually EAT/consume food or restore hunger/health by eating, or delegate general autonomous survival play where eating is a routine means? Finding/harvesting/crafting edible food is NOT a request to eat it; 能吃的食物 describes a product, not eating. A previous food search supplies a noun for 吃一个, but no old eating instruction carries into a new task. Do-not-eat means NO.",
            },
            "creatures": {
                "type": "noul",
                "instructions": "Does the requested task permit harming non-hostile animals (e.g. explicitly hunt, kill, obtain meat)? Generic find food, gardening, exploration and general takeover do not by themselves request hunting neutral creatures. Respect do-not-harm restrictions.",
            },
        },
        deadline_ms=2200,
    )
    restricted = a["terrain_restricted"]["noul"]
    if 0.2 < restricted < 0.8:
        b = await mode.r.controller.judge_typed(
            "voxel_outcome_terrain_scope",
            {"request": request},
            {
                "scope": {
                    "type": "choice",
                    "instructions": "Resolve permitted physical means. 收集到背包里 means acquire by normal harvesting, not pick up only. 暂时不吃/不管房子 means no eating/building, not no harvest. Only explicit do-not-dig/only-pick-up-existing-drops/only-walk bars terrain work.",
                    "criteria": {
                        "NORMAL": "Normal harvesting and necessary local access are permitted",
                        "PRESERVE": "Explicitly must not change terrain",
                    },
                }
            },
            deadline_ms=1800,
        )
        restricted = (
            0
            if b["scope"]["choice"] == "NORMAL" and b["scope"]["confidence"] >= 0.75
            else 1
        )
    return {
        "alter": restricted <= 0.2,
        "creatures": a["creatures"]["noul"] >= 0.85,
        "consume": a["consume"]["noul"] >= 0.8,
        "model_outcome": True,
    }


async def authorized_plan(mode, task, snapshot, previous=None):
    means = await permissions(mode, task["request"], previous)
    framed = {**task, "scope": {**task.get("scope", {}), **means}}
    prior = (previous or {}).get("outcome")
    from .spatial import positional

    if (
        prior
        and positional(prior)
        and (not prior.get("craft_products"))
        and (previous.get("status") not in {"CANCELLED", "COMPLETED"})
    ):
        same = await mode.r.controller.judge_typed(
            "voxel_repeated_destination",
            {
                "current_request": task["request"],
                "previous_request": previous["request"],
                "retained_final_conditions": prior["success_checks"],
            },
            {
                "same": {
                    "type": "noul",
                    "instructions": "Does this CURRENT request explicitly delegate the same still-unfinished destination/outcome as the previous request, with the same final conditions? New destination, different count/extent, question, cancellation or changed final requirement is NO. Repeating please get me to the same hilltop is YES. Judge user semantics only, not previous execution success.",
                }
            },
            deadline_ms=1800,
        )
        if same["same"]["noul"] >= 0.9:
            value = copy.deepcopy(prior)
            value["target"] = None
            mode.r.emit(
                "voxel.outcome.retained",
                objective=value["objective"],
                reason="same_unfinished_destination",
                judgment=same,
            )
            return (value, means)
    for attempt in range(2):
        proposed = await plan(
            mode,
            framed,
            snapshot,
            previous,
            reason="new_request"
            if not attempt
            else "Remove unauthorized final consumption; the user requested obtaining/crafting food, not eating it.",
        )
        if not means["consume"]:
            check = await mode.r.controller.judge_typed(
                "voxel_plan_consumption_scope",
                {
                    "requested_result": proposed["objective"],
                    "checks": proposed["success_checks"],
                },
                {
                    "requires_eating": {
                        "type": "noul",
                        "instructions": "Does this proposed final result or any success check require eating/consuming food? Making an edible food and holding it does not require eating. References to an edible product or ability to eat it are NO; actually eating/consuming/restoring hunger is YES.",
                    }
                },
                deadline_ms=1400,
            )
            if check["requires_eating"]["noul"] >= 0.45:
                framed = {**framed, "outcome": None}
                continue
        return (proposed, means)
    raise ValueError("Planner added consumption outside the current request")


async def review(mode, task, snapshot):
    import asyncio

    outcome = task["outcome"]
    rows = evidence(task, snapshot)
    checks_data = {}
    for i, c in enumerate(outcome["success_checks"]):
        checks_data[f"check{i}"] = {
            "condition": c,
            "evidence": matching_evidence(c, rows),
        }

    async def check_condition(i, c):
        if not has_evidence(c, rows):
            return {"finished": {"noul": 0}, "count_insufficient": True}
        scoped = await counted_scope(
            mode, c, checks_data[f"check{i}"]["evidence"], snapshot
        )
        checks_data[f"check{i}"]["evidence"] = scoped
        if not has_evidence(c, scoped):
            return {"finished": {"noul": 0}, "count_insufficient": True}
        relevant = list(checks_data[f"check{i}"]["evidence"].values())
        if "position" in c["evidence"]:
            from .spatial import geometry

            position_state = {
                "actual_user_request": task["request"],
                "condition": c["description"],
                "start_position": task["start_position"],
                "current_observation": geometry(snapshot, task),
                "own_support_placements": [
                    r.get("target")
                    for r in task.get("effects", [])
                    if r["kind"] == "JUMP_PLACE"
                ][-6:],
            }
            result = await mode.r.controller.judge_typed(
                "voxel_position_condition",
                position_state,
                {
                    "result": {
                        "type": "choice",
                        "instructions": "Judge arrival at the local destination named by the user using the surveyed terrain and route. For a hilltop, standing on the highest natural plateau of the surveyed hillside after ascending its slope counts as arrival; summits can be flat. A temporary ledge below known higher terrain, our own pillar, or a blocked wall is MISSING. Do not demand proof that no taller mountain exists outside the surveyed hill. Use UNCLEAR for an ambiguous named destination or missing local geometry, not merely a finite sensor range. Apply the full user request, not a weaker height-gain condition.",
                        "criteria": {
                            "SATISFIED": "Actual arrival at requested destination is established",
                            "MISSING": "Requested destination not yet reached",
                            "UNCLEAR": "Insufficient observation to establish arrival",
                        },
                    }
                },
                deadline_ms=2200,
            )
            if (
                result["result"]["confidence"] < 0.7
                or result["result"]["choice"] == "UNCLEAR"
            ):
                from .judgment import choose

                checked = await choose(
                    mode.reasoning,
                    "Decide arrival at the requested LOCAL destination, using the full surveyed_surface_levels and ascent_history. A hilltop may be a flat plateau: if the player ascended the surveyed slope and is standing on its highest natural plateau, that is SATISFIED. Do not require a pointed peak or global map. A higher known continuation of this same hill, an own pillar, or an obstructing wall means MISSING. Unknown places outside the surveyed hillside do not by themselves prevent arrival. Only use UNCLEAR for unresolved local destination/geometry. Player feet are 0.5 above supporting block centers. Honor the actual request even if the proposed condition is weaker.",
                    position_state,
                    {
                        "SATISFIED": "Requested destination reached",
                        "MISSING": "Still needs to reach destination",
                        "UNCLEAR": "Need another observation",
                    },
                )
                result = {
                    "result": {"choice": checked["choice"], "confidence": 1},
                    "review": checked,
                }
                mode.r.emit(
                    "voxel.position.review", result=checked, observation=position_state
                )
            return result
        check_state = {
            "requested": c["description"],
            "required_count": c.get("minimum", 1),
            "inventory_mode": c.get("inventory_mode", "stock"),
            "actual_evidence": [
                r for r in relevant if not r.get("summary_of_receipts")
            ],
            "total_confirmed_matching_effects": max(
                sum(
                    (
                        r.get("confirmed_distinct_effects", 0)
                        for r in relevant
                        if r.get("summary_of_receipts")
                    )
                ),
                len(
                    {
                        r["target_id"]
                        for r in relevant
                        if r["kind"] == "defeat" and r.get("target_id")
                    }
                ),
            ),
            "distinct_confirmed_effect_counts": [
                r for r in relevant if r.get("summary_of_receipts")
            ],
        }
        if "inventory" in c["evidence"]:
            carried = [r for r in relevant if r["kind"] == "inventory"]
            deaths = [r for r in relevant if r["kind"] == "defeat"]
            crafts = []
            for r in task.get("effects", []):
                if r["kind"] == "CRAFT":
                    recipe = mode.bridge.recipe_catalog.get(r.get("recipe_id")) or next(
                        (
                            v
                            for v in mode.bridge.recipe_catalog.values()
                            if v["item_id"] == r.get("item_id")
                        ),
                        None,
                    )
                    crafts.append(
                        {
                            "product": r.get("item_id"),
                            "count": r.get("count"),
                            "native_ingredients": recipe and recipe["ingredients"],
                        }
                    )
            return await mode.r.controller.judge_typed(
                "voxel_outcome_inventory_condition",
                {
                    "requested": c["description"],
                    "required_count": c.get("minimum", 1),
                    "inventory_mode": c.get("inventory_mode", "stock"),
                    "current_inventory": [
                        {
                            k: v
                            for k, v in {
                                **r,
                                "newly_obtained": r.get("gained", 0),
                            }.items()
                            if k
                            in {
                                "item_id",
                                "name",
                                "count",
                                "newly_obtained",
                                "consumed",
                                "edible",
                            }
                        }
                        for r in carried
                    ],
                    "native_consumption_receipts": [
                        r
                        for r in task.get("effects", [])
                        if r["kind"] in {"USE", "PLACE"}
                        and (not c.get("item_ids") or r.get("item_id") in c["item_ids"])
                    ][-24:],
                    "native_confirmed_kills": deaths,
                    "native_crafting_receipts": crafts[-4:],
                },
                {
                    "result": {
                        "type": "choice",
                        "instructions": "Does the inventory evidence satisfy the requested condition under inventory_mode? stock compares remaining count; gained compares newly_obtained units already picked up, not world drops; consumed compares consumed units from confirmed native consumption receipts, even when count is zero. A decrease without such receipts is not consumption. For a condition also requiring planting/eating or another result, verify the actual receipt effects; consumption alone is insufficient. A condition also requiring a kill must have native_confirmed_kills. Do not require a separate pickup button receipt; the game attracts drops automatically. For food only edible items qualify.",
                        "criteria": {
                            "SATISFIED": "Requested inventory condition and associated result are established",
                            "MISSING": "Required condition/count is missing",
                            "UNCLEAR": "Evidence does not establish it",
                        },
                    }
                },
                deadline_ms=2200,
            )
        if set(c["evidence"]) == {"defeat"}:
            check_state["related_inventory"] = [
                {k: r[k] for k in ("item_id", "name", "count", "gained") if k in r}
                for r in rows.values()
                if r["kind"] == "inventory" and r.get("gained", 0) > 0
            ]
        return await mode.r.controller.judge_typed(
            "voxel_outcome_condition",
            check_state,
            {
                "result": {
                    "type": "choice",
                    "instructions": "Does the actual evidence satisfy the requested condition with required_count? These are confirmed native effects or current inventory. native_death_confirmed means the creature was killed, not just hit. Inventory gained is newly collected units. For use, above_before/above_after shows a crop appeared, count_before/count_after consumption. Compare distinct effects with required_count. Summary counts summarize detail rows; never add both. Preparation alone is insufficient.",
                    "criteria": {
                        "SATISFIED": "The requested condition is established by evidence",
                        "MISSING": "The required result/count is still missing",
                        "UNCLEAR": "Evidence does not establish it",
                    },
                }
            },
            deadline_ms=2200,
        )

    questions = {}
    questions["strategy_fit"] = {
        "type": "noul",
        "instructions": "Does the current strategy still offer a productive next step toward the final outcome with the new observations? A newly found target should replace aimless search. Repeated no-effect use/movement, an unmet prerequisite, or no practical next step means NO; normal progress is YES.",
    }
    questions["progress"] = {
        "type": "score",
        "instructions": "How far has this task advanced according to real evidence, judged against its final user outcome?",
        "criteria": [
            "No relevant progress, or only failed/repeated actions",
            "Useful preparation or genuinely new search observations, final outcome still missing",
            "Part of the requested final result is proven, remaining result still missing",
            "The whole requested final result is proven by current evidence",
        ],
    }
    results = await asyncio.gather(
        *(check_condition(i, c) for i, c in enumerate(outcome["success_checks"])),
        mode.r.controller.judge_typed(
            "voxel_outcome_progress",
            {
                "request": task["request"],
                "outcome": outcome["objective"],
                "strategy": outcome["strategy"],
                "checks": {
                    k: {"condition": v["condition"]} for k, v in checks_data.items()
                },
                "baseline_inventory": task.get("baseline_inventory"),
                "player": context(snapshot)["player"],
                "observed_nodes": context(snapshot)["nodes"][:16],
                "attempts": task.get("attempts", [])[-4:],
                "stalled_s": task.get("stalled_s", 0),
                "approach_results": approach_history(task, context(snapshot)),
            },
            questions,
            deadline_ms=2600,
        ),
    )
    a = results[-1]
    for i, result in enumerate(results[:-1]):
        a[f"check{i}"] = result.get("result", {"choice": "MISSING", "confidence": 1})
    checks = [
        has_evidence(c, checks_data[f"check{i}"]["evidence"])
        and a[f"check{i}"]["choice"] == "SATISFIED"
        and (a[f"check{i}"]["confidence"] >= 0.8)
        for i, c in enumerate(outcome["success_checks"])
    ]
    done = all(checks)
    plausible = all(
        (
            has_evidence(c, checks_data[f"check{i}"]["evidence"])
            and a[f"check{i}"]["choice"] == "SATISFIED"
            for i, c in enumerate(outcome["success_checks"])
        )
    )
    if plausible and (not done):
        mode.r.emit(
            "voxel.outcome.review",
            objective=outcome["objective"],
            answers=a,
            complete=False,
            evidence=rows,
        )
        return {
            "complete": False,
            "replan": False,
            "needs_completion_review": True,
            "progress": a["progress"],
            "checks": checks,
        }
    if done:
        registry = {
            **getattr(mode.operations, "native_item_memory", {}),
            **facts(snapshot),
        }
        proof = await mode.r.controller.judge_typed(
            "voxel_outcome_completion",
            {
                "user_request": task["request"],
                "independently_verified_outcome": "；".join(
                    (c["description"] for c in outcome["success_checks"])
                ),
                "confirmed_source_work": [
                    {
                        **{k: r[k] for k in ("kind", "above_after", "count") if k in r},
                        "item": registry.get(r.get("item_id"), {}).get(
                            "name", r.get("item_id")
                        ),
                        "registered_drops": [
                            registry.get(i, {}).get("name", i)
                            for i in registry.get(r.get("item_id"), {}).get(
                                "possible_drops"
                            )
                            or []
                        ],
                    }
                    for r in task.get("effects", [])
                    if r["kind"] in {"DIG", "CRAFT", "USE", "ATTACK"}
                ][-8:],
                "position_evidence": [
                    r for r in rows.values() if r["kind"] == "position"
                ],
                "confirmed_backpack": [
                    {
                        "item": r.get("name", r["item_id"]),
                        "count": r["count"],
                        "newly_acquired": r.get("gained", 0),
                    }
                    for r in rows.values()
                    if r["kind"] == "inventory" and r.get("gained", 0) > 0
                ],
            },
            {
                "missing": {
                    "type": "noul",
                    "instructions": "Does an explicitly requested FINAL result remain missing after the independently verified facts? Harvesting a source yields its registered drop, not necessarily a same-named block. If that source was harvested and its drops are in the backpack, collection succeeded. Tools/climbing/travel are means, not extra deliverables. Unrequested extra tasks do not count as missing. For positional tasks inspect position_evidence: climbing one block or standing on our pillar does not prove reaching the requested hilltop or upper ledge; observed higher terrain contradicts summit completion. Missing geometry is not evidence of arrival. YES for an unmet explicit result/count or restriction.",
                }
            },
            deadline_ms=1700,
        )
        done = proof["missing"]["noul"] <= 0.2
        a["whole_request"] = proof
        if not done:
            a["coverage_uncertain"] = True
            return {
                "complete": False,
                "replan": False,
                "needs_completion_review": True,
                "progress": a["progress"],
                "checks": checks,
            }
    mode.r.emit(
        "voxel.outcome.review",
        objective=outcome["objective"],
        answers=a,
        complete=done,
        evidence=rows,
    )
    return {
        "complete": done,
        "replan": a["strategy_fit"]["noul"] < 0.45 or task.get("stalled_s", 0) >= 15,
        "progress": a["progress"],
        "checks": checks,
    }


def action_options(task, snapshot, actions, visited):
    from .locomotion import cell, distance

    game = context(snapshot)
    registry = facts(snapshot)
    outcome = task["outcome"]
    pos = game["player"]["position"]
    target = outcome.get("target")
    interest = target and target.get("position")
    options = {}
    wanted = [interest] if interest else []
    bearing = task.setdefault("navigation", {}).get("bearing", task.get("start_yaw", 0))
    for i, a in enumerate(actions):
        if (
            a["kind"] == "USE"
            and a.get("interaction") == "self"
            and (task.get("scope", {}).get("consume") is False)
        ):
            continue
        row = {
            k: v
            for k, v in a.items()
            if k
            not in {
                "action_id",
                "target_index",
                "route_id",
                "passage_depth",
                "distance_from_passage",
            }
        }
        f = registry.get(a.get("item_id"), {})
        if f:
            row["item_name"] = f.get("name")
        if a["kind"] == "DIG":
            row.update(possible_drops=f.get("possible_drops"), maturity=f.get("plant"))
        if a["kind"] == "ATTACK":
            row["item_id"] = a.get("entity_kind")
        if a["kind"] == "CRAFT":
            requirements = (snapshot["view"].get("survival", {}).get("task") or {}).get(
                "craft_requirements", []
            )
            row["dependency_product"] = next(
                (
                    r["item_id"]
                    for r in requirements
                    if r["recipe_id"] == a.get("dependency_for")
                ),
                None,
            )
            row["native_prerequisite"] = bool(a.get("dependency_for"))
        if a["kind"] == "USE":
            target_facts = registry.get(a.get("target_item_id"), {})
            row["target_name"] = target_facts.get("name", a.get("target_item_id"))
            row["target_groups"] = {
                k: v
                for k, v in target_facts.get("groups", {}).items()
                if k in {"soil", "cultivatable", "plant", "water", "solid"}
            }
        p = a.get("target")
        if p:
            row["visits"] = visited.get(cell(p), 0)
            if wanted:
                row["distance_to_focus"] = round(
                    min((distance(p, x) for x in wanted)), 2
                )
            row["distance_from_start"] = round(distance(p, task["start_position"]), 2)
            row["forward"] = round(
                -(p["x"] - pos["x"]) * math.sin(bearing)
                + (p["z"] - pos["z"]) * math.cos(bearing),
                2,
            )
        options[str(i)] = row
    waypoint = task.get("navigation", {}).get("waypoint")
    if waypoint:
        blockers = [r["position"] for r in waypoint.get("blockers", [])]
        for row in options.values():
            row["on_selected_route"] = bool(
                row.get("target") in blockers
                or row.get("target") == waypoint.get("target")
            )
    return options


async def choose(mode, task, snapshot, actions, recent, visited):
    outcome = task["outcome"]
    game = context(snapshot)
    options = action_options(task, snapshot, actions, visited)
    from . import spatial

    if spatial.positional(outcome):
        return await spatial.choose(mode, task, snapshot, actions, recent, visited)
    wanted = {
        i
        for c in outcome.get("success_checks", [])
        if "place" in c["evidence"]
        for i in c.get("item_ids", [])
    }
    placed = {
        tuple((r["target"][k] for k in ("x", "y", "z")))
        for r in task.get("effects", [])
        if r["kind"] == "PLACE" and r.get("item_id") in wanted and r.get("target")
    }
    options = {
        k: r
        for k, r in options.items()
        if not (
            r["kind"] == "DIG"
            and r.get("target")
            and (tuple((r["target"][a] for a in ("x", "y", "z"))) in placed)
        )
    }
    registry = {**getattr(mode.operations, "native_item_memory", {}), **facts(snapshot)}
    desired = {i: 1 for i in outcome.get("craft_products", [])}
    explicit = task.get("resolved_product")
    if explicit:
        desired[explicit["item_id"]] = (
            task.get("baseline_inventory", {}).get(explicit["item_id"], 0)
            + explicit["quantity"]
        )
    pending_products = [
        i for i, n in desired.items() if game["inventory"].get(i, 0) < n
    ]
    native = snapshot["view"].get("survival", {}).get("task") or {}
    requirements = [
        r
        for r in native.get("craft_requirements", [])
        if r.get("item_id") in pending_products
    ]
    recipe_ids = {r["recipe_id"] for r in requirements}
    dependency_options = {
        k: r
        for k, r in options.items()
        if r["kind"] == "CRAFT"
        and (
            r.get("item_id") in pending_products
            or r.get("dependency_for") in recipe_ids
        )
    }
    station_needed = any((r.get("workbench_required") for r in requirements))
    if station_needed and (not any((r.get("missing") for r in requirements))):
        dependency_options.update(
            {
                k: r
                for k, r in options.items()
                if r["kind"] in {"PLACE", "SELECT", "EQUIP"}
                and r.get("item_id") == "mcl_crafting_table:crafting_table"
            }
        )
    if dependency_options:
        return await mode.r.controller.judge_typed(
            "voxel_outcome_recipe_step",
            {
                "root_request": task["request"],
                "model_selected_products": pending_products,
                "inventory": game["inventory"],
                "native_requirements": requirements,
                "held": game["player"].get("wielded"),
            },
            {
                "action": {
                    "type": "choice",
                    "instructions": "Advance the model-selected product through its real native recipe dependencies. Each offered CRAFT can run one batch now. If a carried workbench must be placed for a 3x3 recipe, select/place it. Choose a concrete prerequisite; do not demand the entire bill in one batch. Once the selected product is in inventory, the actor returns to the root goal. These options already derive from native recipes, not tactical guesses.",
                    "criteria": dependency_options,
                }
            },
            deadline_ms=1800,
        )
    self_uses = {
        k: r
        for k, r in options.items()
        if r["kind"] == "USE" and r.get("interaction") == "self"
    }
    if task.get("scope", {}).get("consume"):
        self_uses.update(
            {
                k: r
                for k, r in options.items()
                if r["kind"] in {"SELECT", "EQUIP"}
                and registry.get(r.get("item_id"), {}).get("edible")
            }
        )
    if self_uses:
        a = await mode.r.controller.judge_typed(
            "voxel_outcome_ready_use",
            {
                "request": task["request"],
                "held": game["player"].get("wielded"),
                "hunger": game["player"].get("hunger"),
            },
            {
                "action": {
                    "type": "choice",
                    "instructions": "Choose a carried-food action to satisfy the current request. All shown items are already in inventory and natively edible. SELECT/EQUIP means take that food into your hand; USE means eat it. To eat one, first equip a carried food if necessary, then use it, rather than go after more drops. Follow any explicit food preference and native risk.",
                    "criteria": (
                        {}
                        if task.get("scope", {}).get("consume")
                        else {"SKIP": "Self-use not requested/appropriate"}
                    )
                    | self_uses,
                }
            },
            deadline_ms=1400,
        )
        if a["action"]["choice"] != "SKIP":
            return a
    combat = {k: r for k, r in options.items() if r["kind"] == "ATTACK"}
    if combat:
        combat.update(
            {
                k: r
                for k, r in options.items()
                if r["kind"] in {"SELECT", "EQUIP"} and r.get("tool")
            }
        )
        judgment = await mode.r.controller.judge_typed(
            "voxel_outcome_combat_opportunity",
            {
                "request": task["request"],
                "strategy": outcome["strategy"],
                "constraints": task.get("constraints", []),
                "held": game["player"].get("wielded"),
                "hp": game["player"].get("hp"),
                "visible_creatures": game["entities"],
                "recent": list(recent)[-3:],
            },
            {
                "action": {
                    "type": "choice",
                    "instructions": "A visible creature is within native attack reach NOW. If defeating it serves the current request and is permitted, ATTACK it or first equip an actually stronger carried weapon. Do not spend time moving closer when already in reach. Respect explicit not-to-kill restrictions. SKIP if combat is unrelated or retreat is needed; then the general planner chooses.",
                    "criteria": {
                        "SKIP": "Combat does not serve this request right now",
                        **combat,
                    },
                }
            },
            deadline_ms=1400,
        )
        if judgment["action"]["choice"] != "SKIP":
            return judgment
    observed_target = outcome.get("target")
    if observed_target and observed_target.get("position"):
        fresh = next(
            (
                r
                for r in game["entities"]
                if r.get("target_id") == observed_target.get("target_id")
            ),
            None,
        ) or next(
            (
                r
                for r in game["nodes"] + game["drops"]
                if r.get("position") == observed_target["position"]
            ),
            None,
        )
        if fresh:
            observed_target = {
                **observed_target,
                **fresh,
                "historical_intent_item": observed_target.get("item_id"),
            }
    if not observed_target:
        creatures = {
            r["target_id"]: r
            for r in [
                *getattr(mode.operations, "entity_memory", {}).values(),
                *game["entities"],
            ]
            if r.get("target_id") not in task.get("defeated", [])
        }
        if creatures:
            target_choice = await mode.r.controller.judge_typed(
                "voxel_current_creature_target",
                {"request": task["request"], "strategy": outcome["strategy"]},
                {
                    "target": {
                        "type": "choice",
                        "instructions": "Does a visible or previously observed creature match the requested result? Select the actual matching target to approach; historical positions require reobservation. NONE when unrelated. This is a target reference, not permission to attack.",
                        "criteria": {
                            "NONE": "No relevant creature",
                            **{
                                k: {
                                    f: v.get(f)
                                    for f in (
                                        "kind",
                                        "relation",
                                        "position",
                                        "possible_drops",
                                        "last_seen",
                                    )
                                }
                                for k, v in creatures.items()
                            },
                        },
                    }
                },
                deadline_ms=1500,
            )
            observed_target = creatures.get(target_choice["target"]["choice"])
            if observed_target:
                outcome["target"] = copy.deepcopy(observed_target)
    if observed_target and observed_target.get("relation") and (not combat):
        from .locomotion import distance

        target = observed_target["position"]
        current_distance = distance(game["player"]["position"], target)
        moves = {
            k: {
                **r,
                "distance_to_tracked_target": round(distance(r["target"], target), 2),
            }
            for k, r in options.items()
            if r["kind"] in {"MOVE", "JUMP_MOVE", "SWIM"}
        }
        if moves:
            answer = await mode.r.controller.judge_typed(
                "voxel_track_target_motion",
                {
                    "request": task["request"],
                    "target": observed_target,
                    "current_distance": round(current_distance, 2),
                    "hp": game["player"].get("hp"),
                    "constraints": task.get("constraints", []),
                },
                {
                    "action": {
                        "type": "choice",
                        "instructions": "Follow this model-selected moving creature toward the user outcome. Choose a currently legal movement reducing distance_to_tracked_target. Prefer close approach over further planning while the target moves. Historical last-seen position is a search clue only; attack still needs fresh native reach. SKIP only when pursuit is unrelated or unsafe.",
                        "criteria": {
                            "SKIP": "Use another means instead of following now",
                            **moves,
                        },
                    }
                },
                deadline_ms=1200,
            )
            if answer["action"]["choice"] != "SKIP":
                answer["tracked_target"] = observed_target
                return answer
    if (
        observed_target
        and observed_target.get("position")
        and (not observed_target.get("relation"))
    ):
        access = await choose_access(
            mode, task, game, options, observed_target, recent, registry
        )
        if access:
            return access
    if task.get("stalled_s", 0) >= 12 or task.get("local_loop"):
        from .judgment import choose as reasoned_choice

        reviewed = await reasoned_choice(
            mode.reasoning,
            "The current approach has not advanced the retained outcome. Choose ONE concrete next legal action, not a new speech plan. Use actual target reach/height, successful effects, native item uses and failures. A route blocker may need DIG before MOVE. Our own pillar can be lowered one safe level with DIG_DOWN. After harvesting, approach actual drops; do not keep building toward a removed source. Never repeat ineffectual movement/tool swaps just because they were technically successful. REPLAN only when missing tools/products or a different objective strategy is necessary.",
            {
                "request": task["request"],
                "strategy": outcome["strategy"],
                "target": observed_target,
                "player": game["player"],
                "inventory": game["inventory"],
                "nodes": game["nodes"][:12],
                "drops": game["drops"],
                "routes": game["navigation"],
                "recent_attempts": list(recent)[-6:],
            },
            {"REPLAN": "A missing prerequisite/strategy must be planned", **options},
        )
        return {
            "action": {"choice": reviewed["choice"], "confidence": 1},
            "recovery": reviewed,
        }
    state = {
        "user_outcome": outcome["objective"],
        "current_strategy": outcome["strategy"],
        "remaining_success_conditions": outcome["success_checks"],
        "constraints": task.get("constraints", []),
        "permissions": {k: task["scope"].get(k) for k in ("alter", "creatures")},
        "player": game["player"],
        "inventory": game["inventory"],
        "inventory_uses": {
            i: {k: r[k] for k in ("name", "edible", "food_risk", "usage") if k in r}
            for i, r in registry.items()
            if i in game["inventory"]
        },
        "target": observed_target,
        "focus_items": outcome.get("focus_items"),
        "observed_nodes_now": [
            {k: n[k] for k in ("item_id", "position") if k in n}
            for n in game["nodes"][:10]
        ],
        "drops": game["drops"],
        "entities": game["entities"],
        "routes": game["navigation"],
        "craft_requirements": (
            snapshot["view"].get("survival", {}).get("task") or {}
        ).get("craft_requirements", []),
        "recent_actions": [
            {
                k: v
                for k, v in r.items()
                if k in {"kind", "item_id", "target", "status", "reason"}
            }
            for r in list(recent)[-3:]
        ],
        "recent_effects": [
            {
                k: v
                for k, v in r.items()
                if k
                in {
                    "kind",
                    "item_id",
                    "target",
                    "above_after",
                    "node_after",
                    "count_after",
                }
            }
            for r in task.get("effects", [])[-2:]
        ],
    }
    from collections import Counter

    state["confirmed_effect_totals"] = [
        {
            "kind": key[0],
            "item_id": key[1],
            "result_node": key[2],
            "result_above": key[3],
            "count": count,
        }
        for key, count in Counter(
            (
                (r["kind"], r.get("item_id"), r.get("node_after"), r.get("above_after"))
                for r in task.get("effects", [])
            )
        ).items()
    ]
    state["observed_node_counts"] = dict(Counter((r["item_id"] for r in game["nodes"])))
    instructions = "Choose one currently offered native action that best advances the retained user outcome using the strategy and game facts. Plans can be revised: use a newly observed useful opportunity. A visible wanted drop must actually enter inventory; walk toward it. USE on a node invokes the held item normal placement/use callback: select a suitable held item and target using item_usage/target_groups. Do not dig young crops to obtain food. Harvest mature crops/fruit when useful; do not eat seeds or craft unrelated supplies. Use item_usage and actual target_groups before acting: seed use on soil group<2 cannot plant. Already prepared clear farmland should be used promptly instead of preparing an unnecessary surplus; dry unused farmland can revert. Preserve crops already planted. CRAFT options are real recipe batches/dependencies for your chosen products. When a required table is carried, select/place it instead of waiting or making more. Move toward an observed target until reachable; expose the relevant blockers if allowed. SEARCH should inspect unvisited ground continuing a direction, not turn or bounce forever. SELECT/EQUIP only when it enables a needed next operation. Owned tools count as available even when not held: switch to them to prepare what is missing. Actual damage/drowning may interrupt, then the root goal remains. Do not request REPLAN for ordinary execution of the same strategy, such as switching to an owned hoe when no suitable farmland remains, or moving toward a target. REPLAN only if genuinely different unprovided tools/recipes or strategy are needed; WAIT only for transient cooldown/observation. Native not_visible_or_reachable is a range/line-of-sight limitation at THIS position, not proof that the target is unreachable; approach a useful work stance on observed supported routes. JUMP_PLACE raises your support by one carried block through real jumping; choose it for access to elevated targets, after equipping an ordinary support block if needed. DIG_DOWN removes one own scaffold block and descends to the verified support below, allowing return to ground. Check confirmed_effect_totals and observed_node_counts: once prerequisites already exist, switch tools/items to perform the FINAL outcome. Do not keep producing more prerequisites while the final result remains missing. Respect all user exclusions. Unknown world state is not a completed goal."
    from .locomotion import choose_route, route_actions

    routes = {}
    for i, route in enumerate(game["navigation"]):
        if route.get("protected"):
            continue
        valid = route_actions(route, actions)
        if not valid:
            continue
        target = route["target"]
        from .locomotion import cell

        routes[str(i)] = {
            **route,
            "visits": visited.get(cell(target), 0),
            "new_ground": cell(target) not in task.get("progress", {}).get("cells", []),
            "available_kinds": sorted({a["kind"] for a in valid}),
        }
    if routes:
        mode_question = await mode.r.controller.judge_typed(
            "voxel_outcome_local_means",
            {
                "goal": outcome["objective"],
                "strategy": outcome["strategy"],
                "position": game["player"]["position"],
                "approach_results": approach_history(task, game),
                "visible_items": [
                    {"item_id": r["item_id"], "position": r["position"]}
                    for r in game["nodes"][:12]
                ],
                "inventory": game["inventory"],
                "held_item": game["player"].get("wielded"),
                "visible_creatures": game["entities"],
                "available_attacks": [
                    r for r in options.values() if r["kind"] == "ATTACK"
                ],
                "drops": game["drops"],
                "route_options": routes,
                "recent_moves": [
                    r for r in list(recent)[-5:] if r["kind"] in {"MOVE", "JUMP_MOVE"}
                ],
                "ready_effect_items": list(
                    {
                        json.dumps(
                            {
                                k: r.get(k)
                                for k in (
                                    "kind",
                                    "item_id",
                                    "target_item_id",
                                    "possible_drops",
                                    "maturity",
                                )
                            },
                            sort_keys=True,
                        ): {
                            k: r.get(k)
                            for k in (
                                "kind",
                                "item_id",
                                "item_name",
                                "target_item_id",
                                "possible_drops",
                                "maturity",
                            )
                        }
                        for r in options.values()
                        if r["kind"] in {"DIG", "USE", "CRAFT", "JUMP_PLACE"}
                    }.values()
                ),
                "item_facts": {
                    i: {
                        k: r[k]
                        for k in ("name", "edible", "plant", "possible_drops")
                        if k in r
                    }
                    for i, r in registry.items()
                    if r.get("edible") or r.get("plant")
                },
                "already_observed_search_is_only_a_means": True,
            },
            {
                "means": {
                    "type": "choice",
                    "instructions": "Choose the local means needed NOW. WORK if a directly useful item/resource/plant operation, an available attack on the requested creature, crafting a required product, or equipping its tool can advance the outcome here. All recipe products needed by the current strategy must be prepared through WORK. ROUTE when useful resources/space are absent or unreachable: select and open a route to new ground, instead of shuffling in the same confined area. Do not walk onto the crop/field you want to operate on; work from beside it. All terrain changes obey user restrictions.",
                    "criteria": {
                        "WORK": "Choose an item/resource interaction here",
                        "ROUTE": "Choose an adjacent route and make it traversable if needed",
                    },
                }
            },
            deadline_ms=1800,
        )
        if mode_question["means"]["choice"] == "ROUTE":
            nav_task = copy.deepcopy(task)
            nav_task["agent_goal"] = "EXPLORE"
            nav_task["stage_goal"] = {"label": outcome["strategy"]}
            destination = observed_target
            if observed_target:
                nav_task["return_site"] = observed_target["position"]
                nav_task["agent_goal"] = "RETURN_SITE"
            if not observed_target:
                groups = {}
                remembered = list(
                    getattr(mode.operations, "observation_memory", {}).values()
                )
                for row in game["nodes"] + game["drops"] + remembered:
                    if str(row["position"]) in getattr(
                        mode.operations, "removed_resources", set()
                    ):
                        continue
                    groups.setdefault(row["item_id"], []).append(row)
                options_by_item = {
                    i: {
                        "name": registry.get(i, {}).get("name", i),
                        "possible_drops": registry.get(i, {}).get("possible_drops"),
                        "plant": registry.get(i, {}).get("plant"),
                        "edible": registry.get(i, {}).get("edible"),
                        "drop_food_facts": [
                            {k: registry.get(d, {}).get(k) for k in ("name", "edible")}
                            for d in registry.get(i, {}).get("possible_drops") or []
                        ],
                    }
                    for i in groups
                }
                if options_by_item:
                    found = await mode.r.controller.judge_typed(
                        "voxel_outcome_visible_destination",
                        {
                            "request": task["request"],
                            "selected_strategy": outcome["strategy"],
                            "approach_results": approach_history(task, game),
                        },
                        {
                            "item": {
                                "type": "choice",
                                "instructions": "Which observed source is worth approaching NOW for this outcome and strategy? Use registered drops and actual approach_results. A source may theoretically yield the goal but repeated work without desired gains can make exploring new ground better; choose NONE in that case. Respect the strategy to leave an unproductive area rather than retarget the same nearby source. A source need not itself be edible if its registered drops are useful. Historical resources are search clues, not fresh execution targets.",
                                "criteria": {
                                    "NONE": "No currently useful source in this observation",
                                    **options_by_item,
                                },
                            }
                        },
                        deadline_ms=1700,
                    )
                    item = found["item"]["choice"]
                    if item in groups and found["item"]["confidence"] < 0.65:
                        from .judgment import choose as reasoned_choice

                        confirmed = await reasoned_choice(
                            mode.reasoning,
                            "Choose an observed source worth approaching for the requested outcome. These are native item/drop facts. Optional focus_items may name tools or obstacles, not destinations. NONE if none is useful. This selects a search destination; subsequent work still requires fresh native reach and the current user scope.",
                            {
                                "request": task["request"],
                                "strategy": outcome["strategy"],
                                "initial_judgment": found,
                                "approach_results": approach_history(task, game),
                            },
                            {"NONE": "No useful observed source", **options_by_item},
                        )
                        item = confirmed["choice"]
                    if item in groups:
                        from .locomotion import distance

                        target = min(
                            groups[item],
                            key=lambda r: distance(
                                game["player"]["position"], r["position"]
                            ),
                        )
                        nav_task["return_site"] = target["position"]
                        nav_task["agent_goal"] = "RETURN_SITE"
                        destination = target
            nav_task["observed_terrain"] = (
                snapshot["view"].get("construction", {}).get("terrain", [])
            )
            route = await choose_route(
                mode.r.controller, game, nav_task, actions, visited, [], []
            )
            valid = route_actions(route, actions)
            if valid:
                choices = {
                    str(i): options[str(i)]
                    for i, a in enumerate(actions)
                    if a in valid and str(i) in options
                }
                a = await mode.r.controller.judge_typed(
                    "voxel_outcome_route_action",
                    {
                        "goal": outcome["objective"],
                        "selected_route": route,
                        "recent": list(recent)[-3:],
                        "held": game["player"].get("wielded"),
                    },
                    {
                        "action": {
                            "type": "choice",
                            "instructions": "Execute the selected route toward the retained outcome. If MOVE/JUMP_MOVE reaches it now, move. Otherwise DIG its exposed blockers or SELECT the tool needed. The final user goal remains after reaching this waypoint; do not complete after clearing the route.",
                            "criteria": {
                                "WAIT": "No useful route action now",
                                **choices,
                            },
                        }
                    },
                    deadline_ms=1900,
                )
                a["navigation"] = nav_task.get("navigation", {})
                if destination:
                    a["tracked_target"] = copy.deepcopy(destination)
                    if destination in game["drops"]:
                        a["tracked_target"]["evidence"] = "drop"
                return a
    use_groups = {}
    for key, row in options.items():
        if row["kind"] == "USE":
            use_groups.setdefault(
                (row.get("item_id"), row.get("interaction"), row.get("target_item_id")),
                [],
            ).append(key)
    compatibility = {}
    if use_groups:
        qs = {}
        descriptions = {}
        for i, (group, keys) in enumerate(use_groups.items()):
            key = "u" + str(i)
            row = options[keys[0]]
            descriptions[key] = {
                "item": registry.get(group[0], {}).get("name", group[0]),
                "usage": registry.get(group[0], {}).get("usage"),
                "interaction": group[1],
                "target_name": row.get("target_name"),
                "target_groups": row.get("target_groups"),
            }
            qs[key] = {
                "type": "noul",
                "instructions": f"Does interaction {key} have the right target type according to usage? Self-use means eating. Seeds require farmland soil>=2; grass/dirt soil=1 cannot plant. Judge compatibility only.",
            }
        judged = await mode.r.controller.judge_typed(
            "voxel_item_affordance", descriptions, qs, deadline_ms=2000
        )
        for i, (group, keys) in enumerate(use_groups.items()):
            valid = judged["u" + str(i)]["noul"] >= 0.7
            compatibility[str(group)] = {"compatible": valid}
            if not valid:
                for key in keys:
                    options.pop(key, None)
        state["item_compatibility"] = compatibility
    families = {}
    for key, row in options.items():
        family = (
            "EQUIP"
            if row["kind"] in {"SELECT", "EQUIP"}
            else "MOVE"
            if row["kind"] in {"MOVE", "JUMP_MOVE", "SWIM", "SWIM_UP", "JUMP"}
            else row["kind"]
        )
        families.setdefault(family, {})[key] = row
    summary = {}
    for family, rows in families.items():
        items = {
            r.get("item_id") or r.get("entity_kind")
            for r in rows.values()
            if r.get("item_id") or r.get("entity_kind")
        }
        details = {}
        for r in rows.values():
            compact = {
                k: r[k]
                for k in (
                    "kind",
                    "target_item_id",
                    "target_name",
                    "interaction",
                    "harvest",
                    "possible_drops",
                    "maturity",
                    "rise",
                    "distance_to_focus",
                    "visits",
                    "dependency_product",
                    "native_prerequisite",
                )
                if k in r
            }
            key = json.dumps(compact, sort_keys=True)
            details[key] = compact
        summary[family] = {
            "native_action_count": len(rows),
            "items": sorted(items),
            "details": list(details.values()) if family != "EQUIP" else None,
        }
    phase_state = {
        k: v
        for k, v in state.items()
        if k not in {"routes", "remaining_success_conditions"}
    }
    phase_state.pop("target", None)
    phase_state.pop("focus_items", None)
    phase_state["inventory_uses"] = {
        i: r
        for i, r in state["inventory_uses"].items()
        if i
        in {
            a.get("item_id") for a in actions if a["kind"] in {"SELECT", "EQUIP", "USE"}
        }
    }
    phase_state["item_facts"] = {
        i: {k: r[k] for k in ("name", "edible", "plant", "food_risk") if k in r}
        for i, r in registry.items()
        if r.get("edible") or r.get("plant")
    }
    phase_state["final_result_required"] = outcome["success_checks"]
    phase = await mode.r.controller.judge_typed(
        "voxel_outcome_action_family",
        phase_state,
        {
            "family": {
                "type": "choice",
                "instructions": instructions
                + " Select the kind of action to attempt now from these concrete available effects. A missing productive action can need REPLAN.",
                "criteria": {
                    "REPLAN": "Change strategy or recipe prerequisites",
                    "WAIT": "Transient cooldown",
                    **summary,
                },
            }
        },
        deadline_ms=2300,
    )
    family = phase["family"]["choice"]
    if family in {"REPLAN", "WAIT"}:
        return {
            "action": {"choice": family, "confidence": phase["family"]["confidence"]},
            "family": phase,
        }
    options = families[family]
    state.pop("routes", None)
    state.pop("remaining_success_conditions", None)
    selected_items = {r.get("item_id") for r in options.values()}
    state["inventory_uses"] = {
        i: r for i, r in state["inventory_uses"].items() if i in selected_items
    }
    if family not in {"EQUIP", "CRAFT"}:
        state.pop("craft_requirements", None)
    q = {
        "action": {
            "type": "choice",
            "instructions": instructions,
            "criteria": {
                "REPLAN": "Need a different strategy or missing capability/recipe prerequisite",
                "WAIT": "Briefly wait for a transient native change",
                **options,
            },
        }
    }
    from .judgment import bounded_choice

    a = await bounded_choice(
        mode.r.controller,
        "voxel_outcome_action",
        state,
        instructions,
        q["action"]["criteria"],
        deadline_ms=2600,
    )
    a["family"] = phase
    selected = options.get(a["action"]["choice"])
    if (
        selected
        and selected["kind"] in EFFECTS
        and (not (selected["kind"] == "CRAFT" and selected.get("native_prerequisite")))
    ):
        check = await mode.r.controller.judge_typed(
            "voxel_outcome_action_review",
            {
                "request": task["request"],
                "objective": outcome["objective"],
                "strategy": outcome["strategy"],
                "constraints": task.get("constraints", []),
                "selected": selected,
                "inventory": game["inventory"],
                "item_usage": registry.get(selected.get("item_id"), {}).get("usage"),
                "observed_nodes_now": state["observed_nodes_now"],
                "compatibility": compatibility,
                "recent_actions": list(recent)[-5:],
            },
            {
                "conflict": {
                    "type": "noul",
                    "instructions": "Does this selected effect contradict an explicit user restriction or pursue an unrelated goal? Normal prerequisites chosen in current_strategy are allowed. Small route clearing, equipping tools and crafting required ingredients are routine means, not new user goals.",
                },
                "useful": {
                    "type": "noul",
                    "instructions": "Can this effect make useful progress or necessary preparation toward the user outcome? Independently check item_usage; strategy may be wrong. A shovel creates a grass PATH, not farmland, so using it to supposedly cultivate for seed planting is NO. A hoe cultivates farmland. Seeds on ordinary dirt (soil=1) cannot plant. A harmless alternate cell is fine; repeating ineffective preparations or unrelated effects is not.",
                },
            },
            deadline_ms=1900,
        )
        if check["conflict"]["noul"] >= 0.5 or check["useful"]["noul"] < 0.45:
            alternatives = {
                k: r for k, r in options.items() if k != a["action"]["choice"]
            }
            correction = await bounded_choice(
                mode.r.controller,
                "voxel_outcome_action_correction",
                {
                    "request": task["request"],
                    "held": game["player"].get("wielded"),
                    "inventory": game["inventory"],
                    "rejected_action": selected,
                    "review": check,
                    "item_usage": registry.get(selected.get("item_id"), {}).get(
                        "usage"
                    ),
                },
                "The previous action failed relevance or item compatibility review. Choose a different valid target in this family if available. REPLAN if another action family or missing prerequisites are needed. Never repeat an incompatible action.",
                {"REPLAN": "Reconsider action family/strategy", **alternatives},
                deadline_ms=1800,
            )
            a["action"] = correction["action"]
            a["rejected"] = check
    return a


def remember_effect(task, action, result):
    if result.get("status") != "SUCCEEDED" or action["kind"] not in EFFECTS:
        return
    row = {
        k: v
        for k, v in {**action, **result}.items()
        if k
        in {
            "kind",
            "item_id",
            "target",
            "above",
            "target_id",
            "entity_kind",
            "count",
            "count_before",
            "count_after",
            "defeated",
            "node_before",
            "node_after",
            "above_before",
            "above_after",
            "hunger_before",
            "hunger_after",
            "interaction",
        }
    }
    if action["kind"] == "ATTACK":
        for key in ("weapon_item_id", "weapon_evidence"):
            if key in result:
                row[key] = copy.deepcopy(result[key])
    if action["kind"] == "CRAFT":
        row["recipe_id"] = action.get("recipe_id")
    if row["kind"] == "JUMP_PLACE":
        row["kind"] = "PLACE"
        row["method"] = "jump_underfoot"
    if row["kind"] == "DIG_DOWN":
        row["kind"] = "DIG"
        row["method"] = "descend_own_support"
    task["effects"] = (task.get("effects", []) + [row])[-96:]
    task["effect_version"] = task.get("effect_version", 0) + 1


async def choose_access(mode, task, game, options, target, recent, registry):
    from .locomotion import distance

    pos = game["player"]["position"]
    at = target["position"]
    ready = {
        k: r
        for k, r in options.items()
        if r["kind"] in {"DIG", "USE"} and r.get("target") == at
    }
    if ready:
        a = await mode.r.controller.judge_typed(
            "voxel_target_work_opportunity",
            {
                "current_request": task["request"],
                "target_name": target.get("name"),
                "current_strategy": task["outcome"]["strategy"],
                "approach_results": approach_history(task, game),
                "native_facts": {
                    k: v
                    for k, v in registry.get(target.get("item_id"), {}).items()
                    if k in {"name", "possible_drops", "plant", "description"}
                },
                "ready_work": ready,
            },
            {
                "action": {
                    "type": "choice",
                    "instructions": "Does one of these legal actions productively advance the current strategy and user outcome? DIG with harvest=true can harvest with the current tool, but legal work is not automatically useful. Compare approach_results with the desired gains. SKIP a wrong/unripe resource or an approach the current strategy has abandoned after repeated unproductive attempts; allow another route/source to compete. Do not repeat preparation already satisfied.",
                    "criteria": {
                        "SKIP": "No useful work on the requested resource now",
                        **{
                            k: {
                                f: v
                                for f, v in r.items()
                                if f
                                in {
                                    "kind",
                                    "item_name",
                                    "harvest",
                                    "possible_drops",
                                    "maturity",
                                    "target_item_id",
                                }
                            }
                            for k, r in ready.items()
                        },
                    },
                }
            },
            deadline_ms=1200,
        )
        if a["action"]["choice"] != "SKIP":
            a["tracked_target"] = target
            return a
    if not ready:
        route_options = {}
        from .locomotion import route_actions

        native_actions = [dict(r, action_key=k) for k, r in options.items()]
        for i, route in enumerate(game["navigation"]):
            if route.get("protected"):
                continue
            eligible = route_actions(route, native_actions)
            if not eligible:
                continue
            route_options[str(i)] = {
                "stance": route["target"],
                "distance_to_resource": round(distance(route["target"], at), 2),
                "visited": min(
                    (
                        r.get("visits", 0)
                        for r in eligible
                        if r["kind"] in {"MOVE", "JUMP_MOVE", "SWIM"}
                    ),
                    default=0,
                ),
                "immediately_walkable": any(
                    (r["kind"] in {"MOVE", "JUMP_MOVE", "SWIM"} for r in eligible)
                ),
                "exposed_obstacles": route["blockers"],
                "steps": {
                    r["action_key"]: {
                        k: r[k] for k in ("kind", "item_id", "target", "slot") if k in r
                    }
                    for r in eligible
                },
            }
        for key, row in options.items():
            if row["kind"] not in {"MOVE", "JUMP_MOVE", "SWIM"} or any(
                (r["stance"] == row["target"] for r in route_options.values())
            ):
                continue
            route_options["M" + key] = {
                "stance": row["target"],
                "distance_to_resource": round(distance(row["target"], at), 2),
                "visited": row.get("visits", 0),
                "immediately_walkable": True,
                "exposed_obstacles": [],
                "steps": {key: {k: row[k] for k in ("kind", "target") if k in row}},
            }
        for key, row in options.items():
            if row["kind"] not in {"JUMP_PLACE", "DIG_DOWN"}:
                continue
            stance = {**pos, "y": pos["y"] + (1 if row["kind"] == "JUMP_PLACE" else -1)}
            route_options["V" + key] = {
                "stance": stance,
                "distance_to_resource": round(distance(stance, at), 2),
                "visited": row.get("visits", 0),
                "immediately_walkable": False,
                "exposed_obstacles": [],
                "native_effect": "raise standing support one block"
                if row["kind"] == "JUMP_PLACE"
                else "lower own scaffold one block onto verified support",
                "steps": {
                    key: {k: row[k] for k in ("kind", "target", "item_id") if k in row}
                },
            }
        if route_options:
            routed = await mode.r.controller.judge_typed(
                "voxel_resource_approach",
                {
                    "goal": task["outcome"]["objective"],
                    "target": {
                        k: v
                        for k, v in target.items()
                        if k
                        in {"name", "item_id", "position", "evidence", "historical"}
                    },
                    "player": pos,
                    "current_distance": round(distance(pos, at), 2),
                    "recent_moves": [
                        r.get("target")
                        for r in list(recent)[-5:]
                        if r["kind"] in {"MOVE", "JUMP_MOVE", "SWIM"}
                    ],
                },
                {
                    "route": {
                        "type": "choice",
                        "instructions": "Choose an approach to this useful resource. Prefer a stance reducing distance_to_resource; exposed obstacles can be dug before walking. JUMP_PLACE and DIG_DOWN are actual vertical approaches: they raise/lower support by one using normal blocks and physics. An unwalkable route with a real available DIG/PLACE step can be prepared. Do not repeat visited lateral moves that increase distance. When an elevated target has no useful offered approach, SKIP to equip a scaffold block or change means.",
                        "criteria": {
                            "SKIP": "Need a vertical scaffold/other access action here",
                            **route_options,
                        },
                    }
                },
                deadline_ms=1600,
            )
            route = route_options.get(routed["route"]["choice"])
            if route:
                step = await mode.r.controller.judge_typed(
                    "voxel_resource_route_step",
                    {
                        "goal": task["outcome"]["objective"],
                        "selected_approach": route["stance"],
                        "exposed_obstacles": route["exposed_obstacles"],
                    },
                    {
                        "action": {
                            "type": "choice",
                            "instructions": "Take the next physical step of this chosen approach: walk if offered, otherwise remove an exposed blocker or equip a suitable tool. These steps were natively validated; the retained final goal is still the resource, not this waypoint.",
                            "criteria": route["steps"],
                        }
                    },
                    deadline_ms=1300,
                )
                step["tracked_target"] = target
                return step
    eye_now = game["player"].get("eye_position") or {**pos, "y": pos["y"] + 1.625}
    eye_offset = {k: eye_now[k] - pos[k] for k in ("x", "y", "z")}
    candidates = {}
    for key, row in options.items():
        kind = row["kind"]
        if kind not in {
            "DIG",
            "DIG_DOWN",
            "USE",
            "SELECT",
            "EQUIP",
            "MOVE",
            "JUMP_MOVE",
            "SWIM",
            "JUMP_PLACE",
            "PLACE",
        }:
            continue
        item = row.get("item_id")
        value = {
            k: row[k]
            for k in (
                "kind",
                "item_id",
                "item_name",
                "target",
                "slot",
                "dig_ms",
                "harvest",
                "visits",
                "placeable_support",
            )
            if k in row
        }
        if kind in {"MOVE", "JUMP_MOVE", "SWIM", "JUMP_PLACE", "DIG_DOWN"}:
            stance = (
                {**pos, "y": pos["y"] + (1 if kind == "JUMP_PLACE" else -1)}
                if kind in {"JUMP_PLACE", "DIG_DOWN"}
                else row["target"]
            )
            eye = {k: stance[k] + eye_offset[k] for k in ("x", "y", "z")}
            value.update(
                eye_distance_after=round(distance(eye, at), 2), height_after=stance["y"]
            )
        if kind == "DIG":
            value["selected_resource"] = row.get("target") == at
        if kind in {"SELECT", "EQUIP"}:
            value["placeable_support"] = row.get("placeable_support", False)
            value["tool"] = row.get("tool")
        candidates[key] = value
    if not candidates:
        return None
    state = {
        "request": task["request"],
        "strategy": task["outcome"]["strategy"],
        "constraints": task.get("constraints", []),
        "target": target,
        "target_facts": {
            k: v
            for k, v in registry.get(target.get("item_id"), {}).items()
            if k in {"name", "edible", "possible_drops", "plant", "usage"}
        },
        "player": pos,
        "held": game["player"].get("wielded"),
        "eye_distance_now": round(distance(eye_now, at), 2),
        "recent_actions": [
            {
                k: v
                for k, v in r.items()
                if k in {"kind", "item_id", "target", "status", "reason"}
            }
            for r in list(recent)[-4:]
        ],
        "access_attempts": task.get("access_progress"),
    }
    from .judgment import bounded_choice

    if task.get("stalled_s", 0) >= 12 or task.get("local_loop"):
        from .judgment import choose

        review = await choose(
            mode.reasoning,
            "Choose ONE concrete next action that improves access or operates on this selected useful resource. A blocked approach needs DIG of its exposed obstruction, not circling on existing nearby squares. Use DIG_DOWN to descend our own scaffold when the target/drop is below. Stop scaffolding when work is already in reach. Never repeat the recent unproductive approach. Return SKIP only for genuinely missing new prerequisites.",
            state,
            {"SKIP": "Another prerequisite/target is needed", **candidates},
        )
        return (
            {
                "action": {"choice": review["choice"], "confidence": 1},
                "tracked_target": target,
                "recovery": review,
            }
            if review["choice"] != "SKIP"
            else None
        )
    answer = await bounded_choice(
        mode.r.controller,
        "voxel_target_access",
        state,
        "Reach a useful work stance for the selected resource. Minimize the necessary access work while preserving a way back: approach on existing support before erecting a tall isolated pillar. An elevated target can require SELECT/EQUIP a placeable_support block then JUMP_PLACE, which raises standing height by one. Plain JUMP returns to the same height. Stop rising when the resource is in native reach; standing level with its block is unnecessary. Clear only relevant exposed obstacles. Compare eye_distance_after, current distance and visits; avoid moves/rises that worsen access. SKIP only if another prerequisite/target is needed.",
        {
            "SKIP": "Use the general outcome planner for other prerequisites or a different target",
            **candidates,
        },
        deadline_ms=2000,
    )
    if answer["action"]["choice"] == "SKIP" and answer["action"]["confidence"] < 0.65:
        from .judgment import choose

        review = await choose(
            mode.reasoning,
            "Choose the next one bounded action to gain access to or operate on this useful resource. A carried ordinary block enables JUMP_PLACE after SELECT/EQUIP; that is a real available capability, so do not keep jumping in place or circling. Use actual native reach and recent failures. SKIP only if another prerequisite or target is needed.",
            state,
            {"SKIP": "Another target/prerequisite is needed", **candidates},
        )
        answer["action"] = {"choice": review["choice"], "confidence": 1}
        answer["review"] = review
    if answer["action"]["choice"] == "SKIP":
        return None
    answer["tracked_target"] = target
    return answer
