import math


def key(p):
    return tuple((p[k] for k in ("x", "y", "z")))


def regions(plan, snapshot, previous=None):
    view = snapshot["view"]
    survey = view.get("construction", {}).get("survey", [])
    known = {key(r["position"]): r for r in survey}
    visible = view.get("survival", {}).get("nodes", [])
    p = view["self"]["position"]
    w, d, h = plan.size
    anchors = set()
    for x, y, z in known:
        for dx, dz in ((0, 0), (w - 1, 0), (0, d - 1), (w - 1, d - 1)):
            if math.hypot(x - dx + w / 2 - p["x"], z - dz + d / 2 - p["z"]) <= 8:
                anchors.add((x - dx, y + 1, z - dz))
    result = []
    for x, y, z in anchors:
        support = sum(
            ((x + dx, y - 1, z + dz) in known for dx in range(w) for dz in range(d))
        )
        clear = sum(
            (
                known.get((x + dx, y - 1, z + dz), {}).get("clearance", 0) >= min(h, 4)
                for dx in range(w)
                for dz in range(d)
            )
        )
        blockers = {
            key(r["position"]): r
            for r in visible
            if x <= r["position"]["x"] < x + w
            and z <= r["position"]["z"] < z + d
            and (y <= r["position"]["y"] < y + h)
        }
        for r in survey:
            b = r.get("obstacle")
            if (
                b
                and x <= b["position"]["x"] < x + w
                and (z <= b["position"]["z"] < z + d)
                and (y <= b["position"]["y"] < y + h)
            ):
                blockers[key(b["position"])] = b
        if not support:
            continue
        result.append(
            {
                "origin": {"x": int(x), "y": int(y), "z": int(z)},
                "width": w,
                "depth": d,
                "height": h,
                "supported_columns": support,
                "clear_columns": clear,
                "total_columns": w * d,
                "unknown_columns": w * d - support,
                "blockers": list(blockers.values())[:24],
                "distance": round(
                    math.hypot(x + w / 2 - p["x"], z + d / 2 - p["z"]), 2
                ),
            }
        )
    for r in result:
        o = r["origin"]
        w = r["width"]
        d = r["depth"]
        y = o["y"] - 1
        rim = (
            {(o["x"] - 1, o["z"] + dz) for dz in range(-1, d + 1)}
            | {(o["x"] + w, o["z"] + dz) for dz in range(-1, d + 1)}
            | {(o["x"] + dx, o["z"] - 1) for dx in range(w)}
            | {(o["x"] + dx, o["z"] + d) for dx in range(w)}
        )
        r["observed_working_space"] = sum(
            (known.get((x, y, z), {}).get("clearance", 0) >= 2 for x, z in rim)
        )
        r["working_space_size"] = len(rim)
    result.sort(
        key=lambda r: (
            r["unknown_columns"],
            -r["observed_working_space"],
            r["total_columns"] - r["clear_columns"],
            r["distance"],
        )
    )
    retained = next(
        (r for r in result if previous and r["origin"] == previous["origin"]), None
    )
    return ([retained] if retained else []) + [r for r in result if r is not retained][
        : 7 if retained else 8
    ]


def contains(region, p):
    o = region["origin"]
    return (
        o["x"] <= p["x"] < o["x"] + region["width"]
        and o["z"] <= p["z"] < o["z"] + region["depth"]
        and (o["y"] <= p["y"] < o["y"] + region["height"])
    )


def working_region(region):
    o = region["origin"]
    return {
        "origin": {"x": o["x"] - 1, "y": o["y"], "z": o["z"] - 1},
        "width": region["width"] + 2,
        "depth": region["depth"] + 2,
        "height": region["height"],
    }


def ready(region, snapshot):
    o = region["origin"]
    known = {
        key(r["position"]): r.get("clearance", 0)
        for r in snapshot["view"].get("construction", {}).get("terrain", [])
    }
    for x in range(o["x"] - 1, o["x"] + region["width"] + 1):
        for z in range(o["z"] - 1, o["z"] + region["depth"] + 1):
            interior = (
                o["x"] <= x < o["x"] + region["width"]
                and o["z"] <= z < o["z"] + region["depth"]
            )
            if known.get((x, o["y"] - 1, z), 0) < (
                min(4, region["height"]) if interior else 2
            ):
                return False
    return True


