"""Task identity, live snapshots, durable checkpoints, and restart recovery.

SQLite is the query index. task_result.json contains the complete resumable state.
"""

import copy
import json
import logging
import os
import threading

from core import config
from core.task_queue import task_queue
from core.files import atomic_json
from core.database import (
    load_tasks_index as db_load_tasks_index,
    save_task_to_index,
    delete_task_from_index,
    get_task_from_index,
)

# ============================================================
# 内存中的任务状态
# ============================================================

tasks = {}  # {task_id: {status, progress, message, ...}}
_persist_lock = threading.RLock()
tasks_lock = threading.Lock()

cancel_flags = {}  # {task_id: threading.Event}  终止信号
task_subprocesses = {}  # {task_id: subprocess.Popen} 后台子进程引用


def task_identity(*records):
    """Task kind is independent of the shared audio-processing sub-pipeline."""
    records = [record for record in records if record]
    if any(
        r.get("type") == "analysis" or r.get("process_mode") == "analysis"
        for r in records
    ):
        kind = "analysis"
    elif any(
        r.get("type") == "video"
        or r.get("process_mode") == "video"
        or r.get("_video_path")
        or r.get("video_result")
        for r in records
    ):
        kind = "video"
    else:
        kind = "audio"
    return {
        "type": kind,
        "process_mode": kind if kind != "audio" else "sentence_translate",
    }


def _restore_video_source(task):
    if task["type"] in ("video", "analysis") and not task.get("_video_path"):
        source = task.get("url", "")
        if source.startswith("file://"):
            task["_video_path"] = source[len("file://") :]


# ============================================================
# 持久化：通过 SQLite 存储任务索引
# ============================================================


def load_tasks_index() -> dict:
    """从 SQLite 加载历史任务索引"""
    try:
        return db_load_tasks_index()
    except Exception as e:
        import traceback

        print(f"[ERROR] 从 SQLite 加载任务索引失败: {e}")
        traceback.print_exc()
        return {}


def save_task_result_to_disk(result_dir: str, data: dict):
    """将完整任务结果持久化到磁盘（供重启后恢复）"""
    try:
        # 自动注入 skip_confirmation（如果 data 中没有，从内存 tasks 中获取）
        if "skip_confirmation" not in data:
            task_id = data.get("task_id", "")
            if task_id and task_id in tasks:
                data["skip_confirmation"] = tasks[task_id].get(
                    "skip_confirmation", getattr(config, "SKIP_CONFIRMATION", True)
                )
            else:
                data["skip_confirmation"] = getattr(config, "SKIP_CONFIRMATION", True)
        # 自动注入 type，确保 redo 恢复时不会丢失视频/音频类型
        if "type" not in data:
            task_id = data.get("task_id", "")
            if task_id and task_id in tasks:
                mem_type = tasks[task_id].get("type", "")
                if mem_type:
                    data["type"] = mem_type
        # 自动注入 _subtitle_path，删除任务时能找回并清理用户上传的字幕文件
        if "_subtitle_path" not in data:
            task_id = data.get("task_id", "")
            if task_id and task_id in tasks:
                sub_path = tasks[task_id].get("_subtitle_path", "")
                if sub_path:
                    data["_subtitle_path"] = sub_path

        task_id = data.get("task_id", "")
        current = tasks.get(task_id, {})
        data.update(task_identity(current, data))
        for key in (
            "url",
            "title",
            "created_at",
            "tts_reference_mode",
            "_tts_reference_manifest",
            "_analysis_id",
            "_dub_manifest",
            "_speaker_analysis",
            "_reference_manifest",
            "_audio_path",
            "_media_sha256",
            "_video_path",
            "_raw_segments",
            "_subtitle_mode",
            "_subtitle_font_size",
        ):
            if key in current and key not in data:
                data[key] = current[key]
        path = os.path.join(result_dir, "task_result.json")
        atomic_json(path, data)
        logging.debug("Task checkpoint saved: %s", path)
    except Exception as e:
        print(f"[WARN] 保存任务结果失败: {e}")
        raise


# ============================================================
# 任务状态操作
# ============================================================


def update_task(task_id: str, **kwargs):
    """Publish critical states only after their checkpoint and index are durable."""
    with _persist_lock:
        with tasks_lock:
            previous = tasks.get(task_id)
            if previous is None:
                return
            candidate = dict(previous)
        timing = candidate.get("_step_timing", [])
        if "step" in kwargs and (
            not timing or timing[-1]["step"] != kwargs["step"] or timing[-1].get("end")
        ):
            import time

            now = round(time.time(), 1)
            timing = [dict(item) for item in candidate.get("_step_timing", [])]
            if timing:
                timing[-1]["end"] = now
            timing.append(dict(step=kwargs["step"], start=now, end=0))
            candidate["_step_timing"] = timing
        candidate.update(kwargs)
        candidate.update(task_identity(previous, candidate))
        durable_status = kwargs.get("status") in (
            "completed",
            "error",
            "cancelled",
            "awaiting_confirmation",
            "awaiting_sentence_confirmation",
            "queued",
        )
        if durable_status:
            import time

            timing = [dict(item) for item in candidate.get("_step_timing", [])]
            if timing and not timing[-1].get("end"):
                timing[-1]["end"] = round(time.time(), 1)
            candidate["_step_timing"] = timing
        if durable_status or any(
            kwargs.get(key) for key in ("_basename", "_audio_path", "original_duration")
        ):
            _persist_task(task_id, candidate)
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id] = candidate


