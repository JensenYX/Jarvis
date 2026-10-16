from collections import deque


def exterior_distances(plan, completed, remaining, actions, unproductive=()):
    origin = plan.origin
    ox, oz = (origin["x"], origin["z"])
    w, d, _ = plan.size
    blocked = {
        (r["target"]["x"], r["target"]["z"])
        for r in completed
        if r.get("target")
        and (not r.get("removed"))
        and (origin["y"] <= r["target"]["y"] <= origin["y"] + 1)
    }
    stations = set()
    for row in remaining:
        p = row["position"]
        x, z = (p["x"], p["z"])
        if x == ox:
            stations.add((ox - 1, z))
        if x == ox + w - 1:
            stations.add((ox + w, z))
        if z == oz:
            stations.add((x, oz - 1))
        if z == oz + d - 1:
            stations.add((x, oz + d))
    excluded = {(round(p["x"]), round(p["z"])) for p in unproductive}
    viable = stations - excluded
    distances = {p: 0 for p in viable if p not in blocked}
    queue = deque(distances)
    while queue:
        x, z = queue.popleft()
        for p in ((x - 1, z), (x + 1, z), (x, z - 1), (x, z + 1)):
            if (
                p not in distances
                and p not in blocked
                and (ox - 2 <= p[0] <= ox + w + 1)
                and (oz - 2 <= p[1] <= oz + d + 1)
            ):
                distances[p] = distances[x, z] + 1
                queue.append(p)
    return (
        {
            str(i): distances.get((a["target"]["x"], a["target"]["z"]))
            for i, a in enumerate(actions)
            if a["kind"] == "PROJECT_MOVE"
        },
        [{"x": x, "z": z} for x, z in sorted(viable)],
    )
