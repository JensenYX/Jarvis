import copy
from .locomotion import distance, cell, route_actions


def positional(outcome):
    checks = outcome.get("success_checks") or []
    return bool(checks) and all(
        (set(c["evidence"]) <= {"position", "observation"} for c in checks)
    )


def geometry(snapshot, task=None):
    from .survival_intent import context

    game = context(snapshot)
    surfaces = snapshot["view"].get("construction", {}).get("terrain", [])
    above = sorted(game["nodes"], key=lambda r: r["position"]["y"], reverse=True)[:8]
    known = {cell(r["position"]): r for r in (task or {}).get("spatial_survey", [])}
    for r in surfaces:
        known[cell(r["position"])] = r
    levels = {}
    for r in known.values():
        p = r["position"]
        levels.setdefault(p["y"], []).append(p)
    overview = [
        {
            "block_center_y": h,
            "standing_feet_y": h + 0.5,
            "observed_surface_cells": len(ps),
            "x_range": [min((p["x"] for p in ps)), max((p["x"] for p in ps))],
            "z_range": [min((p["z"] for p in ps)), max((p["z"] for p in ps))],
        }
        for h, ps in sorted(levels.items())
    ]
    return {
        "position": game["player"]["position"],
        "grounded": game["grounded"],
        "dry_land": game["dry_land"],
        "adjacent_routes": [
            {
                "target": r["target"],
                "supported": r.get("supported"),
                "blockers": len(r.get("blockers", [])),
            }
            for r in game["navigation"]
        ],
        "highest_observed_standing_surfaces": sorted(
            surfaces, key=lambda r: r["position"]["y"], reverse=True
        )[:8],
        "highest_exposed_surfaces": [
            {k: r[k] for k in ("position", "item_id") if k in r} for r in above
        ],
        "surface_height_range": [
            min((r["position"]["y"] for r in surfaces), default=None),
            max((r["position"]["y"] for r in surfaces), default=None),
        ],
        "lower_surface_examples": sorted(surfaces, key=lambda r: r["position"]["y"])[
            :5
        ],
        "observed_approach_surfaces": sorted(
            (task or {}).get("spatial_survey", []), key=lambda r: r["position"]["y"]
        )[:8],
        "surveyed_surface_levels": overview,
        "ascent_history": (task or {}).get("spatial_positions", [])[:12],
        "confirmed_grounded_route": (task or {}).get("spatial_positions", [])[-12:],
        "coordinate_meaning": "Player feet stand 0.5 above a full supporting block center. Higher block coordinates need not mean higher feet. Compare ground heights consistently.",
        "visibility_limit": "Local observed surfaces only; absence of a route is not proof of reaching a distant destination or summit.",
    }


def observe(task, snapshot):
    rows = {cell(r["position"]): r for r in task.get("spatial_survey", [])}
    for r in snapshot["view"].get("construction", {}).get("terrain", []):
        rows[cell(r["position"])] = r
    task["spatial_survey"] = list(rows.values())[-160:]
    game = snapshot["view"].get("survival", {})
    pos = snapshot["view"]["self"]["position"]
    positions = task.setdefault("spatial_positions", [])
    if game.get("grounded") and (not positions or distance(positions[-1], pos) > 0.65):
        positions.append(dict(pos))
    task["spatial_positions"] = positions[-32:]


