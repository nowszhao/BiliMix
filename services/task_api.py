"""HTTP task commands. Model work is delegated to the application executor."""

import json
from functools import wraps
import os
import threading
import time
from flask import Blueprint, jsonify, request
from core import config
from core.processes import _kill_task_subprocesses
from core.database import save_task_to_index, get_db
from core.task_manager import (
    tasks,
    tasks_lock,
    cancel_flags,
    get_task,
    update_task,
    task_identity,
    restore_task_from_disk,
)
from core.task_queue import task_queue
from application import task_execution as execution
from pipeline.tts_references import validate_reference_mode
from pipeline.speaker_analysis import confirm_speakers, atomic_json
from pipeline.dubbing_plan import validate_manifest, file_sha256
from services.shared import generate_task_id

task_bp = Blueprint("task_commands", __name__)
_speaker_review_lock = threading.Lock()
_command_lock = threading.RLock()


def serialized_command(handler):
    @wraps(handler)
    def command(*args, **kwargs):
        with _command_lock:
            return handler(*args, **kwargs)

    return command


@task_bp.route("/api/parse-subtitle", methods=["POST"])
def parse_subtitle():
    """解析上传的双语字幕文件（ASS 格式，|| 分隔英文和中文）"""
    data = request.get_json() or {}
    subtitle_path = data.get("subtitle_path", "").strip()
    if not subtitle_path or not os.path.isfile(subtitle_path):
        return jsonify({"ok": False, "error": "文件不存在"}), 400

    from pipeline.subtitle_parser import parse_ass_subtitle

    segments, translations, translated_indices = parse_ass_subtitle(subtitle_path)

    bilingual_count = len(translated_indices)
    if not segments:
        return jsonify({"ok": False, "error": "未能解析出任何字幕内容"})

    return jsonify(
        {
            "ok": True,
            "count": len(segments),
            "bilingual_count": bilingual_count,
            "segments_preview": segments[:5],
        }
    )


