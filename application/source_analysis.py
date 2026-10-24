"""Source-speaker analysis, isolated from translation and synthesis."""

import json
import os
from core import config
from core.task_manager import (
    save_task_result_to_disk,
    update_task,
    get_task,
    is_cancelled,
)
from pipeline.speaker_analysis import analyze_source
from pipeline.media_cache import vocal_cache_for_task
from pipeline.step_vocal_separation import separate_vocals


def _process_source_analysis(task_id, audio_path, media_path):
    """Shared initial/retry path. Analysis never enters translation or synthesis."""
    basename = get_task(task_id).get("_basename") or "analysis_" + task_id
    result_dir = os.path.join(config.RESULT_DIR, basename)
    os.makedirs(result_dir, exist_ok=True)
    update_task(
        task_id,
        step="separate",
        progress=5,
        message="正在准备原声分析音频...",
        _basename=basename,
        _audio_path=audio_path,
        _video_path=media_path,
        url=(get_task(task_id) or {}).get("url") or "file://" + media_path,
    )
    try:
        task = get_task(task_id) or {}
        sep = separate_vocals(
            audio_path,
            vocal_cache_for_task(task, os.path.join(result_dir, "vocals")),
        )
        source_audio = sep.get("vocals_path") if sep.get("ok") else audio_path
        analysis = analyze_source(
            source_audio,
            media_path,
            result_dir,
            task_id=task_id,
            media_sha256=task.get("_media_sha256") or None,
        )
        if is_cancelled(task_id):
            update_task(task_id, status="cancelled")
            return
        analysis["analysis_id"] = task_id
        result = {
            "analysis": analysis,
            "original_audio": audio_path,
            "basename": basename,
        }
        completion = dict(
            status="completed",
            step="done",
            progress=100,
            result=result,
            message=(
                "人物分析完成，可进行精翻与配音"
                if analysis["dubbing_ready"]
                else f"人物分析完成：{len(analysis['unresolved_segments'])} 段待确认，{len(analysis['missing_reference_speakers'])} 个人物缺少合格参考；请先复核再配音"
            ),
        )
        save_task_result_to_disk(result_dir, {**get_task(task_id), **completion})
        update_task(task_id, **completion)
    except Exception as exc:
        checkpoint = os.path.join(result_dir, "speaker_analysis.json")
        if os.path.isfile(checkpoint):
            with open(checkpoint, encoding="utf-8") as stream:
                partial = json.load(stream)
            partial["analysis_id"] = task_id
            update_task(
                task_id,
                result={
                    "analysis": partial,
                    "original_audio": audio_path,
                    "basename": basename,
                },
            )
        update_task(
            task_id,
            status="cancelled" if is_cancelled(task_id) else "error",
            message=str(exc),
        )
        save_task_result_to_disk(result_dir, dict(get_task(task_id)))
        raise