async def choose(mode, task, snapshot, actions, recent, visited):
    from .judgment import bounded_choice, choose as review
    from .survival_intent import context

    game = context(snapshot)
    pos = game["player"]["position"]
    routes = {}
    nav = copy.deepcopy(task.get("navigation", {}))
    for i, r in enumerate(game["navigation"]):
        if r.get("protected"):
            continue
        candidates = route_actions(r, actions)
        if not candidates:
            continue
        floor = {**r["target"], "y": r["target"]["y"] - 0.5}
        steps = {
            str(j): {k: v for k, v in a.items() if k != "action_id"}
            for j, a in enumerate(actions)
            if a in candidates
            and (not (a["kind"] == "DIG" and a.get("target") == floor))
        }
        if not steps:
            continue
        routes[str(i)] = {
            **r,
            "steps": steps,
            "visits": visited.get(cell(r["target"]), 0),
            "height_change": round(r["target"]["y"] - pos["y"], 2),
            "support_placement_available": any(
                (a["kind"] == "PLACE" for a in candidates)
            ),
            "needs_unavailable_support_material": bool(r.get("place_step"))
            and (
                not any(
                    (
                        a["kind"] == "PLACE"
                        or (
                            a["kind"] in {"SELECT", "EQUIP"}
                            and a.get("placeable_support")
                        )
                        for a in candidates
                    )
                )
            ),
        }
    for i, a in enumerate(actions):
        if a["kind"] in {"MOVE", "JUMP_MOVE", "SWIM"} and (
            not any((distance(a["target"], r["target"]) < 0.4 for r in routes.values()))
        ):
            routes["M" + str(i)] = {
                "target": a["target"],
                "height_change": round(a["target"]["y"] - pos["y"], 2),
                "visits": visited.get(cell(pos), 0),
                "steps": {str(i): {k: v for k, v in a.items() if k != "action_id"}},
                "blockers": [],
            }
    if not routes:
        stranded = nav.setdefault("stranded_positions", [])
        if cell(pos) not in stranded:
            stranded.append(cell(pos))
        nav["stranded_positions"] = stranded[-16:]
    specials = {
        str(i): a
        for i, a in enumerate(actions)
        if a["kind"]
        in {"LOOK", "JUMP_PLACE", "DIG_DOWN", "SWIM_UP", "SELECT", "EQUIP", "CRAFT"}
        or (a["kind"] == "DIG" and a.get("harvest") is True)
    }
    for key, a in list(specials.items()):
        if a["kind"] == "DIG_DOWN":
            specials[key] = {
                **a,
                "meaning": "Remove one own scaffold block and descend onto its verified support; recovers the block and may unlock travel.",
            }
        if a["kind"] in {"DIG_DOWN", "JUMP_PLACE"}:
            landing = {**pos, "y": pos["y"] + (-1 if a["kind"] == "DIG_DOWN" else 1)}
            specials[key] = {
                **specials[key],
                "standing_after": landing,
                "returns_to_known_stranded_position": cell(landing)
                in nav.get("stranded_positions", []),
            }
    terrain = snapshot["view"].get("construction", {}).get("terrain", [])
    higher = [
        r["position"] for r in terrain if r["position"]["y"] + 0.5 > pos["y"] + 0.4
    ]
    for r in routes.values():
        if higher:
            r["distance_to_observed_higher_ground"] = round(
                min((distance(r["target"], p) for p in higher)), 2
            )
    state = {
        "request": task["request"],
        "strategy": task["outcome"]["strategy"],
        "constraints": task.get("constraints", []),
        "position": pos,
        "start_position": task["start_position"],
        "inventory": game["inventory"],
        "held": game["player"].get("wielded"),
        "observed_higher_ground": higher[:12],
        "exposed_higher_terrain": [
            {k: r[k] for k in ("position", "item_id") if k in r}
            for r in sorted(
                game["nodes"], key=lambda r: r["position"]["y"], reverse=True
            )[:8]
        ],
        "observed_surfaces": sorted(
            terrain, key=lambda r: r["position"]["y"], reverse=True
        )[:12],
        "recent_attempts": list(recent)[-5:],
        "progress": {k: v for k, v in task.get("progress", {}).items() if k != "cells"},
        "last_routes": nav.get("recent_routes", [])[-6:],
        "positions_observed_with_no_routes": nav.get("stranded_positions", []),
    }
    waypoint = nav.get("waypoint")
    chosen = None
    if (
        waypoint
        and distance(pos, waypoint["target"]) >= 0.65
        and (task.get("stalled_s", 0) < 12)
    ):
        chosen = next(
            (r for r in routes.values() if r["target"] == waypoint["target"]), None
        )
    if not chosen and routes:
        options = {
            k: {f: v for f, v in r.items() if f != "steps"}
            | {"available_steps": list(r["steps"].values())}
            for k, r in routes.items()
        }
        options["OTHER"] = {
            "description": "Need another prerequisite, observation or vertical support",
            "actions": list(specials.values()),
        }
        instruction = "Choose a local route that advances the actual user destination. For climbing, approach observed higher ground or expose a step through the nearby higher terrain. Preserve its floor, clear body/head blockers, then MOVE/JUMP_MOVE onto it. A route with needs_unavailable_support_material cannot be completed just by digging its ceiling; prefer a natural ledge or a new flat approach to the slope. Distinguish unavailable material from unknown_support: unknown support may become observable after clearing headroom. Prefer new useful ground over repeating the last two positions; a temporary descent can make progress. To explore, continue to new ground. Do not harvest stone merely because it is focused. OTHER only for a necessary prerequisite when routes cannot help. Digging harvest=false yields no material. Several useful alternatives are acceptable."
        a = await bounded_choice(
            mode.r.controller,
            "voxel_spatial_route",
            state,
            instruction,
            options,
            deadline_ms=2200,
        )
        if a["action"]["choice"] == "OTHER" or task.get("stalled_s", 0) >= 12:
            try:
                checked = await review(
                    mode.reasoning,
                    instruction
                    + " Select a concrete useful route now if possible; the user has delegated movement, not another speech plan.",
                    state,
                    options,
                )
                a["action"] = {"choice": checked["choice"], "confidence": 1}
            except TimeoutError:
                a = await bounded_choice(
                    mode.r.controller,
                    "voxel_spatial_route_review",
                    state,
                    instruction,
                    options,
                    deadline_ms=2000,
                )
        chosen = routes.get(a["action"]["choice"])
        if chosen:
            nav["waypoint"] = {"target": chosen["target"]}
            nav["recent_routes"] = (nav.get("recent_routes", []) + [chosen["target"]])[
                -8:
            ]
    if chosen:
        steps = chosen["steps"]
        state = {
            "request": task["request"],
            "selected_route": chosen,
            "held": game["player"].get("wielded"),
            "inventory": game["inventory"],
            "recent_attempts": list(recent)[-3:],
        }
        a = await bounded_choice(
            mode.r.controller,
            "voxel_spatial_step",
            state,
            "Execute this one selected route. If a MOVE/JUMP_MOVE is available take it before further excavation. Otherwise choose the relevant exposed blocker or equip the needed tool. Keep the supporting step intact; harvest=false may clear access but yields NO building material. Do not require crafting to clear one exposed blocker if the goal can be reached by existing steps.",
            {"WAIT": "No useful route step now", **steps},
            deadline_ms=1800,
        )
        a["navigation"] = nav
        return a
    if not specials:
        return {"action": {"choice": "REPLAN", "confidence": 1}}
    a = await bounded_choice(
        mode.r.controller,
        "voxel_spatial_prerequisite",
        state,
        "Choose a prerequisite serving the actual destination, or REPLAN when none fits. A temporary descent may be needed to leave an isolated scaffold before climbing the hillside. DIG_DOWN is offered only for an own scaffold over known solid support: descend one block and recover it when stuck with no horizontal route. Do not reject it simply because the final destination is higher. LOOK can expose another route. JUMP_PLACE physically raises support using one held block; it is a means of reaching terrain, not a summit. Plain jumping in place is omitted because it does not change the standing location. SELECT/EQUIP enables use of a carried tool/support. Do not make unrelated crafting products.",
        {"REPLAN": "Need a different plan based on missing prerequisites", **specials},
        deadline_ms=1800,
    )
    if a["action"]["confidence"] < 0.65 or a["action"]["choice"] == "REPLAN":
        question = "Choose one offered native prerequisite that changes this stalled situation. When no routes exist on an isolated own scaffold, DIG_DOWN onto known solid support can recover blocks and let us approach from below; it may require multiple descents. Do not JUMP_PLACE back to a returns_to_known_stranded_position after descending: that recreates the same dead end. A nearby harvestable resource can supply missing support. The final goal is unchanged; temporary descent is allowed. Repeating LOOK after inspected views does not open a route. REPLAN only if no offered action helps; never invent inventory or coordinates."
        try:
            checked = await review(
                mode.reasoning,
                question,
                state,
                {"REPLAN": "No useful offered prerequisite", **specials},
            )
            a["action"] = {"choice": checked["choice"], "confidence": 1}
        except TimeoutError:
            a = await bounded_choice(
                mode.r.controller,
                "voxel_spatial_prerequisite_review",
                state,
                question,
                {"REPLAN": "No useful offered prerequisite", **specials},
                deadline_ms=2000,
            )
    a["navigation"] = nav
    return a
