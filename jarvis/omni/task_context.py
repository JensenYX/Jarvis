import time


def task_status_snapshot(runtime, current_task, *, max_tasks=12):
    if type(max_tasks) is not int or not 1 <= max_tasks <= 32:
        raise ValueError("Task status context needs 1..32 tasks")
    excluded_id = current_task.task_id if current_task is not None else None
    eligible = [
        t
        for t in runtime.tasks.values()
        if t.task_id != excluded_id and (not t.provisional)
    ]
    records = []
    for task in eligible[-max_tasks:]:
        result = runtime.results.get((task.task_id, task.version))
        records.append(
            {
                "task_id": task.task_id,
                "version": task.version,
                "objective": task.objective,
                "status": task.status.value,
                "delivery_state": task.delivery_state,
                "created_at": task.created_at,
                "completed_at": task.completed_at,
                "result_status": result.status if result else None,
                "successful_write_receipts": [
                    {
                        k: a[k]
                        for k in ("filename", "title", "sha256", "bytes")
                        if k in a
                    }
                    for a in result.artifacts
                ]
                if result
                else [],
                "result_available": result is not None,
            }
        )
    return {
        "source": "same_user_task_registry_snapshot",
        "observed_at": time.time(),
        "tasks": records,
        "omitted_tasks": len(eligible) - len(records),
        "interpretation": "These are observed task states and successful write receipts, not assistant dialogue or permission to access files. A write receipt establishes a saved artifact at that operation, not its current contents or continued existence. Delivery state is separate from completion. A missing task or missing result is unknown, not proof that no work was done. Available_artifacts lists only file access grants for THIS task; an empty list does not invalidate these other task receipts. Use the current request to judge which task is relevant; do not answer an unrelated old task. No reply text is supplied.",
    }
