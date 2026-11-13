"""
Task Query Blueprint - list, status, result, delete endpoints.
"""
import os
import json
import shutil
import sys
import traceback

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Blueprint, jsonify, request
from core import config
from core.task_queue import task_queue
from core.task_manager import (
    tasks, tasks_lock, cancel_flags, task_subprocesses,
    get_task, is_cancelled, restore_task_from_disk,
    update_task,
)
from pipeline.media_cache import is_shared_media_path
from core.database import load_tasks_index, delete_task_from_index
from services.shared import _load_step_timing_from_disk, _load_video_result_from_disk
from core.processes import _kill_task_subprocesses

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
                "tts_reference_mode": task.get("tts_reference_mode", "speaker"),
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
                "queue_order": task_queue.position(tid),
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

    # 任务不在内存时（如服务重启后），从磁盘恢复以获得文件路径
    if not task:
        task = restore_task_from_disk(task_id)

    # Terminate if running
    if task and task.get("status") in ("downloading", "processing", "queued"):
        event = cancel_flags.get(task_id)
        if event:
            event.set()
        _kill_task_subprocesses(task_id)

    basename = ""
    audio_path = ""
    video_path = ""
    subtitle_path = ""
    if task:
        basename = task.get("_basename", "")
        audio_path = task.get("_audio_path", "")
        video_path = task.get("_video_path", "")
        subtitle_path = task.get("_subtitle_path", "")
        if not basename and task.get("result"):
            basename = task["result"].get("basename", "")
        # 本地 file:// 源可直接从 url 恢复视频/音频路径
        if not video_path:
            src = task.get("url", "")
            if src.startswith("file://"):
                candidate = src[len("file://"):]
                if os.path.isfile(candidate):
                    video_path = candidate
    if not basename and task_id in index:
        basename = index[task_id].get("basename", "")

    task_queue.discard(task_id)
    if not task_queue.wait(task_id, timeout=10):
        return jsonify({"error": "任务仍在停止中，请稍后重试删除"}), 409

    cleaned = []

    def _remove_downloads_file(path):
        """删除 data/downloads 下的关联文件（防止误删用户任意路径的文件）。

        返回是否实际删除了文件。
        """
        if not path:
            return False
        norm = os.path.normpath(path)
        dl_root = os.path.normpath(config.DOWNLOAD_DIR)
        if not (norm.startswith(dl_root + os.sep) or norm == dl_root):
            print(f"[Delete] 跳过删除（不在 downloads 目录）: {path}")
            return False
        if os.path.isfile(norm):
            os.remove(norm)
            cleaned.append(f"data/downloads/{os.path.relpath(norm, dl_root)}")
            return True
        return False

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
            # Shared analysis/dubbing media is content-addressed and must
            # outlive a single task deletion.
            if not is_shared_media_path(audio_path):
                _remove_downloads_file(audio_path)
        else:
            for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
                p = os.path.join(config.DOWNLOAD_DIR, f"{basename}{ext}")
                if os.path.exists(p):
                    os.remove(p)
                    cleaned.append(f"data/downloads/{basename}{ext}")
            # 视频任务：basename 带 taskid 后缀；旧的路径键音频缓存可清理，
            # 新的 _shared 内容缓存目录不会被这里的文件匹配命中。
            # 仅限视频任务，避免音频任务（如 audio_xxx.mp3）误删缓存
            if task and task.get("type") == "video":
                base_no_taskid = basename.rsplit("_", 1)[0]
                if base_no_taskid != basename:
                    vc_dir = os.path.join(config.DOWNLOAD_DIR, "video_cache")
                    if os.path.isdir(vc_dir):
                        for name in os.listdir(vc_dir):
                            if name.startswith(base_no_taskid):
                                p = os.path.join(vc_dir, name)
                                if os.path.isfile(p):
                                    os.remove(p)
                                    cleaned.append(f"data/downloads/video_cache/{name}")

    # 删除源视频文件（本地上传或 yt-dlp 下载，均位于 downloads 目录）
    if video_path:
        removed = _remove_downloads_file(video_path)
        if removed:
            # yt-dlp 会额外生成 <视频>.info.json 元数据
            info_path = video_path.rsplit(".", 1)[0] + ".info.json"
            if os.path.isfile(info_path):
                os.remove(info_path)
                rel = os.path.relpath(info_path, config.DOWNLOAD_DIR)
                cleaned.append(f"data/downloads/{rel}")

    # 删除用户上传的字幕文件（.ass / .srt）
    if subtitle_path:
        _remove_downloads_file(subtitle_path)

    # Remove from queue if waiting
    task_queue.discard(task_id)

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
        "tts_reference_mode": task.get("tts_reference_mode", "speaker"),
    })


