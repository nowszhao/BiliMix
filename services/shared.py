"""HTTP response helpers for media URLs and persisted task metadata."""
import hashlib
import json
import os
from core import config

def generate_task_id(url: str) -> str:
    """Generate deterministic task ID from URL."""
    return hashlib.md5((url or "").encode()).hexdigest()


def _load_step_timing_from_disk(task: dict) -> list:
    """Load step_timing from task_result.json if memory is empty."""
    basename = task.get("_basename", "")
    if not basename:
        return task.get("_step_timing", [])
    result_dir = os.path.join(config.RESULT_DIR, basename)
    checkpoint_path = os.path.join(result_dir, "task_result.json")
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return saved.get("_step_timing", task.get("_step_timing", []))
        except Exception:
            pass
    return task.get("_step_timing", [])


def _load_video_result_from_disk(task: dict) -> dict:
    """Load video_result from task_result.json."""
    basename = task.get("_basename", "")
    if not basename:
        return task.get("video_result", {})
    result_dir = os.path.join(config.RESULT_DIR, basename)
    checkpoint_path = os.path.join(result_dir, "task_result.json")
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return saved.get("video_result", task.get("video_result", {}))
        except Exception:
            pass
    return task.get("video_result", {})


def _make_audio_url(abs_path: str) -> str:
    """Convert absolute file path to API download URL."""
    if not abs_path:
        return ""
    rel = os.path.relpath(abs_path, config.RESULT_DIR)
    return f"/api/audio/{rel}"
