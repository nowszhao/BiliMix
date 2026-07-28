"""
Task Query Blueprint - list, status, result, delete endpoints.
"""
import os
import shutil
import sys
import traceback

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Blueprint, jsonify, request
from core import config
from core.task_manager import (
    tasks, tasks_lock, cancel_flags, task_subprocesses,
    get_task, is_cancelled, restore_task_from_disk,
    update_task,
)
from core.database import load_tasks_index, delete_task_from_index
from services.shared import _queue_condition, _queue_waiters, _kill_task_subprocesses, _load_step_timing_from_disk, _load_video_result_from_disk

task_query_bp = Blueprint('task_query', __name__, url_prefix='/api')


# ── List Tasks ──────────────────────────────────────────────

@task_query_bp.route("/tasks")
def list_tasks():
    index = load_tasks_index()
    result = {}
    for tid, summary in index.items():
        result[tid] = summary

    with tasks_lock:
        for tid, task in tasks.items():
            sqlite_summary = index.get(tid, {})
            sqlite_status = sqlite_summary.get("status", "")
            if sqlite_status in ("completed", "error", "cancelled"):
                continue
            result[tid] = {
                "task_id": task.get("task_id"),
                "url": task.get("url", ""),
                "title": task.get("title", ""),
                "status": task.get("status"),
                "progress": task.get("progress", 0),
                "message": task.get("message", ""),
                "created_at": task.get("created_at", ""),
                "process_mode": task.get("process_mode", "sentence_translate"),
                "type": task.get("type", "audio"),
                "basename": ((task.get("result") or {}).get("basename", "")
                             if task.get("result") else task.get("_basename", "")),
                "total_words": ((task.get("result") or {}).get("total_words", 0)
                                if task.get("result") else 0),
                "total_replacements": ((task.get("result") or {}).get("total_replacements", 0)
                                       if task.get("result") else 0),
                "original_duration": (task.get("original_duration", 0)
                                      or ((task.get("result") or {}).get("original_duration", 0)
                                          if task.get("result") else 0)),
                "mixed_duration": ((task.get("result") or {}).get("mixed_duration", 0)
                                   if task.get("result") else 0),
                "keep_bgm": task.get("keep_bgm", False),
                "queue_order": task.get("queue_order",
                                        sqlite_summary.get("queue_order", 0)),
            }

    sorted_tasks = sorted(result.values(), key=lambda t: t.get("created_at", ""), reverse=True)
    limit = request.args.get("limit", type=int, default=0)
    if limit > 0:
        sorted_tasks = sorted_tasks[:limit]
    return jsonify({"tasks": sorted_tasks})


# ── Delete Task ─────────────────────────────────────────────

@task_query_bp.route("/task/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    task = get_task(task_id)
    index = load_tasks_index()

    if not task and task_id not in index:
        return jsonify({"error": "任务不存在"}), 404

    # Terminate if running
    if task and task.get("status") in ("downloading", "processing", "queued"):
        event = cancel_flags.get(task_id)
        if event:
            event.set()
        _kill_task_subprocesses(task_id)

    basename = ""
    audio_path = ""
    if task:
        basename = task.get("_basename", "")
        audio_path = task.get("_audio_path", "")
        if not basename and task.get("result"):
            basename = task["result"].get("basename", "")
    if not basename and task_id in index:
        basename = index[task_id].get("basename", "")

    cleaned = []
    if basename:
        result_dir = os.path.join(config.RESULT_DIR, basename)
        if os.path.isdir(result_dir):
            shutil.rmtree(result_dir, ignore_errors=True)
            cleaned.append(f"data/results/{basename}/")

        output_json = os.path.join(config.OUTPUT_DIR, f"{basename}.json")
        if os.path.exists(output_json):
            os.remove(output_json)
            cleaned.append(f"data/transcripts/{basename}.json")

        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
            cleaned.append(os.path.basename(audio_path))
        else:
            for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
                p = os.path.join(config.DOWNLOAD_DIR, f"{basename}{ext}")
                if os.path.exists(p):
                    os.remove(p)
                    cleaned.append(f"data/downloads/{basename}{ext}")

    # Remove from queue if waiting
    with _queue_condition:
        if task_id in _queue_waiters:
            _queue_waiters.remove(task_id)
        _queue_condition.notify_all()

    with tasks_lock:
        tasks.pop(task_id, None)
    cancel_flags.pop(task_id, None)

    if task_id in index:
        delete_task_from_index(task_id)

    return jsonify({"message": "任务已删除", "cleaned_files": cleaned})


# ── Task Status ─────────────────────────────────────────────

@task_query_bp.route("/task/<task_id>")
def get_task_status(task_id):
    task = get_task(task_id)
    if not task:
        task = restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    return jsonify({
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "step": task.get("step"),
        "progress": task.get("progress", 0),
        "message": task.get("message", ""),
        "process_mode": task.get("process_mode", "sentence_translate"),
    })


# ── Task Result ─────────────────────────────────────────────

@task_query_bp.route("/task/<task_id>/result")
def get_task_result(task_id):
    task = get_task(task_id)
    if not task:
        task = restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    result = task.get("result") or {}
    segments = task.get("segments", [])
    mixed_segments = task.get("segments_mixed", [])
    tmap = task.get("time_mapping", [])
    translations = task.get("translations", {})
    sentence_pairs = task.get("sentence_pairs", [])
    video_result = task.get("video_result") or _load_video_result_from_disk(task)
    step_timing = _load_step_timing_from_disk(task)

    return jsonify({
        "task_id": task.get("task_id"),
        "url": task.get("url", ""),
        "title": task.get("title", ""),
        "status": task.get("status"),
        "step": task.get("step"),
        "progress": task.get("progress", 0),
        "message": task.get("message", ""),
        "created_at": task.get("created_at", ""),
        "process_mode": task.get("process_mode", "sentence_translate"),
        "transcription_text": task.get("transcription_text", ""),
        "segments_count": len(segments),
        "segments": segments[:20] if segments else [],
        "segments_mixed": mixed_segments[:20] if mixed_segments else [],
        "total_segments": len(segments),
        "difficult_words": task.get("difficult_words", []),
        "translations": translations,
        "translated_indices": task.get("translated_indices", []),
        "sentence_pairs": sentence_pairs,
        "result": result,
        "time_mapping": tmap,
        "video_result": video_result,
        "original_duration": result.get("original_duration", 0),
        "mixed_duration": result.get("mixed_duration", 0),
        "_step_timing": step_timing,
        "_video_path": task.get("_video_path", ""),
        "_subtitle_mode": task.get("_subtitle_mode", ""),
        "_subtitle_font_size": task.get("_subtitle_font_size", -1),
        "_ref_select_mode": task.get("_ref_select_mode", ""),
        "_subtitle_path": task.get("_subtitle_path", ""),
        "skip_confirmation": task.get("skip_confirmation", True),
        "keep_bgm": task.get("keep_bgm", False),
        "type": task.get("type", "audio"),
    })
