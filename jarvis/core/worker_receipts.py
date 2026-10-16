import asyncio
from collections import deque
from copy import deepcopy
import json


class ToolReceipts:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.rows = deque(maxlen=64)

    def others(self, task):
        return deepcopy(
            [
                row
                for row in self.rows
                if (row["task_id"], row["version"]) != (task.task_id, task.version)
            ]
        )

    def start(self, task, call):
        function = call.get("function", {})
        arguments = function.get("arguments", "")
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            pass
        row = {
            "task_id": task.task_id,
            "version": task.version,
            "source_utterance_id": task.source_utterance_id,
            "tool_call_id": call["id"],
            "tool": function.get("name", ""),
            "arguments": arguments,
            "execution": "in_flight",
        }
        self.rows.append(row)
        return row

    def returned(self, row, output):
        row.update(execution="returned", observation=deepcopy(output))

    def interrupted(self, row, error):
        row.update(
            execution="outcome_unknown",
            error=type(error).__name__,
            instruction="Execution was interrupted without a tool receipt. Do not claim success or assume it had no effect. Reconcile the actual state before repeating a mutation.",
        )
