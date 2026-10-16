import asyncio
from dataclasses import dataclass


@dataclass
class _Entry:
    value: object


class SnapshotQueue(asyncio.Queue):
    def __init__(self, *, actor=False):
        super().__init__()
        self.actor = actor
        self.latest = None

    def is_snapshot(self, item):
        if self.actor:
            return (
                isinstance(item, tuple)
                and len(item) == 2
                and (item[0] == "voxel.event")
                and isinstance(item[1], dict)
                and (item[1].get("kind") == "snapshot")
            )
        return (
            isinstance(item, dict)
            and item.get("type") == "event"
            and (item.get("event", {}).get("type") == "voxel.snapshot")
        )

    def put_nowait(self, item):
        snapshot = self.is_snapshot(item)
        if snapshot and self.latest is not None:
            self.latest.value = item
            return
        entry = _Entry(item)
        super().put_nowait(entry)
        if snapshot:
            self.latest = entry

    def get_nowait(self):
        entry = super().get_nowait()
        if entry is self.latest:
            self.latest = None
        return entry.value