@task_bp.route("/api/dubbing/<analysis_id>/confirm-speakers", methods=["POST"])
def confirm_source_speakers(analysis_id):
    """An explicit listening decision, separate from a translator's manifest."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    with _speaker_review_lock:
        task = get_task(analysis_id) or restore_task_from_disk(analysis_id)
        original = ((task or {}).get("result") or {}).get("analysis")
        if (
            not original
            or task.get("type") != "analysis"
            or task.get("status") != "completed"
        ):
            return jsonify({"error": "Completed source analysis required"}), 400
        try:
            revised = confirm_speakers(original, data.get("resolutions"))
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify({"error": str(exc)}), 400
        result = {**task["result"], "analysis": revised}
        message = (
            "人物确认完成，可提交精翻配音"
            if revised["dubbing_ready"]
            else f"已保存人物确认，仍有 {len(revised['unresolved_segments'])} 段待确认或参考不足"
        )
        folder = os.path.join(config.RESULT_DIR, "analysis_" + analysis_id)
        task_file = os.path.join(folder, "task_result.json")
        cache_file = os.path.join(folder, "speaker_analysis.json")
        # Disk is authoritative across restarts. Publish memory only after both
        # snapshots succeed; restore the previous task snapshot on write failure.
        with open(task_file, encoding="utf-8") as stream:
            previous = json.load(stream)
        try:
            atomic_json(task_file, {**previous, "result": result, "message": message})
            atomic_json(cache_file, revised)
        except OSError:
            atomic_json(task_file, previous)
            raise
        update_task(analysis_id, result=result, message=message)
        return jsonify({"analysis": revised, "dubbing_ready": revised["dubbing_ready"]})


@task_bp.route("/api/dubbing/analyze", methods=["POST"])
def submit_source_analysis():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    data["type"] = "analysis"
    return submit_task()


@task_bp.route("/api/submit", methods=["POST"])
def submit_task():
    """提交音频/视频处理任务"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请提供数据"}), 400

    task_type = data.get("type", "audio")  # "audio" | "video"
    try:
        reference_mode = validate_reference_mode(
            data.get("tts_reference_mode", "speaker")
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    local_path = data.get("local_path", "").strip()
    audio_url = data.get("url", "").strip()
    video_url = data.get("video_url", "").strip()

    # 视频任务
    if task_type in ("video", "analysis"):
        if not local_path and not video_url:
            return jsonify({"error": "请提供视频URL或上传本地视频文件"}), 400
        if local_path:
            if not os.path.isfile(local_path):
                return jsonify({"error": f"文件不存在: {local_path}"}), 400
            source = f"file://{local_path}"
        else:
            if not video_url.startswith(("http://", "https://")):
                return jsonify({"error": "请提供有效的HTTP/HTTPS URL"}), 400
            source = video_url

        subtitle_mode = data.get("subtitle_mode", "bilingual")
        subtitle_font_size = data.get("subtitle_font_size", -1)
    else:
        # 音频任务
        if not local_path and not audio_url:
            return jsonify({"error": "请提供音频URL或上传本地文件"}), 400
        if local_path:
            if not os.path.isfile(local_path):
                return jsonify({"error": f"文件不存在: {local_path}"}), 400
            source = f"file://{local_path}"
        elif not audio_url.startswith(("http://", "https://")):
            return jsonify({"error": "请提供有效的HTTP/HTTPS URL"}), 400
        else:
            source = audio_url
        subtitle_mode = "bilingual"
        subtitle_font_size = 20

    # Always use sentence_translate mode with 100% translation
    process_mode = (
        task_type if task_type in ("video", "analysis") else "sentence_translate"
    )
    print(f"[Submit] process_mode={process_mode!r} (task_type={task_type!r})")
    skip_confirmation = data.get(
        "skip_confirmation", getattr(config, "SKIP_CONFIRMATION", True)
    )
    title = data.get("title", "").strip()
    duration_str = data.get("duration", "").strip()

    # 解析预知时长：支持 "HH:MM:SS" / "MM:SS" / 纯秒数
    pre_duration = 0.0
    if duration_str:
        try:
            parts = [float(p) for p in duration_str.replace(",", "").split(":")]
            if len(parts) == 3:
                pre_duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                pre_duration = parts[0] * 60 + parts[1]
            else:
                pre_duration = float(duration_str)
        except ValueError:
            pass

    subtitle_path = data.get("subtitle_path", "")
    if subtitle_path and not os.path.isfile(subtitle_path):
        return jsonify({"error": "外部精翻字幕文件不存在，禁止回退到服务端翻译"}), 400

    analysis = None
    manifest = data.get("dub_manifest")
    analysis_id = data.get("analysis_id", "")
    if bool(subtitle_path) != (manifest is not None):
        return jsonify({"error": "外部精翻必须同时提供工作 ASS 和 dub_manifest"}), 400
    if manifest is not None:
        from pipeline.subtitle_parser import parse_ass_subtitle

        parsed, translations, indices = parse_ass_subtitle(subtitle_path)
        if not parsed or len(indices) != len(parsed):
            return jsonify({"error": "工作 ASS 必须包含完整的 EN || ZH 双语行"}), 400
        previous = get_task(analysis_id) or restore_task_from_disk(analysis_id)
        analysis = ((previous or {}).get("result") or {}).get("analysis")
        if not analysis or (previous or {}).get("status") != "completed":
            return jsonify({"error": "Completed source analysis_id required"}), 400
        if not local_path or file_sha256(local_path) != analysis["source_media_sha256"]:
            return jsonify({"error": "Source media hash disagrees with analysis"}), 400
        try:
            validate_manifest(manifest, analysis)
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "analysis_not_ready"
                    if not analysis.get("dubbing_ready", True)
                    else "invalid_manifest",
                    "unresolved_segments": analysis.get("unresolved_segments", []),
                    "missing_reference_speakers": analysis.get(
                        "missing_reference_speakers", []
                    ),
                }
            ), 400

    task_id = generate_task_id(source + str(time.time()))

    cancel_flags[task_id] = threading.Event()

    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    with tasks_lock:
        tasks[task_id] = {
            "task_id": task_id,
            "url": source,
            "title": title,
            "process_mode": process_mode,
            "type": task_type,
            "skip_confirmation": skip_confirmation,
            "tts_reference_mode": reference_mode,
            "status": "downloading",
            "step": "download",
            "progress": 0,
            "message": "任务已创建，准备下载..."
            if task_type != "video"
            else "准备下载视频...",
            "created_at": created_at,
            "original_duration": pre_duration,
            "transcription_text": "",
            "segments": [],
            "difficult_words": [],
            "replacements": [],
            "translations": {},
            "translated_indices": [],
            "result": None,
            "video_result": None,
            "_basename": task_id,
            "_audio_path": "",
            "_video_path": "",
            "_subtitle_mode": subtitle_mode,
            "_subtitle_font_size": subtitle_font_size,
            "keep_bgm": bool(data.get("keep_bgm", False)),
            "_subtitle_path": data.get("subtitle_path", ""),
            "_analysis_id": analysis_id,
            "_dub_manifest": manifest,
            "_speaker_analysis": analysis,
        }

    update_task(task_id, status="queued")
    execution.enqueue_task(task_id)

    return jsonify({"task_id": task_id, "message": "任务已提交"})


