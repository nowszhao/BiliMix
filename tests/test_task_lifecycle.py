"""HTTP cancellation/deletion cannot bypass the shared execution owner."""

import threading
from pathlib import Path
import pytest
from core import config, task_manager as tm, tts_jobs
from core.files import atomic_json
from core.task_queue import TaskQueue
from application import task_execution
from services import task_api, task_query, tts_api
from services.web_app import create_app


@pytest.fixture
def queue(monkeypatch):
    queue = TaskQueue()
    for module in (tm, task_execution, task_api, task_query, tts_api):
        monkeypatch.setattr(module, "task_queue", queue)
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    yield queue
    queue.shutdown()


def test_cancelled_media_holds_execution_slot_until_its_worker_exits(
    queue, tmp_path, monkeypatch
):
    started, release, tts_started = (threading.Event() for _ in range(3))
    monkeypatch.setattr(
        config, "TTS_JOB_DIR", str(Path(config.RESULT_DIR) / "lifecycle-tts")
    )

    def media(task_id):
        started.set()
        assert release.wait(4)

    def synth(texts, reference, job_id, **kwargs):
        tts_started.set()
        path = Path(tts_jobs.get_job_dir(job_id)) / "audio.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return [dict(index=0, text=texts[0], output_path=str(path))]

    monkeypatch.setattr(task_execution, "run_source_task", media)
    monkeypatch.setattr(tts_api, "synthesize_texts", synth)
    task_id = "lifecycle-media"
    tm.tasks[task_id] = dict(task_id=task_id, type="video", status="queued")
    tm.cancel_flags[task_id] = threading.Event()
    client = create_app().test_client()
    try:
        task_execution.enqueue_task(task_id)
        assert started.wait(2)
        reference = Path(config.DOWNLOAD_DIR) / "lifecycle-ref.wav"
        reference.write_bytes(b"reference")
        response = client.post(
            "/api/tts/synthesize", json=dict(ref_audio=str(reference), text="测试。")
        )
        assert response.status_code == 202
        job_id = response.get_json()["job_id"]
        assert tts_jobs.get_job(job_id)["status"] == "queued"
        assert client.post(f"/api/task/{task_id}/cancel").status_code == 200
        assert not tts_started.is_set()
        assert queue.position(task_id) == 0
        release.set()
        assert tts_started.wait(2)
        assert queue.wait("tts:" + job_id, 2)
        assert tts_jobs.get_job(job_id)["status"] == "completed"
    finally:
        release.set()


def test_deleted_waiting_task_is_never_executed(queue):
    release, started, deleted_ran = (threading.Event() for _ in range(3))
    queue.submit("active", lambda: (started.set(), release.wait(4)))
    assert started.wait(2)
    task_id = "lifecycle-deleted"
    tm.tasks[task_id] = dict(task_id=task_id, type="audio", status="queued")
    tm.cancel_flags[task_id] = threading.Event()
    queue.submit(task_id, deleted_ran.set)
    try:
        assert (
            create_app().test_client().delete(f"/api/task/{task_id}").status_code == 200
        )
        assert queue.position(task_id) == -1
        release.set()
        assert queue.wait("active", 2)
        assert not deleted_ran.is_set() and not tm.get_task(task_id)
    finally:
        release.set()


def test_failed_snapshot_commit_preserves_previous_checkpoint(tmp_path, monkeypatch):
    from core import files

    path = tmp_path / "task.json"
    atomic_json(path, {"status": "queued"})

    def fail(*a):
        raise OSError("disk error")

    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_json(path, {"status": "completed"})
    assert "queued" in path.read_text()
    assert list(tmp_path.iterdir()) == [path]


def test_completed_state_is_published_after_durable_commit(tmp_path, monkeypatch):
    task_id = "durable-publication"
    tm.tasks[task_id] = dict(
        task_id=task_id, type="audio", status="processing", _basename=task_id
    )
    started, release = (threading.Event() for _ in range(2))
    original = tm.save_task_result_to_disk

    def blocked(*args):
        started.set()
        assert release.wait(3)
        original(*args)

    monkeypatch.setattr(tm, "save_task_result_to_disk", blocked)
    worker = threading.Thread(
        target=lambda: tm.update_task(task_id, status="completed")
    )
    worker.start()
    try:
        assert started.wait(2)
        assert tm.get_task(task_id)["status"] == "processing"
    finally:
        release.set()
        worker.join(timeout=3)
    assert tm.get_task(task_id)["status"] == "completed"
    tm.tasks.pop(task_id)
    assert tm.restore_task_from_disk(task_id)["status"] == "completed"


def test_repeated_progress_does_not_create_artificial_step_boundaries():
    task_id = "step-timing"
    tm.tasks[task_id] = dict(task_id=task_id, type="audio", status="processing")
    tm.update_task(task_id, step="synthesize", progress=60)
    tm.update_task(task_id, step="synthesize", progress=70)
    tm.update_task(task_id, step="synthesize", progress=80)
    assert len(tm.get_task(task_id)["_step_timing"]) == 1
    tm.update_task(task_id, step="merge", progress=90)
    steps = tm.get_task(task_id)["_step_timing"]
    assert len(steps) == 2 and steps[0]["end"] and not steps[1]["end"]
