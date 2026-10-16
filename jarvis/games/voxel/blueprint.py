from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import copy

MAX_CELLS = 512
MAX_WIDTH = 12
MAX_HEIGHT = 8


@dataclass
class Blueprint:
    title: str
    rationale: str
    layers: list
    palette: dict
    cells: list
    size: tuple
    digest: str
    origin: dict | None = None

    def public(self):
        return {
            "title": self.title,
            "rationale": self.rationale,
            "layers": self.layers,
            "palette": self.palette,
            "size": list(self.size),
            "materials": dict(Counter((c["item_id"] for c in self.cells))),
            "count": len(self.cells),
            "digest": self.digest,
            "origin": self.origin,
        }

    def absolute(self):
        if self.origin is None:
            raise ValueError("Choose an observed build site first")
        return [
            {
                "x": c["x"] + self.origin["x"],
                "y": c["y"] + self.origin["y"],
                "z": c["z"] + self.origin["z"],
                "item_id": c["item_id"],
            }
            for c in self.cells
        ]

    def geometry_facts(self):
        occupied = {(c["x"], c["y"], c["z"]) for c in self.cells}
        width, depth, height = self.size
        columns = []
        open_edges = []
        for z in range(depth):
            for x in range(width):
                clearance = 0
                while clearance < height and (x, clearance, z) not in occupied:
                    clearance += 1
                if clearance >= 2:
                    columns.append([x, z, clearance])
                    if x in (0, width - 1) or z in (0, depth - 1):
                        open_edges.append([x, z, clearance])
        walkable = {(x, z) for x, z, _ in columns}
        reachable = {(x, z) for x, z, _ in open_edges}
        frontier = list(reachable)
        while frontier:
            x, z = frontier.pop()
            for point in ((x - 1, z), (x + 1, z), (x, z - 1), (x, z + 1)):
                if point in walkable and point not in reachable:
                    reachable.add(point)
                    frontier.append(point)
        interior = {
            (x, z) for x, z in walkable if 0 < x < width - 1 and 0 < z < depth - 1
        }
        roofed = {
            (x, z)
            for x, z in interior
            if any(((x, y, z) in occupied for y in range(2, height)))
        }
        wall_counts = {}
        for y in range(min(2, height)):
            wall_counts[y] = sum(
                (
                    (x, y, z) in occupied
                    for x in range(width)
                    for z in range(depth)
                    if x in (0, width - 1) or z in (0, depth - 1)
                )
            )
        return {
            "axes": "x=width, z=depth, y=height; y=0 is first block ABOVE existing ground; dot is air; ground is not in the blueprint",
            "size_xyz": [width, height, depth],
            "blocks_per_y": dict(Counter((c["y"] for c in self.cells))),
            "ground_columns_with_2plus_air_count": len(columns),
            "ground_columns_sample": columns[:16],
            "openings_at_perimeter": open_edges[:16],
            "interior_columns_with_2plus_air": len(interior),
            "interior_reachable_from_entry": len(interior & reachable),
            "roofed_reachable_interior_columns": len(roofed & reachable),
            "perimeter_blocks_lower_layers": wall_counts,
            "total_materials": dict(Counter((c["item_id"] for c in self.cells))),
        }


def parse_blueprint(value, materials, origin=None):
    if not isinstance(value, dict):
        raise ValueError("Blueprint object required")
    if "boxes" in value:
        value = expand_boxes(value)
    palette = value.get("palette")
    layers = value.get("layers")
    if isinstance(palette, dict) and palette.get(".") == "air":
        palette = {k: v for k, v in palette.items() if k != "."}
    if not isinstance(palette, dict) or not 1 <= len(palette) <= 8:
        raise ValueError("One to eight palette entries required")
    if any(
        (
            not isinstance(k, str)
            or len(k) != 1
            or (not k.isascii())
            or (k == ".")
            or (not isinstance(v, str))
            or (v not in materials)
            for k, v in palette.items()
        )
    ):
        raise ValueError("Unsupported palette item")
    if not isinstance(layers, list) or not 1 <= len(layers) <= MAX_HEIGHT:
        raise ValueError("Invalid layer count")
    occupied = {}
    width = None
    depth = None
    height = 0
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("Invalid layer")
        lo = layer.get("y")
        hi = layer.get("through_y")
        hi = lo if hi is None else hi
        rows = layer.get("rows")
        if (
            type(lo) is not int
            or type(hi) is not int
            or (not 0 <= lo <= hi < MAX_HEIGHT)
        ):
            raise ValueError("Invalid layer height")
        if (
            not isinstance(rows, list)
            or not 1 <= len(rows) <= MAX_WIDTH
            or any((not isinstance(row, str) for row in rows))
        ):
            raise ValueError("Invalid rows")
        if width is None:
            width = len(rows[0])
            depth = len(rows)
        if (
            not 1 <= width <= MAX_WIDTH
            or len(rows) != depth
            or any((len(row) != width for row in rows))
        ):
            raise ValueError("Layers must have consistent dimensions")
        for y in range(lo, hi + 1):
            if y in occupied:
                raise ValueError("Overlapping layer heights")
            occupied[y] = rows
        height = max(height, hi + 1)
    cells = []
    for y, rows in sorted(occupied.items()):
        for z, row in enumerate(rows):
            for x, char in enumerate(row):
                if char == ".":
                    continue
                if char not in palette:
                    raise ValueError("Unknown material symbol")
                cells.append({"x": x, "y": y, "z": z, "item_id": palette[char]})
    if not cells or len(cells) > MAX_CELLS:
        raise ValueError("Blueprint block limit exceeded")
    if origin is not None:
        if set(origin) != {"x", "y", "z"} or any(
            (type(v) is not int or abs(v) > 30000 for v in origin.values())
        ):
            raise ValueError("Invalid site")
        origin = dict(origin)
    layers = [
        {
            k: copy.deepcopy(layer[k])
            for k in ("y", "through_y", "rows")
            if k in layer and layer[k] is not None
        }
        for layer in layers
    ]
    palette = dict(palette)
    canonical = {"cells": cells, "origin": origin}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    return Blueprint(
        str(value.get("title", "建造方案"))[:100],
        str(value.get("rationale", ""))[:500],
        layers,
        palette,
        cells,
        (width, depth, height),
        digest,
        origin,
    )


