"""Exercise execution ownership, not the scheduler's private deque or locks."""

import threading
import pytest
from core.task_queue import TaskQueue


def test_active_cancellation_does_not_release_slot_and_pending_delete_does():
    queue = TaskQueue()
    started = threading.Event()
    release = threading.Event()
    next_started = threading.Event()
    ran = []

    def active():
        started.set()
        assert release.wait(3)
        ran.append("active")

    try:
        queue.submit("active", active)
        assert started.wait(2)
        queue.submit("deleted", lambda: ran.append("deleted"))
        queue.submit("next", lambda: (ran.append("next"), next_started.set()))
        assert queue.discard("deleted")
        assert not queue.discard("active")
        assert not queue.wait("active", 0.03)
        assert not next_started.is_set()
        assert queue.snapshot() == ["active", "next"]
        release.set()
        assert next_started.wait(2)
        assert queue.wait("next", 2)
        assert ran == ["active", "next"]
    finally:
        release.set()
        queue.shutdown()


def test_duplicate_and_reorder_cannot_move_an_active_job():
    queue = TaskQueue()
    release = threading.Event()
    started = threading.Event()
    ran = []
    try:
        queue.submit("active", lambda: (started.set(), release.wait(3)))
        assert started.wait(2)
        queue.submit("one", lambda: ran.append("one"))
        queue.submit("two", lambda: ran.append("two"))
        with pytest.raises(ValueError, match="already"):
            queue.submit("active", lambda: None)
        with pytest.raises(ValueError, match="already"):
            queue.submit("two", lambda: None)
        with pytest.raises(ValueError, match="pending"):
            queue.move("active", "down")
        assert queue.move("two", "up")
        assert queue.position("active") == 0 and queue.position("two") == 1
        release.set()
        assert queue.wait("one", 2)
        assert ran == ["two", "one"]
    finally:
        release.set()
        queue.shutdown()


def test_exception_releases_slot_for_following_work():
    queue = TaskQueue()
    done = threading.Event()

    def failed():
        raise RuntimeError("expected test failure")

    try:
        queue.submit("bad", failed)
        queue.submit("next", done.set)
        assert done.wait(2)
        assert queue.wait("next", 2)
        assert queue.snapshot() == []
    finally:
        queue.shutdown()
