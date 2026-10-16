def current_rows(runtime):
    candidates = [
        task
        for task in runtime.tasks.values()
        if not task.provisional
        and str(task.status) not in {"CANCELLED", "SUPERSEDED", "FAILED"}
        and (task.expected_output == "answer")
    ]
    if not candidates:
        return []
    task = max(candidates, key=lambda item: (item.created_at, item.task_id))
    result = runtime.results.get((task.task_id, task.version))
    if result is None:
        question = "The worker is handling this current service turn. Delegate its replies and diagnostic follow-ups to the harness."
    else:
        question = "Most recent service response: " + result.spoken_answer
    return [
        {
            "task_id": task.task_id,
            "version": task.version,
            "objective": "Current service turn: " + task.objective[:140],
            "question": question,
        }
    ]