@task_bp.route("/api/task/<task_id>/cancel", methods=["POST"])
@serialized_command
def cancel_task(task_id):
    """终止正在运行的任务"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    status = task.get("status", "")
    if status in ("completed", "error", "cancelled"):
        return jsonify({"error": f"任务已是 {status} 状态，无需终止"}), 400

    event = cancel_flags.get(task_id)
    if event:
        event.set()

    _kill_task_subprocesses(task_id)

    update_task(task_id, status="cancelled", message="任务已被终止")

    task_queue.discard(task_id)

    return jsonify({"message": "任务终止请求已发送"})


@task_bp.route("/api/task/<task_id>/reorder", methods=["POST"])
@serialized_command
def reorder_queue(task_id):
    """调整排队中任务在队列中的顺序（上移/下移）"""
    data = request.get_json() or {}
    direction = data.get("direction", "up")

    try:
        moved = task_queue.move(task_id, direction)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with get_db() as conn:
        for index, tid in enumerate(task_queue.snapshot()):
            conn.execute(
                "UPDATE tasks SET queue_order = ? WHERE task_id = ?", (index, tid)
            )
    if not moved:
        return jsonify({"ok": True, "message": "已在边界"})

    return jsonify({"ok": True})


@task_bp.route("/api/task/<task_id>/confirm_sentences", methods=["POST"])
@serialized_command
def confirm_sentences(task_id):
    """用户确认翻译后，继续执行句子翻译模式的后续流程"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task.get("status") != "awaiting_sentence_confirmation":
        return jsonify(
            {"error": f"任务状态为 {task.get('status')}，不在等待句子确认状态"}
        ), 400

    if not task_queue.wait(task_id, timeout=2):
        return jsonify({"error": "上一阶段仍在退出，请稍后重试"}), 409

    data = request.get_json()
    if not data or "translations" not in data:
        return jsonify({"error": "请提供 translations"}), 400

    confirmed_translations = {int(k): v for k, v in data["translations"].items()}
    confirmed_indices = data.get("translated_indices", [])
    if confirmed_indices:
        confirmed_indices = [int(i) for i in confirmed_indices]
    else:
        confirmed_indices = sorted(confirmed_translations.keys())

    update_task(
        task_id,
        translations=confirmed_translations,
        translated_indices=confirmed_indices,
        status="queued",
        step="synthesize",
        progress=58,
        message=f"用户确认 {len(confirmed_translations)} 个翻译，继续处理...",
    )

    execution.enqueue_task(task_id, "confirm")
    return jsonify(
        {"message": f"已确认 {len(confirmed_translations)} 个翻译，继续处理"}
    )


@task_bp.route("/api/task/<task_id>/redo", methods=["POST"])
@serialized_command
def redo_task(task_id):
    """完整重做：清空所有处理结果，仅保留源文件，从头跑 pipeline"""
    task = get_task(task_id)
    if not task:
        task = restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    if task.get("status") not in (
        "completed",
        "error",
        "cancelled",
    ) or not task_queue.wait(task_id, timeout=2):
        return jsonify({"error": "只能重做已结束且退出执行队列的任务"}), 409

    source_url = task.get("url", "")
    task_type = task_identity(task)["type"]
    if not source_url:
        return jsonify({"error": "任务缺少源 URL，无法重做"}), 400

    basename = task.get("_basename", "")

    # ---- 1. 清理磁盘结果目录（保留源文件） ----
    if basename:
        result_dir = os.path.join(config.RESULT_DIR, basename)
        if os.path.isdir(result_dir):
            import shutil

            # 需要保留的：原始下载的音频/视频（由 _audio_path/_video_path 指向）
            # 删除所有生成物：TTS cache、人声分离缓存、sentence mp3、字幕、结果 JSON
            keep_paths = set()
            audio_path = task.get("_audio_path", "")
            video_path = task.get("_video_path", "")
            for p in (audio_path, video_path):
                if p and os.path.exists(p):
                    keep_paths.add(os.path.realpath(p))

            for name in os.listdir(result_dir):
                full = os.path.join(result_dir, name)
                real = os.path.realpath(full)
                if real not in keep_paths:
                    try:
                        if os.path.isdir(full):
                            shutil.rmtree(full)
                        else:
                            os.remove(full)
                    except Exception:
                        pass

    # ---- 2. 清空内存 task 中的所有处理状态 ----
    reset_fields = {
        "status": "queued",
        "step": "download",
        "progress": 0,
        "message": "正在重做...",
        "original_duration": 0,
        "transcription_text": "",
        "segments": [],
        "difficult_words": [],
        "replacements": [],
        "translations": {},
        "translated_indices": [],
        "sentence_pairs": [],
        "time_mapping": [],
        "segments_mixed": [],
        "tts_audio_map": {},
        "result": None,
        "video_result": None,
    }

    # 清理所有 checkpoint 和内部键
    checkpoint_keys = [
        "_checkpoint_translate_batch",
        "_checkpoint_translations",
        "_checkpoint_tts_idx",
        "_bgm_path",
        "_vocals_path",
        "_saved_result_path",
        "_tts_reference_manifest",
        "_reference_manifest",
    ]
    with tasks_lock:
        t = tasks.get(task_id, {})
        for k, v in reset_fields.items():
            t[k] = v
        for k in checkpoint_keys:
            t.pop(k, None)
        tasks[task_id] = t

    # ---- 3. 更新 SQLite index ----
    try:
        save_task_to_index(
            task_id,
            {
                "task_id": task_id,
                "url": source_url,
                "title": task.get("title", ""),
                "process_mode": task_identity(task)["process_mode"],
                "type": task_type,
                "status": "queued",
                "progress": 0,
                "message": "正在重做...",
                "created_at": task.get("created_at", ""),
                "basename": basename,
                "total_words": 0,
                "total_replacements": 0,
                "keep_bgm": task.get("keep_bgm", False),
                "tts_reference_mode": task.get("tts_reference_mode", "speaker"),
            },
        )
    except Exception:
        pass

    cancel_flags[task_id] = threading.Event()
    update_task(task_id, status="queued")
    execution.enqueue_task(task_id)
    return jsonify({"message": "重做已启动", "task_id": task_id})


