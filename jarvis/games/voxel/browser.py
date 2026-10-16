import base64
import time


def snapshot_view(s):
    view = s.get("view", {})
    construction = view.get("construction") or {}
    project = construction.get("project")
    return {
        **{k: v for k, v in s.items() if k != "view"},
        "view": {
            **{
                k: view[k]
                for k in ("game", "self", "environment", "focus", "unknown_fields")
                if k in view
            },
            "construction": {
                "materials": [
                    {k: r[k] for k in ("item_id", "name") if k in r}
                    for r in construction.get("materials", [])
                ],
                "project": {
                    k: project[k]
                    for k in ("project_id", "revision", "status", "completed", "total")
                    if k in project
                }
                if project
                else None,
            },
        },
    }


class BrowserDelivery:
    def __init__(self):
        self.last_snapshot = 0.0
        self.binding = None

    def message(self, message):
        if message.get("type") == "audio":
            return {
                **message,
                "format": "f32le",
                "sample_count": len(base64.b64decode(message["pcm"])) // 4,
            }
        if message.get("type") != "event":
            return message
        e = message["event"]
        kind = e["type"]
        p = e["payload"]
        if kind.startswith(("jev.", "venus.")) or kind in {
            "assistant.playback.progress",
            "assistant.audio.generated",
        }:
            return None
        if kind == "voxel.snapshot":
            s = p["snapshot"]
            binding = tuple(
                (
                    s.get(k)
                    for k in (
                        "world_id",
                        "game_instance_id",
                        "player_life_id",
                        "bridge_epoch",
                        "phase",
                    )
                )
            ) + (
                s.get("control", {}).get("input_mode"),
                s.get("control", {}).get("client_menu"),
            )
            now = time.monotonic()
            if binding == self.binding and now - self.last_snapshot < 0.25:
                return None
            self.last_snapshot = now
            self.binding = binding
            p = {**p, "snapshot": snapshot_view(s)}
        elif kind == "voxel.task" and p.get("task"):
            t = p["task"]
            p = {
                **p,
                "task": {
                    k: t[k]
                    for k in (
                        "id",
                        "kind",
                        "status",
                        "request",
                        "authorized",
                        "completed",
                        "quantity",
                        "pending_question",
                        "agenda",
                        "agenda_index",
                    )
                    if k in t
                },
            }
            if t.get("outcome"):
                p["task"]["summary"] = t["outcome"]["strategy"]
        elif kind == "voxel.stream.stats":
            return None
        return {**message, "event": {**e, "payload": p}}
