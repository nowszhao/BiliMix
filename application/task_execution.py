"""Schedule and recover media tasks through one execution path."""

import hashlib
import os
import traceback
from core import config
from core.task_queue import task_queue
from core.task_manager import (
    get_task,
    update_task,
    is_cancelled,
    save_task_result_to_disk,
    task_identity,
    task_subprocesses,
    read_task_checkpoint,
)
from core.database import save_task_to_index
from pipeline.media_io import (
    _probe_audio_duration,
    download_audio,
    _try_resolve_local_url,
)
from pipeline.media_cache import extracted_audio_dir
from pipeline.step0_video_prepare import prepare_video
from application import dubbing, source_analysis


def _prepare_audio(task_id, source_url):
    """准备音频文件（下载或使用本地），返回音频路径。"""
    is_file_url = source_url.startswith("file://")
    if is_file_url:
        local_path = source_url[len("file://") :]
        if os.path.isfile(local_path):
            local_file = local_path
        else:
            update_task(task_id, status="error", message=f"文件不存在: {local_path}")
            return None
    else:
        local_file = _try_resolve_local_url(source_url)

    if local_file:
        basename = os.path.splitext(os.path.basename(local_file))[0]
        audio_path = local_file
        update_task(
            task_id,
            _basename=get_task(task_id).get("_basename") or basename,
            _audio_path=audio_path,
        )
        size_mb = os.path.getsize(local_file) / (1024 * 1024)
        update_task(task_id, progress=5, message=f"使用本地音频文件 ({size_mb:.1f} MB)")
        # 探测本地音频时长并立即更新到任务，便于任务列表展示
        try:
            duration = _probe_audio_duration(local_file)
            if duration > 0:
                update_task(task_id, original_duration=duration)
        except Exception:
            pass
    else:
        basename = hashlib.md5(source_url.encode()).hexdigest()
        ext = os.path.splitext(source_url.split("?")[0])[-1] or ".mp3"
        if ext not in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
            ext = ".mp3"
        audio_path = os.path.join(config.DOWNLOAD_DIR, f"{basename}{ext}")
        update_task(
            task_id,
            _basename=get_task(task_id).get("_basename") or basename,
            _audio_path=audio_path,
        )

        if os.path.exists(audio_path):
            update_task(task_id, progress=5, message="文件已存在，跳过下载")
        else:
            if not download_audio(source_url, audio_path, task_id):
                return None
    return audio_path


def run_source_task(task_id):
    task = get_task(task_id)
    source_url = task["url"]
    task_type = task["type"]
    title = task.get("title", "")
    process_mode = task["process_mode"]
    # ---- 视频任务：先下载/提取视频 ----
    if task_type in ("video", "analysis"):
        is_local = source_url.startswith("file://")
        if is_local:
            local_path = source_url[len("file://") :]
            if not os.path.isfile(local_path):
                update_task(
                    task_id, status="error", message=f"文件不存在: {local_path}"
                )
                return
            video_source = local_path
        else:
            video_source = source_url

        # 视频缓存目录
        video_cache_dir = os.path.join(config.DOWNLOAD_DIR, "video_cache")
        os.makedirs(video_cache_dir, exist_ok=True)

        update_task(
            task_id, step="download", progress=1, message="正在下载/提取视频..."
        )
        prep = prepare_video(
            video_source,
            video_cache_dir,
            title=title,
            audio_cache_dir=extracted_audio_dir(),
        )
        if not prep.get("ok"):
            update_task(
                task_id,
                status="error",
                message=f"视频准备失败: {prep.get('error', '未知错误')}",
            )
            return

        video_path = prep["video_path"]
        audio_file = prep["audio_path"]
        real_title = prep.get("title", title) or os.path.basename(video_path)
        update_task(
            task_id,
            _video_path=video_path,
            _basename=get_task(task_id).get("_basename") or task_id,
            _audio_path=audio_file,
            _media_sha256=prep.get("media_sha256", ""),
            title=real_title,
            progress=5,
            message=f"视频就绪: {os.path.basename(video_path)}",
        )
        # 探测从视频中提取的音频时长
        try:
            duration = _probe_audio_duration(audio_file)
            if duration > 0:
                update_task(task_id, original_duration=duration)
        except Exception:
            pass

        # 更新任务标题到数据库
        try:
            save_task_to_index(
                task_id,
                {
                    "task_id": task_id,
                    "url": source_url,
                    "title": real_title,
                    "process_mode": process_mode,
                    "type": task_type,
                    "status": "processing",
                    "progress": 5,
                    "message": "视频准备完成",
                },
            )
        except Exception:
            pass

        # 重新从 task 读取更新后的值
        current_task = get_task(task_id)
        if not current_task:
            return
        audio_path_for_pipeline = audio_file
    else:
        audio_path_for_pipeline = _prepare_audio(task_id, source_url)
        if not audio_path_for_pipeline:
            return

    if task_type == "analysis":
        source_analysis._process_source_analysis(
            task_id,
            audio_path_for_pipeline,
            video_source if os.path.isfile(video_source) else video_path,
        )
        return

    # 执行核心管道（转录 → 翻译 → TTS → 混音）
    dubbing.process_audio_sentence_mode(task_id, audio_path_for_pipeline)

    # ---- 视频任务：追加字幕和组装 ----
    current = get_task(task_id)
    if task_type == "video" and current.get("status") not in (
        "error",
        "cancelled",
        "awaiting_sentence_confirmation",
        "awaiting_confirmation",
    ):
        dubbing._run_video_post_process(task_id)