@task_bp.route("/api/task/<task_id>/retry", methods=["POST"])
@serialized_command
def retry_task(task_id):
    """Resume the saved task without changing its media type or output contract."""
    task = get_task(task_id) or restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    if not task_queue.wait(task_id, timeout=2):
        return jsonify({"error": "任务仍在退出或已排队，请稍后查询"}), 409
    try:
        task = execution._load_retry_checkpoint(task_id, task)
    except (OSError, ValueError, TypeError) as exc:
        return jsonify({"error": "恢复任务断点失败: " + str(exc)}), 400
    can_refresh = (
        payload.get("refresh_analysis") is True
        and task["type"] == "analysis"
        and task.get("status") == "completed"
    )
    if (
        task.get("status") not in ("error", "cancelled") and not can_refresh
    ) or task_queue.position(task_id) >= 0:
        return jsonify(
            {
                "error": f"任务状态为 {task.get('status')}，仅 error/cancelled 且退出队列后可重试"
            }
        ), 400

    cancel_flags[task_id] = threading.Event()
    update_task(task_id, status="queued", progress=0, message="排队恢复原任务…")
    execution.enqueue_task(task_id, "resume")
    return jsonify(
        {"message": "已开始断点续传", "task_id": task_id, "type": task["type"]}
    )


@task_bp.route("/api/task/<task_id>/retry-synthesis", methods=["POST"])
@serialized_command
def retry_sentence_synthesis(task_id):
    """TTS 合成失败后手动重试"""
    task = get_task(task_id) or restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if not task_queue.wait(task_id, timeout=2):
        return jsonify({"error": "任务仍在退出或已排队，请稍后查询"}), 409
    try:
        task = execution._load_retry_checkpoint(task_id, task)
    except (OSError, ValueError, TypeError) as exc:
        return jsonify({"error": "恢复任务断点失败: " + str(exc)}), 400
    if task["type"] == "analysis":
        return jsonify({"error": "人物分析任务不能进入配音合成重试"}), 400

    status = task.get("status", "")
    if status not in ("error", "cancelled") or task_queue.position(task_id) >= 0:
        return jsonify(
            {"error": f"任务状态为 {status}，仅 error/cancelled 且退出队列后可重试合成"}
        ), 400

    segments = task.get("segments", [])
    translated_indices = task.get("translated_indices", [])
    translations = task.get("translations", {})
    translations = {int(k): v for k, v in translations.items()}
    audio_path = task.get("_audio_path", "")
    basename = task.get("_basename", "")

    if not segments or not translated_indices or not translations:
        return jsonify({"error": "任务数据不完整，无法重试"}), 400

    cancel_flags[task_id] = threading.Event()
    update_task(task_id, status="queued", progress=0, message="排队恢复配音…")
    execution.enqueue_task(task_id, "resume")
    return jsonify({"message": "已排队恢复配音", "task_id": task_id}), 202
