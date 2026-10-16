import math

MOTIONS = {"MOVE", "JUMP_MOVE", "SWIM"}
NAV_GOALS = {
    "EXPLORE",
    "ASCEND",
    "FORWARD",
    "FIND_WOOD",
    "FIND_STONE",
    "MATERIALS",
    "RETURN_SITE",
}


def cell(p):
    return ",".join((str(math.floor(p[k] + 0.5)) for k in ("x", "y", "z")))


def distance(a, b):
    return math.sqrt(sum(((a[k] - b[k]) ** 2 for k in ("x", "y", "z"))))


def observe(task, game, now):
    p = game["player"].get("position")
    if not p:
        return
    progress = task.setdefault(
        "progress",
        {"cells": [], "distance": 0.0, "max_radius": 0.0, "last_effect_at": now},
    )
    if game["grounded"] or game["in_water"]:
        k = cell(p)
        if k not in progress["cells"]:
            progress["cells"] = (progress["cells"] + [k])[-512:]
            progress["last_effect_at"] = now
        progress["max_radius"] = max(
            progress["max_radius"],
            math.hypot(
                p["x"] - task["start_position"]["x"],
                p["z"] - task["start_position"]["z"],
            ),
        )
        progress["highest_ground"] = max(progress.get("highest_ground", p["y"]), p["y"])
    before = progress.get("position")
    if before and distance(before, p) > 0.15:
        progress["distance"] += distance(before, p)
    progress["position"] = dict(p)
    task["stalled_s"] = round(max(0, now - progress["last_effect_at"]), 1)
    nav = task.setdefault("navigation", {})
    peak = task.setdefault("navigation_peak", {"y": p["y"], "higher_seen": p["y"]})
    if game["grounded"]:
        peak["y"] = max(peak["y"], p["y"])
    peak["higher_seen"] = max(
        peak["higher_seen"],
        max(
            (
                r["target"]["y"]
                for r in game.get("navigation", [])
                if r.get("supported")
            ),
            default=p["y"],
        ),
    )
    waypoint = nav.get("waypoint")
    if waypoint and distance(p, waypoint["target"]) < 0.65 and game["grounded"]:
        nav.pop("waypoint", None)
        nav["arrivals"] = nav.get("arrivals", 0) + 1


def effect(task, action, result, now):
    progress = task.setdefault(
        "progress",
        {"cells": [], "distance": 0.0, "max_radius": 0.0, "last_effect_at": now},
    )
    if result.get("status") != "SUCCEEDED":
        return
    if action["kind"] in {
        "DIG",
        "DIG_DOWN",
        "PLACE",
        "JUMP_PLACE",
        "CRAFT",
        "ATTACK",
        "USE",
    }:
        progress["last_effect_at"] = now
        task["stalled_s"] = 0
        task["local_loop"] = False
        task.setdefault("navigation", {}).pop("failed_choices", None)
    if (
        action["kind"] in MOTIONS
        and result.get("position")
        and result.get("position_before")
    ):
        if distance(result["position"], result["position_before"]) > 0.25:
            progress["last_motion_at"] = now


def filter_actions(actions, task, position, viewed, recent):
    result = []
    for action in actions:
        kind = action["kind"]
        if kind == "LOOK":
            sector = round(action.get("yaw", 0) % (2 * math.pi) / (math.pi / 2)) % 4
            if sector in viewed:
                continue
        if kind == "JUMP" and task.get("agent_goal") in NAV_GOALS:
            continue
        if (
            kind == "JUMP"
            and task.get("outcome")
            and any(
                (
                    r.get("kind") == "JUMP"
                    and r.get("status") == "SUCCEEDED"
                    and r.get("position_after")
                    and r.get("position_before")
                    and (distance(r["position_after"], r["position_before"]) < 0.2)
                    for r in list(recent)[-3:]
                )
            )
        ):
            continue
        same_failures = sum(
            (
                r.get("status") != "SUCCEEDED"
                and r.get("kind") == kind
                and (r.get("target") == action.get("target"))
                for r in list(recent)[-4:]
            )
        )
        if same_failures >= 2:
            continue
        result.append(action)
    return result


