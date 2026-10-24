"""Cancellation of model processes owned by a task."""

from core.task_manager import task_subprocesses


def _kill_task_subprocesses(task_id: str):
    """Kill all subprocesses for a task."""
    procs = task_subprocesses.get(task_id)
    if not procs:
        return
    if not isinstance(procs, list):
        procs = [procs]
    for p in procs:
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    task_subprocesses.pop(task_id, None)