def _persist_task(task_id: str, snapshot=None):
    """Persist a resumable snapshot and its query index under one writer lock."""
    with _persist_lock:
        with tasks_lock:
            task = copy.deepcopy(
                snapshot if snapshot is not None else tasks.get(task_id)
            )
            if not task:
                return
            # 优先使用顶层 original_duration（下载完成后立即设置），否则尝试从 result 取
            orig_dur = task.get("original_duration", 0)
            if not orig_dur and task.get("result"):
                orig_dur = (task.get("result") or {}).get("original_duration", 0)
            summary = {
                "task_id": task.get("task_id"),
                "url": task.get("url", ""),
                "title": task.get("title", ""),
                "difficulty": task.get("difficulty", ""),
                "process_mode": task.get("process_mode", "sentence_translate"),
                "type": task.get("type", "audio"),
                "step": task.get("step", ""),
                "status": task.get("status"),
                "progress": task.get("progress", 0),
                "message": task.get("message", ""),
                "created_at": task.get("created_at", ""),
                "keep_bgm": task.get("keep_bgm", False),
                "tts_reference_mode": task.get("tts_reference_mode", "speaker"),
                "basename": (
                    (task.get("result") or {}).get("basename")
                    or task.get("_basename", "")
                ),
                "total_words": (
                    (task.get("result") or {}).get("total_words", 0)
                    if task.get("result")
                    else 0
                ),
                "total_replacements": (
                    (task.get("result") or {}).get("total_replacements", 0)
                    if task.get("result")
                    else 0
                ),
                "original_duration": orig_dur,
                "mixed_duration": (
                    (task.get("result") or {}).get("mixed_duration", 0)
                    if task.get("result")
                    else 0
                ),
                "queue_order": task_queue.position(task_id),
            }
            summary.update(task_identity(task))
        if task.get("_basename"):
            save_task_result_to_disk(
                os.path.join(config.RESULT_DIR, task["_basename"]), task
            )
        save_task_to_index(task_id, summary)


def get_task(task_id: str) -> dict:
    """线程安全地获取任务状态（返回副本）"""
    with tasks_lock:
        return dict(tasks.get(task_id, {}))


def is_cancelled(task_id: str) -> bool:
    """检查任务是否被请求终止"""
    event = cancel_flags.get(task_id)
    return event.is_set() if event else False


# ============================================================
# 任务恢复
# ============================================================


class InvalidTaskCheckpoint(ValueError):
    """A present checkpoint is corrupt or belongs to a different task."""


def read_task_checkpoint(task_id, basename):
    path = os.path.join(config.RESULT_DIR, basename, "task_result.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as stream:
            saved = json.load(stream)
        if not isinstance(saved, dict) or saved.get("task_id") != task_id:
            raise ValueError("task identity mismatch")
        for key in ("translations", "tts_audio_map", "_checkpoint_translations"):
            if key in saved:
                saved[key] = {int(k): v for k, v in (saved[key] or {}).items()}
        return saved
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise InvalidTaskCheckpoint(
            f"任务 {task_id} 的检查点损坏，不能自动回退或重新翻译: {exc}"
        ) from exc


def restore_task_from_disk(task_id: str) -> dict:
    """Restore the whole snapshot; source artifacts are never guessed from loose files."""
    summary = get_task_from_index(task_id)
    if not summary:
        return {}
    basename = summary.get("basename", "")
    if not basename:
        from pathlib import Path

        matches = []
        for path in Path(config.RESULT_DIR).glob(f"*{task_id[:8]}*/task_result.json"):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(candidate, dict) and candidate.get("task_id") == task_id:
                matches.append(path.parent.name)
        if len(matches) == 1:
            basename = matches[0]
    saved = read_task_checkpoint(task_id, basename) if basename else {}
    task = dict(
        segments=[],
        translations={},
        translated_indices=[],
        sentence_pairs=[],
        time_mapping=[],
        tts_audio_map={},
        result=None,
        video_result=None,
        transcription_text="",
        _audio_path="",
        _video_path="",
        _subtitle_path="",
        _subtitle_mode="bilingual",
        _subtitle_font_size=-1,
    )
    task.update(summary)
    task.update(saved)
    task.update(task_identity(summary, saved))
    status = summary.get("status") or saved.get("status", "error")
    interrupted = status in ("queued", "downloading", "processing")
    if interrupted:
        status = "error"
    task.update(
        task_id=task_id,
        _basename=basename,
        status=status,
        tts_reference_mode=saved.get(
            "tts_reference_mode", summary.get("tts_reference_mode", "speaker")
        ),
        skip_confirmation=saved.get(
            "skip_confirmation", getattr(config, "SKIP_CONFIRMATION", True)
        ),
        progress=100 if status == "completed" else task.get("progress", 0),
        step="done" if status == "completed" else task.get("step", "download"),
        message="任务因服务中断而暂停，可继续原任务重试"
        if interrupted
        else task.get("message", ""),
    )
    if not task.get("_audio_path"):
        task["_audio_path"] = (task.get("result") or {}).get("original_audio", "")
    _restore_video_source(task)
    with tasks_lock:
        tasks[task_id] = task
    return dict(task)