def expand_boxes(value):
    size = value.get("size")
    boxes = value.get("boxes")
    palette = value.get("palette")
    if (
        not isinstance(size, list)
        or len(size) != 3
        or any((type(n) is not int for n in size))
    ):
        raise ValueError("Box geometry requires size [width,depth,height]")
    w, d, h = size
    if not 1 <= w <= MAX_WIDTH or not 1 <= d <= MAX_WIDTH or (not 1 <= h <= MAX_HEIGHT):
        raise ValueError("Box geometry bounds exceeded")
    if (
        not isinstance(boxes, list)
        or not 1 <= len(boxes) <= 64
        or (not isinstance(palette, dict))
    ):
        raise ValueError("Invalid geometry boxes")
    grid = {}
    for box in boxes:
        lo = box.get("min")
        hi = box.get("max")
        symbol = box.get("material")
        if (
            not isinstance(lo, list)
            or not isinstance(hi, list)
            or len(lo) != 3
            or (len(hi) != 3)
            or any((type(v) is not int for v in lo + hi))
        ):
            raise ValueError("Box coordinates require integer [x,y,z]")
        if any((not 0 <= lo[i] <= hi[i] < limit for i, limit in enumerate((w, h, d)))):
            raise ValueError("Box outside declared geometry")
        if symbol != "." and symbol not in palette:
            raise ValueError("Unknown box material")
        for x in range(lo[0], hi[0] + 1):
            for y in range(lo[1], hi[1] + 1):
                for z in range(lo[2], hi[2] + 1):
                    grid[x, y, z] = symbol
    layers = [
        {
            "y": y,
            "rows": [
                "".join((grid.get((x, y, z), ".") for x in range(w))) for z in range(d)
            ],
        }
        for y in range(h)
    ]
    return {**value, "layers": layers}


def inventory_totals(snapshot):
    counts = Counter()
    for row in snapshot.get("view", {}).get("self", {}).get("inventory_summary", []):
        counts[row["item_id"]] += row["count"]
    return counts


def material_deficits(plan, snapshot, completed=()):
    have = inventory_totals(snapshot)
    done = {
        (tuple((row["target"][axis] for axis in ("x", "y", "z"))), row["item_id"])
        for row in completed
        if row.get("target") and row.get("item_id") and (not row.get("removed"))
    }
    cells = plan.absolute() if plan.origin else plan.cells
    need = Counter(
        (
            c["item_id"]
            for c in cells
            if (tuple((c[axis] for axis in ("x", "y", "z"))), c["item_id"]) not in done
        )
    )
    return {
        item: amount - have[item]
        for item, amount in need.items()
        if amount > have[item]
    }


def site_from_snapshot(snapshot):
    focus = snapshot.get("view", {}).get("focus") or {}
    point = focus.get("place_position")
    if point and point["y"] != focus.get("position", {}).get("y", point["y"]) + 1:
        point = None
    if not point:
        moves = snapshot.get("view", {}).get("survival", {}).get("travel", [])
        own = snapshot.get("view", {}).get("self", {})
        yaw = own.get("yaw", 0)
        import math

        candidates = [
            a
            for a in moves
            if a.get("kind") in {"MOVE", "JUMP_MOVE"}
            and a.get("rise", 0) >= 0
            and a.get("reaches_solid_land", True)
        ]
        if not candidates:
            return None
        move = max(
            candidates,
            key=lambda a: (
                -a.get("dx", 0) * math.sin(yaw) + a.get("dz", 0) * math.cos(yaw)
            ),
        )
        p = move["target"]
        return {
            "x": round(p["x"]),
            "y": math.floor(p["y"] + 0.1) + 1,
            "z": round(p["z"]),
        }
    return {axis: int(point[axis]) for axis in ("x", "y", "z")}


def supported_sites(plan, snapshot):
    terrain = snapshot.get("view", {}).get("construction", {}).get("terrain", [])
    known = {
        (r["position"]["x"], r["position"]["y"], r["position"]["z"]): r["clearance"]
        for r in terrain
    }
    p = snapshot["view"]["self"]["position"]
    w, d, h = plan.size
    sites = []
    for x, y, z in known:
        if all(
            (
                known.get((x + dx, y, z + dz), 0) >= min(h, 4)
                for dx in range(w)
                for dz in range(d)
            )
        ):
            sites.append({"x": int(x), "y": int(y) + 1, "z": int(z)})
    sites.sort(
        key=lambda s: (s["x"] + w / 2 - p["x"]) ** 2 + (s["z"] + d / 2 - p["z"]) ** 2
    )
    return sites[:12]