def _load_retry_checkpoint(task_id, task):
    basename = task.get("_basename", "")
    saved = read_task_checkpoint(task_id, basename) if basename else {}
    recovered = {
        key: value
        for key, value in saved.items()
        if key not in task or task[key] is None
    }
    recovered.update(task_identity(task, saved))
    update_task(task_id, **recovered)
    return get_task(task_id)


def _prepare_retry_input(task_id):
    """Recover the source for its original task kind, inside the queue."""
    task = get_task(task_id)
    identity = task_identity(task)
    update_task(task_id, **identity)
    source_url = task.get("url", "")
    audio = task.get("_audio_path", "")
    if identity["type"] in ("video", "analysis"):
        video = task.get("_video_path", "")
        if not video or not os.path.isfile(video):
            if source_url.startswith("file://"):
                video = source_url[len("file://") :]
                if not os.path.isfile(video):
                    raise FileNotFoundError("原视频文件缺失，无法按视频任务重试")
            elif source_url.startswith(("http://", "https://")):
                video = source_url
            else:
                raise FileNotFoundError("任务缺少原视频来源，不能降级为音频任务重试")
        if os.path.isfile(video) and audio and audio != video and os.path.isfile(audio):
            update_task(task_id, _video_path=video)
            return audio
        cache = os.path.join(config.DOWNLOAD_DIR, "video_cache")
        prep = prepare_video(
            video,
            cache,
            title=task.get("title", ""),
            audio_cache_dir=extracted_audio_dir(),
        )
        if not prep.get("ok"):
            raise ValueError(
                "重试准备原视频失败: " + prep.get("error", "unknown error")
            )
        video, audio = prep.get("video_path"), prep.get("audio_path")
        if (
            not video
            or not audio
            or not os.path.isfile(video)
            or not os.path.isfile(audio)
        ):
            raise FileNotFoundError("重试缺少原视频或提取音频")
        updates = dict(
            _video_path=video,
            _audio_path=audio,
            _media_sha256=prep.get("media_sha256", ""),
        )
        if not task.get("_basename"):
            updates["_basename"] = (
                os.path.splitext(os.path.basename(audio))[0] + "_" + task_id[:8]
            )
        update_task(task_id, **updates)
        return audio
    if audio and os.path.isfile(audio):
        return audio
    if source_url.startswith("file://"):
        audio = source_url[len("file://") :]
        if not os.path.isfile(audio):
            raise FileNotFoundError("原音频文件缺失")
    elif source_url.startswith(("http://", "https://")):
        extension = os.path.splitext(source_url.split("?")[0])[-1].lower()
        if extension not in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
            extension = ".mp3"
        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
        audio = os.path.join(config.DOWNLOAD_DIR, task_id + ".retry" + extension)
        if not download_audio(source_url, audio, task_id):
            if is_cancelled(task_id):
                raise InterruptedError("重试已取消")
            raise ValueError("重新下载音频失败")
    else:
        raise FileNotFoundError("任务缺少原音频来源")
    update_task(task_id, _audio_path=audio)
    return audio


def _resume_task(task_id):
    audio = _prepare_retry_input(task_id)
    task = get_task(task_id)
    if task["type"] == "analysis":
        return source_analysis._process_source_analysis(
            task_id, audio, task["_video_path"]
        )
    if (
        task.get("segments")
        and task.get("translations")
        and task.get("translated_indices")
    ):
        return dubbing._synthesis_resume(task_id)
    dubbing.process_audio_sentence_mode(task_id, audio)
    current = get_task(task_id)
    if current.get("type") == "video" and current.get("status") not in (
        "error",
        "cancelled",
        "awaiting_sentence_confirmation",
        "awaiting_confirmation",
    ):
        return dubbing._run_video_post_process(task_id)


def enqueue_task(task_id, operation="start"):
    actions = {
        "start": run_source_task,
        "resume": _resume_task,
        "confirm": continue_confirmed,
    }
    action = actions[operation]

    def execute():
        if is_cancelled(task_id) or not get_task(task_id):
            return
        update_task(task_id, status="processing", message="开始处理…")
        try:
            action(task_id)
        except Exception as exc:
            traceback.print_exc()
            update_task(
                task_id,
                status="cancelled" if is_cancelled(task_id) else "error",
                message=str(exc),
            )
            current = get_task(task_id)
            if current.get("_basename"):
                save_task_result_to_disk(
                    os.path.join(config.RESULT_DIR, current["_basename"]), current
                )
        finally:
            task_subprocesses.pop(task_id, None)

    task_queue.submit(task_id, execute)


def continue_confirmed(task_id):
    dubbing.continue_after_sentence_confirmation(task_id)
    task = get_task(task_id)
    if task.get("type") == "video" and task.get("status") not in ("error", "cancelled"):
        dubbing._run_video_post_process(task_id)