# ── Task Result ─────────────────────────────────────────────


def _display_segments(segments, mixed_segments, translations, time_mapping=None):
    """One row per source segment with both the source and dubbed timelines.

    The dubbed player runs on the mixed timeline while the original player runs
    on source time. Returning both clocks together is what lets the console
    highlight and seek the right one instead of reusing source time for the
    compressed video. Segments that were never synthesized keep source time.
    """
    def number(value):
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return 0.0

    translated = {}
    for key, value in (translations or {}).items():
        try:
            translated[int(key)] = value
        except (TypeError, ValueError):
            continue
    mapped = set()
    for entry in time_mapping or []:
        if entry.get("type") != "tts_chinese":
            continue
        try:
            mapped.add(int(entry.get("segment_index")))
        except (TypeError, ValueError):
            continue
    rows = []
    for index in range(max(len(segments), len(mixed_segments))):
        source = segments[index] if index < len(segments) else {}
        mixed = mixed_segments[index] if index < len(mixed_segments) else {}
        start_orig = number(source.get("start"))
        end_orig = number(source.get("end"))
        has_mixed = (index in mapped and mixed.get("start") is not None
                     and mixed.get("end") is not None)
        start_mixed = number(mixed.get("start")) if has_mixed else None
        end_mixed = number(mixed.get("end")) if has_mixed else None
        rows.append({
            "index": index,
            "unit_id": source.get("unit_id") or mixed.get("unit_id") or "",
            "speaker": source.get("speaker") or mixed.get("speaker") or "",
            "text": (source.get("text") or "").strip(),
            "chinese": translated.get(index, mixed.get("chinese") or ""),
            "dubbed": has_mixed,
            "start_orig": start_orig,
            "end_orig": end_orig,
            "start_mixed": start_mixed,
            "end_mixed": end_mixed,
            # Default playback clock for the dubbed output. Clients must read
            # the explicit *_orig/*_mixed fields when switching sources.
            "start": start_mixed if has_mixed else start_orig,
            "end": end_mixed if has_mixed else end_orig,
        })
    return rows


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
    display_segments = _display_segments(segments, mixed_segments, translations, tmap)
    video_result = task.get("video_result") or _load_video_result_from_disk(task)
    if video_result and video_result.get("subtitle_path"):
        layout_path = video_result["subtitle_path"]+".layout.json"
        if os.path.isfile(layout_path):
            try:
                with open(layout_path, encoding="utf-8") as stream:
                    layout = json.load(stream)
                video_result = {**video_result, "subtitle_layout":{
                    k:v for k,v in layout.items() if k != "pages"}}
            except (OSError, ValueError):
                video_result = {**video_result, "subtitle_layout":{"status":"INVALID"}}
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
        # Full transcript, with explicit source and dubbed clocks. The web
        # console must not truncate here: a 20-row cap silently kills subtitle
        # sync for every long video.
        "display_segments": display_segments,
        "segments": segments,
        "segments_mixed": mixed_segments,
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
        "_subtitle_path": task.get("_subtitle_path", ""),
        "dubbing_protocol": 1,
        "analysis_id": task.get("_analysis_id", ""),
        "reference_manifest": task.get("_reference_manifest", {}),
        "tts_reference_mode": task.get("tts_reference_mode", "speaker"),
        "tts_reference_manifest": task.get("_tts_reference_manifest", {}),
        "dub_manifest": task.get("_dub_manifest"),
        "skip_confirmation": task.get("skip_confirmation", True),
        "keep_bgm": task.get("keep_bgm", False),
        "type": task.get("type", "audio"),
    })
