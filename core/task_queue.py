"""One process-wide execution slot for model-heavy work.

Pending jobs can be removed or reordered. An active job owns the slot until its
callback exits, even when its task is cancelled or deleted in another thread.
Importing this module starts no thread; the worker is created on first submit.
"""

from collections import OrderedDict
from collections.abc import Callable
import logging
import threading


class TaskQueue:
    def __init__(self):
        self._lock = threading.Condition()
        self._pending = OrderedDict()
        self._active = None
        self._worker = None
        self._closed = False

    def submit(self, task_id: str, callback: Callable[[], None]) -> None:
        if not task_id or not callable(callback):
            raise ValueError("A task ID and callback are required")
        with self._lock:
            if self._closed:
                raise ValueError("Task queue is closed")
            if task_id == self._active or task_id in self._pending:
                raise ValueError("Task is already queued or running")
            self._pending[task_id] = callback
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._run, daemon=True, name="bilimix-task-worker"
                )
                self._worker.start()

    def discard(self, task_id: str) -> bool:
        """Remove pending work only; cancellation never releases an active slot."""
        with self._lock:
            removed = self._pending.pop(task_id, None) is not None
            self._lock.notify_all()
            return removed

    def wait(self, task_id: str, timeout: float | None = None) -> bool:
        """Wait for a removed/cancelled job to finish before cleaning its files."""
        with self._lock:
            return self._lock.wait_for(
                lambda: task_id != self._active and task_id not in self._pending,
                timeout,
            )

    def snapshot(self) -> list[str]:
        with self._lock:
            return ([self._active] if self._active is not None else []) + list(
                self._pending
            )

    def position(self, task_id: str) -> int:
        jobs = self.snapshot()
        return jobs.index(task_id) if task_id in jobs else -1

    def move(self, task_id: str, direction: str) -> bool:
        if direction not in ("up", "down"):
            raise ValueError("direction must be up or down")
        with self._lock:
            if task_id not in self._pending:
                raise ValueError("Only a pending task can be reordered")
            ids = list(self._pending)
            index = ids.index(task_id)
            target = index + (-1 if direction == "up" else 1)
            if not 0 <= target < len(ids):
                return False
            ids[index], ids[target] = ids[target], ids[index]
            self._pending = OrderedDict((key, self._pending[key]) for key in ids)
            return True

    def shutdown(self, wait=True):
        """Stop accepting work, discard pending jobs, and finish the active job."""
        with self._lock:
            self._closed = True
            self._pending.clear()
            self._lock.notify_all()
            worker = self._worker
        if wait and worker is not None and worker is not threading.current_thread():
            worker.join()

    def _run(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._worker = None
                    return
                self._active, callback = self._pending.popitem(last=False)
            try:
                callback()
            except Exception:
                logging.exception("Unhandled task callback: %s", self._active)
            finally:
                with self._lock:
                    self._active = None
                    self._lock.notify_all()


task_queue = TaskQueue()