async def choose_route(controller, game, task, actions, visited, targets, memory):
    goal = task.get("agent_goal")
    pos = game["player"]["position"]
    nav = task.setdefault("navigation", {})
    if goal not in NAV_GOALS:
        return None
    routes = game.get("navigation", [])
    options = {}
    bearing = nav.get("bearing", task.get("start_yaw", 0))
    wanted = [r["position"] for r in game["nodes"] if r.get("item_id") in targets]
    wanted += task.get("collect_positions", [])
    if not wanted:
        wanted = [r["position"] for r in memory if r.get("item_id") in targets][-12:]
    highs = [
        r["position"]
        for r in task.get("observed_terrain", [])
        if r["position"]["y"] + 0.5 > pos["y"] + 0.4
    ]
    for i, r in enumerate(routes):
        if r.get("protected"):
            continue
        target = r["target"]
        blockers = r.get("blockers", [])
        matching = [
            a
            for a in actions
            if a["kind"] in MOTIONS and distance(a["target"], target) < 0.4
        ]
        digs = [
            a
            for a in actions
            if a["kind"] == "DIG"
            and any((a["target"] == b["position"] for b in blockers))
        ]
        places = [
            a
            for a in actions
            if a["kind"] == "PLACE" and a.get("target") == r.get("place_step")
        ]
        if not matching and (not digs) and (not places):
            continue
        row = {
            **r,
            "visits": visited.get(cell(target), 0),
            "available_move": bool(matching),
            "available_digs": len(digs),
            "forward_progress": round(
                -(target["x"] - pos["x"]) * math.sin(bearing)
                + (target["z"] - pos["z"]) * math.cos(bearing),
                2,
            ),
            "height_gain": round(target["y"] - pos["y"], 2),
            "distance_from_start": round(distance(target, task["start_position"]), 2),
        }
        if wanted:
            row["distance_to_resource"] = round(
                min((distance(target, p) for p in wanted)), 2
            )
        if task.get("return_site"):
            row["distance_to_site"] = round(distance(target, task["return_site"]), 2)
        if goal == "ASCEND" and highs:
            row["distance_to_observed_higher_ground"] = round(
                min((distance(target, p) for p in highs)), 2
            )
        options[str(i)] = row
    for i, a in enumerate(actions):
        if a["kind"] not in MOTIONS or any(
            (distance(a["target"], r["target"]) < 0.4 for r in options.values())
        ):
            continue
        target = a["target"]
        row = {
            "id": "move-" + str(i),
            "target": target,
            "blockers": [],
            "supported": a.get("reaches_solid_land", False),
            "rise": a.get("rise", 0),
            "available_move": True,
            "available_digs": 0,
            "visits": visited.get(cell(target), 0),
            "forward_progress": round(
                -(target["x"] - pos["x"]) * math.sin(bearing)
                + (target["z"] - pos["z"]) * math.cos(bearing),
                2,
            ),
            "height_gain": round(target["y"] - pos["y"], 2),
            "distance_from_start": round(distance(target, task["start_position"]), 2),
        }
        if wanted:
            row["distance_to_resource"] = round(
                min((distance(target, p) for p in wanted)), 2
            )
        if task.get("return_site"):
            row["distance_to_site"] = round(distance(target, task["return_site"]), 2)
        if goal == "ASCEND" and highs:
            row["distance_to_observed_higher_ground"] = round(
                min((distance(target, p) for p in highs)), 2
            )
        options["M" + str(i)] = row
    waypoint = nav.get("waypoint")
    if waypoint:
        chosen = next(
            (r for r in options.values() if r["target"] == waypoint["target"]), None
        )
        if chosen and task.get("stalled_s", 0) < 12:
            return chosen
        nav.pop("waypoint", None)
    if not options:
        return None
    instruction = (
        "This is a tracked user-selected target: minimize distance_to_site using the latest observed target position. Ignore distance_from_start for this pursuit. "
        if task.get("outcome") and task.get("return_site")
        else "Choose one adjacent route that makes progress out of this confined area toward the user goal. A route with available_digs opens NEW space and is preferable to repeatedly walking around nearby visited cells. Keep direction when useful and minimize repeated visits. Unknown support does not prohibit digging exposed headroom, but movement needs later observed support. A higher ledge with available_digs is possible. Do not circle because existing flat moves are easier. "
        if task.get("outcome")
        else ""
    )
    answer = await controller.judge_typed(
        "voxel_navigation_route",
        {
            "request": task["request"],
            "goal": goal,
            "player_position": pos,
            "last_routes": nav.get("recent_routes", [])[-6:],
            "stalled_seconds": task.get("stalled_s", 0),
            "constraints": task.get("constraints", []),
            "resources_known": wanted[:6],
            "visible_higher_ground": highs[:8],
            "current_plan_step": task.get("stage_goal"),
        },
        {
            "route": {
                "type": "choice",
                "instructions": instruction
                + "Choose a useful local waypoint. Keep the user goal. ASCEND means reach the hilltop: prefer a higher supported ledge, even if exposed headroom needs digging. EXPLORE means sustained NEW ground: prefer fewer visits then forward_progress, not distance_from_start. When a direction ends, turn toward unvisited interior ground; following a map edge that exposes no new terrain is not useful. A blocked route with available_digs can be opened before walking; low headroom may need multiple digs. For resources, prefer REDUCING distance_to_resource, including useful clearing. Distant remembered resources require several consecutive moves; do not abandon them because near steps were visited. RETURN_SITE minimizes distance_to_site. Do not bounce or dig random floors. NONE only if every option is unhelpful.",
                "criteria": {"NONE": "No goal-serving offered route", **options},
            }
        },
        deadline_ms=2200,
    )
    chosen = options.get(answer["route"]["choice"])
    if chosen:
        nav["waypoint"] = {"target": chosen["target"], "goal": goal}
        nav["recent_routes"] = (nav.get("recent_routes", []) + [chosen["target"]])[-8:]
        dx = chosen["target"]["x"] - pos["x"]
        dz = chosen["target"]["z"] - pos["z"]
        if math.hypot(dx, dz) > 0.4:
            nav["bearing"] = math.atan2(-dx, dz)
    return chosen


def route_actions(route, actions):
    if not route:
        return []
    blockers = route.get("blockers", [])
    target = route["target"]
    return [
        a
        for a in actions
        if a["kind"] in MOTIONS
        and distance(a["target"], target) < 0.4
        or (
            a["kind"] == "DIG" and any((a["target"] == b["position"] for b in blockers))
        )
        or (a["kind"] == "PLACE" and a.get("target") == route.get("place_step"))
        or (
            a["kind"] in {"SELECT", "EQUIP"}
            and (
                bool(blockers)
                and a.get("tool")
                or (bool(route.get("place_step")) and (not a.get("tool")))
            )
        )
    ]