async def strategy(mode, snapshot, project, plan, previous=None):
    candidates = regions(plan, snapshot, previous)
    options = {
        str(i): {k: v for k, v in r.items() if k != "blockers"}
        for i, r in enumerate(candidates)
    }
    for i, r in enumerate(candidates):
        options[str(i)]["obstacle_count"] = len(r["blockers"])
    state = {
        "requests": [r["text"] for r in project.requests[-6:]],
        "design_size": plan.size,
        "player": snapshot["view"]["self"]["position"],
        "last_attempt": {k: v for k, v in previous.items() if k != "blockers"}
        if previous
        else None,
        "known_landmarks": mode.memory.context(),
    }
    answer = await mode.r.controller.judge_typed(
        "voxel_site_strategy",
        state,
        {
            "site": {
                "type": "choice",
                "instructions": "Select a provisional site for this requested structure. Retain last_attempt when it is still workable: do not move the footprint after each observation. Prefer observed support and working space around it. Avoid cliff edges and tree canopies if ordinary ground is available. Local clearing is routine preparation. Unknown columns require observation. SEARCH if none is promising, ASK only for conflicting human requirements. Respect exclusions and human-owned buildings.",
                "criteria": {
                    "SEARCH": "Explore for a better site while retaining the design",
                    "ASK": "A human-only preference conflict must be resolved",
                    **options,
                },
            },
            "prepare": {
                "type": "choice",
                "instructions": "Resolve the allowed preparation for the current building request. Generic help me build, choose/design it yourself permits looking for nearby land and locally clearing/levelling ordinary terrain. Explicit no digging / do not move / only use inventory constrains those means, not the entire building objective.",
                "criteria": {
                    "CLEAR": "May find and clear ordinary local terrain",
                    "SEARCH_ONLY": "May find a clear site but must not alter terrain",
                    "EXACT": "Must remain at an explicitly specified site",
                },
            },
        },
        deadline_ms=2500,
    )
    index = answer["site"]["choice"]
    choice = (
        candidates[int(index)]
        if index.isdigit() and int(index) < len(candidates)
        else None
    )
    if index == "ASK" or answer["prepare"]["confidence"] < 0.7:
        from .judgment import choose

        checked = await choose(
            mode.reasoning,
            "Choose the next preparatory site or search. Unknown support is not usable until observed. Do not give up merely because local terrain needs work.",
            state,
            {
                "SEARCH": "Explore to find/observe a promising site",
                "ASK": "Explicit incompatible requirements need user input",
                **options,
            },
        )
        index = checked["choice"]
        choice = (
            candidates[int(index)]
            if index.isdigit() and int(index) < len(candidates)
            else None
        )
        answer["review"] = checked
    return {
        "region": choice,
        "method": index if not choice else "PREPARE",
        "alter": answer["prepare"]["choice"] == "CLEAR",
        "judgment": answer,
    }


async def choose_step(mode, snapshot, task, actions, recent, visited):
    from .locomotion import cell, distance

    game = snapshot["view"]
    region = task.get("site_region")
    pos = game["self"]["position"]
    options = {}
    protected = {key(r["position"]) for r in task.get("protected_build_cells", [])}
    target = region and region["origin"]
    obstacles = []
    unknown = []
    if region:
        area = working_region(region)
        obstacles = [
            r["position"]
            for r in game.get("survival", {}).get("nodes", [])
            if contains(area, r["position"]) and key(r["position"]) not in protected
        ]
        known = {
            key(r["position"]): r
            for r in game.get("construction", {}).get("survey", [])
        }
        o = area["origin"]
        unknown = [
            {"x": x, "y": o["y"] - 1, "z": z}
            for x in range(o["x"], o["x"] + area["width"])
            for z in range(o["z"], o["z"] + area["depth"])
            if (x, o["y"] - 1, z) not in known
        ]
    for i, a in enumerate(actions):
        kind = a["kind"]
        p = a.get("target")
        keep = False
        if kind in {"MOVE", "JUMP_MOVE", "LOOK"}:
            keep = True
        elif kind == "DIG":
            keep = bool(
                region
                and task.get("site_alter")
                and contains(working_region(region), p)
                and (key(p) not in protected)
            )
        elif kind == "PLACE":
            keep = bool(a.get("site_support") and task.get("site_alter"))
        elif kind == "SELECT":
            keep = bool(region and task.get("site_alter"))
        if not keep:
            continue
        row = {k: v for k, v in a.items() if k not in {"action_id", "target_index"}}
        if p:
            row["visits"] = visited.get(cell(p), 0)
            if target:
                row["distance_to_site"] = round(
                    distance(
                        p,
                        {
                            "x": target["x"] + (region["width"] - 1) / 2,
                            "y": target["y"],
                            "z": target["z"] + (region["depth"] - 1) / 2,
                        },
                    ),
                    2,
                )
            if obstacles:
                row["distance_to_obstruction"] = round(
                    min((distance(p, b) for b in obstacles)), 2
                )
            if unknown:
                row["distance_to_unobserved_column"] = round(
                    min((distance(p, b) for b in unknown)), 2
                )
        if kind == "DIG":
            row["purpose"] = (
                "Expose the selected building footprint; actual support is reobserved before building"
            )
        options[str(i)] = row
    compact_region = (
        {k: v for k, v in region.items() if k != "blockers"} if region else None
    )
    state = {
        "user_goal": task["request"],
        "preparing_region": compact_region,
        "may_alter_terrain": task.get("site_alter"),
        "repair_existing_building": task.get("site_repair", False),
        "do_not_change_site": task.get("site_repair", False),
        "position": pos,
        "inventory": game["self"].get("inventory_summary"),
        "recent_actions": list(recent)[-5:],
        "stalled_seconds": task.get("stalled_s", 0),
        "visible_route_options": game.get("survival", {}).get("navigation", [])[:6],
    }
    answer = await mode.r.controller.judge_typed(
        "voxel_site_step",
        state,
        {
            "action": {
                "type": "choice",
                "instructions": "Advance site preparation for the retained house design. Prioritize an offered DIG obstruction or PLACE support that makes an actual change. When no effect is reachable, approach the remaining obstruction or unobserved column using distances; circling the site center does not prepare it. Keep the same footprint until disproven. Never mine standing support. If no region, explore unvisited supported ground. REVISE if this site is disproven or all approaches loop; WAIT only for a transient observation.",
                "criteria": {
                    "WAIT": "Wait briefly",
                    "REVISE": "Find another candidate site",
                    **options,
                },
            }
        },
        deadline_ms=2200,
    )
    return answer
