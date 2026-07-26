"""
Shared globals and utilities used across service Blueprints.

Extracted from web_app.py during modularization refactoring.
"""
import collections as _collections
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

# sys.path bootstrap
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core import config
from core.task_manager import tasks, tasks_lock, cancel_flags, task_subprocesses

# ── Global FIFO Task Queue ──────────────────────────────────
_queue_condition = threading.Condition()
_queue_waiters = _collections.deque()


def _get_queue_position(task_id: str) -> int:
    """Return the index of a task in the queue, or -1 if not found."""
    try:
        return _queue_waiters.index(task_id)
    except ValueError:
        return -1


# ── Generic Retry Wrapper ─��─────────────────────────────────

def _run_with_retry(fn, *args, max_retries=None, cancel_check=None,
                    progress_cb=None, resume_batch=0,
                    existing_translations=None, checkpoint_cb=None,
                    name="task", **kwargs):
    """Generic auto-retry with cancellation support."""
    auto_retry_max = getattr(config, "AUTO_RETRY_MAX", 2)
    retries = max_retries if max_retries is not None else auto_retry_max
    kwargs.setdefault("cancel_check", cancel_check)
    kwargs.setdefault("progress_cb", progress_cb)
    kwargs.setdefault("resume_batch", resume_batch)
    kwargs.setdefault("existing_translations", existing_translations)
    kwargs.setdefault("checkpoint_cb", checkpoint_cb)

    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except InterruptedError:
            raise
        except Exception as e:
            if cancel_check and cancel_check():
                raise
            last_err = e
            if attempt >= retries:
                break
            wait = 2 ** attempt
            print(f"[Retry] {name} failed (attempt {attempt+1}/{retries+1}), "
                  f"retry in {wait}s: {e}")
            time.sleep(wait)

    raise RuntimeError(f"{name} failed after {retries+1} attempts: {last_err}")


# ── Audio Utilities ─────────────────────────────────────────

def _probe_audio_duration(path: str) -> float:
    """Probe audio duration with ffprobe."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
             path], text=True, timeout=30)
        return float(out.strip())
    except Exception:
        return 0.0


def download_audio(url: str, save_path: str, task_id: str) -> bool:
    """Download audio from URL with progress reporting."""
    from core.task_manager import update_task, is_cancelled
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BiliMix/1.0"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            last_pct = -1
            with open(save_path, "wb") as f:
                while True:
                    if is_cancelled(task_id):
                        return False
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(downloaded / total * 100)
                        if pct != last_pct:
                            update_task(task_id, progress=max(1, min(8, pct // 15)),
                                        message=f"Downloading... {pct}%")
                            last_pct = pct
        return True
    except Exception as e:
        print(f"[Download] Failed: {e}")
        return False


def generate_task_id(url: str) -> str:
    """Generate deterministic task ID from URL."""
    return hashlib.md5((url or "").encode()).hexdigest()


# ── Disk Persistence Helpers ────────────────────────────────

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


# ── File Utilities ──────────────────────────────────────────

def _cleanup_intermediate_files(result_dir: str):
    """Remove intermediate TTS cache files."""
    cache_dir = os.path.join(result_dir, "tts_confucius_cache")
    if os.path.isdir(cache_dir):
        try:
            shutil.rmtree(cache_dir)
        except Exception:
            pass
    gapfill_dir = os.path.join(result_dir, "gapfill")
    if os.path.isdir(gapfill_dir):
        try:
            shutil.rmtree(gapfill_dir)
        except Exception:
            pass


def _try_resolve_local_url(url: str) -> str:
    """Try to resolve a URL to local file path."""
    if not url:
        return ""
    if url.startswith("file://"):
        p = url[len("file://"):]
        if os.path.isfile(p):
            return p
        return ""
    if url.startswith("/") and os.path.isfile(url):
        return url
    download_dir = config.DOWNLOAD_DIR
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if name:
        candidate = os.path.join(download_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _make_audio_url(abs_path: str) -> str:
    """Convert absolute file path to API download URL."""
    if not abs_path:
        return ""
    rel = os.path.relpath(abs_path, config.RESULT_DIR)
    return f"/api/audio/{rel}"


# ── Subprocess Management ───────────────────────────────────

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
