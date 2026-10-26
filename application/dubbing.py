"""Complete-utterance dubbing and video assembly, shared by initial and resumed tasks."""

import os
import threading
import traceback
from core import config
from core.task_manager import (
    cancel_flags,
    task_subprocesses,
    save_task_result_to_disk,
    update_task,
    get_task,
    is_cancelled,
    task_identity,
)
from core.database import update_episode_status_by_task
from core.retry import _run_with_retry
from pipeline.media_io import _cleanup_intermediate_files
from pipeline.media_cache import vocal_cache_for_task
from pipeline.tts_references import build_reference_map, validate_reference_mode
from pipeline.speaker_analysis import (
    analyze_source,
    extract_reference_pool,
    ensure_dubbing_ready,
)
from pipeline.dubbing_plan import validate_manifest
from pipeline.step3_tts_confucius import synthesize_sentences_with_confucius_tts
from pipeline.step2b_translate_sentences import (
    select_sentences_to_translate,
    translate_sentences,
)
from pipeline.step4b_sentence_mixer import (
    mix_sentence_audio,
    build_segments_with_mixed_time,
)
from pipeline.step_vocal_separation import separate_vocals
from pipeline.step5_video_assemble import (
    generate_bilingual_ass,
    assemble_video,
    _probe_video_size,
)


def process_audio_sentence_mode(task_id: str, audio_path: str):
    """句子翻译模式前半段：转录 → 翻译 → 暂停等待确认"""
    try:
        basename = os.path.splitext(os.path.basename(audio_path))[0]
        # 追加 task_id 短前缀，确保同一文件/URL 被多次处理时 result_dir 唯一
        basename = get_task(task_id).get("_basename") or f"{basename}_{task_id[:8]}"
        result_dir = os.path.join(config.RESULT_DIR, basename)
        os.makedirs(result_dir, exist_ok=True)
        update_task(task_id, _basename=basename, _audio_path=audio_path)

        # ---- Step 0: 人声/背景音分离（始终执行）----
        # 无论 keep_bgm 为何值，都需要分离纯人声用于 TTS 参考音频。
        # keep_bgm 仅控制最终混音时是否叠加 BGM 轨。
        task_pre = get_task(task_id) or {}
        keep_bgm = task_pre.get("keep_bgm", False)
        transcribe_audio_path = audio_path
        vocals_path = ""
        bgm_path = None

        update_task(
            task_id,
            status="processing",
            step="separate",
            progress=3,
            message="正在分离人声与背景音...",
        )
        sep_cache_dir = vocal_cache_for_task(
            task_pre, os.path.join(result_dir, "vocal_separation")
        )
        sep_result = separate_vocals(audio_path, sep_cache_dir)
        if sep_result.get("ok"):
            vocals_path = sep_result.get("vocals_path", "")
            bgm_path = sep_result.get("no_vocals_path", "")
            if vocals_path and os.path.exists(vocals_path):
                transcribe_audio_path = vocals_path
                print(f"[VocalSep] 分离成功，转录将使用纯人声音频: {vocals_path}")
            update_task(task_id, _vocals_path=vocals_path)
            if keep_bgm and bgm_path:
                update_task(task_id, _bgm_path=bgm_path)
        else:
            print(f"[VocalSep] 分离失败，转录使用原始音频: {sep_result.get('error')}")

        # ---- Step 1 & 2: 转录 + 翻译（可被外部字幕跳过） ----
        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        # 检查是否有外部字幕文件
        subtitle_path = task_pre.get("_subtitle_path", "")
        external_subtitle_used = False

        if task_pre.get("_dub_manifest") is not None or subtitle_path:
            manifest = task_pre.get("_dub_manifest")
            analysis = task_pre.get("_speaker_analysis")
            if not manifest or not analysis or not os.path.isfile(subtitle_path):
                raise ValueError(
                    "外部精翻需要工作 ASS、dub_manifest 和已完成的人物分析；禁止回退到服务端翻译"
                )
            segments = validate_manifest(manifest, analysis)
            translations = {i: unit["text_zh"] for i, unit in enumerate(segments)}
            translated_indices = list(translations)
            full_text = " ".join(segment["text"] for segment in segments)
            external_subtitle_used = True
            update_task(
                task_id,
                status="processing",
                step="synthesize",
                progress=30,
                message="使用精翻话语合成中文配音...",
                segments=segments,
                translations=translations,
                translated_indices=translated_indices,
                transcription_text=full_text,
            )
            save_task_result_to_disk(
                result_dir,
                {
                    "task_id": task_id,
                    "status": "processing",
                    "process_mode": "sentence_translate",
                    "transcription_text": full_text,
                    "segments": segments,
                    "translations": translations,
                    "translated_indices": translated_indices,
                },
            )

        if not external_subtitle_used:
            # ---- Step 1: 转录 ----
            update_task(
                task_id,
                status="processing",
                step="transcribe",
                progress=5,
                message="Step 1/4: 正在转录音频...",
            )

            media_source = task_pre.get("url", "").removeprefix("file://")
            if not os.path.isfile(media_source):
                media_source = task_pre.get("_video_path") or audio_path
            analysis = analyze_source(
                transcribe_audio_path,
                media_source,
                os.path.join(result_dir, "source_analysis"),
                task_id=task_id,
            )
            ensure_dubbing_ready(analysis)
            segments = analysis["sentences"]
            full_text = " ".join(s["text"] for s in segments)
            update_task(task_id, _speaker_analysis=analysis)

        if not external_subtitle_used:
            if is_cancelled(task_id):
                raise InterruptedError("任务已被用户终止")

            serialized_segments = [
                {
                    "text": s.get("text", "").strip(),
                    "start": s.get("start", 0),
                    "end": s.get("end", 0),
                    "speaker": s.get("speaker", ""),
                }
                for s in segments
            ]
            update_task(
                task_id,
                progress=20,
                message=f"转录完成: {len(segments)} 个句子",
                transcription_text=full_text,
                segments=serialized_segments,
            )

            # ---- Step 2: 选择并翻译句子 ----
            if is_cancelled(task_id):
                raise InterruptedError("任务已被用户终止")
            update_task(
                task_id,
                step="translate",
                progress=25,
                message="Step 2/4: 正在翻译句子...",
            )

            ratio = getattr(config, "SENTENCE_CN_RATIO", 1.0)
            translated_indices = select_sentences_to_translate(
                serialized_segments, ratio
            )

            if not translated_indices:
                update_task(
                    task_id,
                    status="completed",
                    progress=100,
                    step="done",
                    message="没有需要翻译的句子。",
                )
                return

            update_task(
                task_id,
                progress=30,
                message=f"将翻译 {len(translated_indices)}/{len(segments)} 个句子 "
                f"(比例: {ratio * 100:.0f}%)",
            )

            def _translate_progress(batch_idx, total_batches):
                task_cur = get_task(task_id)
                if task_cur and task_cur.get("step") != "translate":
                    return
                pct = 30 + int((batch_idx / max(total_batches, 1)) * 25)
                update_task(
                    task_id,
                    step="translate",
                    progress=pct,
                    message=f"Step 2/4: 翻译批次 ({batch_idx + 1}/{total_batches})",
                )

            def _cancel_check():
                return is_cancelled(task_id)

            def _translate_checkpoint(batch_idx, trans):
                """每批翻译完成后更新内存断点 + 落盘到 task_result.json"""
                update_task(
                    task_id,
                    _checkpoint_translate_batch=batch_idx,
                    _checkpoint_translations=trans,
                )
                save_task_result_to_disk(
                    result_dir,
                    {
                        "task_id": task_id,
                        "status": "processing",
                        "process_mode": "sentence_translate",
                        "_checkpoint_translate_batch": batch_idx,
                        "_checkpoint_translations": trans,
                    },
                )

            _task = get_task(task_id)
            resume_tl_batch = int(_task.get("_checkpoint_translate_batch", 0))
            resume_tl_trans = _task.get("_checkpoint_translations", None)
            if resume_tl_trans:
                resume_tl_trans = {int(k): v for k, v in resume_tl_trans.items()}

            translations = _run_with_retry(
                translate_sentences,
                serialized_segments,
                translated_indices,
                cancel_check=_cancel_check,
                progress_cb=_translate_progress,
                resume_batch=resume_tl_batch,
                existing_translations=resume_tl_trans,
                checkpoint_cb=_translate_checkpoint,
                name="句子翻译",
            )

            update_task(
                task_id, _checkpoint_translate_batch=0, _checkpoint_translations=None
            )

            if is_cancelled(task_id):
                raise InterruptedError("任务已被用户终止")
            update_task(
                task_id,
                progress=55,
                message=f"翻译完成: {len(translations)}/{len(translated_indices)} 个句子",
            )

            # 翻译结果立刻落盘，kill 后断点续传能跳过翻译步骤
            save_task_result_to_disk(
                result_dir,
                {
                    "task_id": task_id,
                    "status": "processing",
                    "process_mode": "sentence_translate",
                    "transcription_text": full_text,
                    "segments": serialized_segments,
                    "translations": translations,
                    "translated_indices": translated_indices,
                },
            )

        # ---- 确认翻译环节 ----
        task = get_task(task_id)
        skip_confirm = task.get(
            "skip_confirmation", getattr(config, "SKIP_CONFIRMATION", True)
        )

        if skip_confirm:
            print(f"[Sentence] 任务 {task_id[:8]}... 跳过确认，自动继续处理")
            update_task(
                task_id,
                translations=translations,
                translated_indices=translated_indices,
                _raw_segments=segments,
                status="processing",
                step="synthesize",
                progress=58,
                message=f"自动确认 {len(translations)} 个翻译，继续处理...",
            )
            continue_after_sentence_confirmation(task_id)
        else:
            update_task(
                task_id,
                status="awaiting_sentence_confirmation",
                step="confirm_sentence",
                progress=55,
                message=f"已翻译 {len(translations)} 个句子，请确认后继续",
                translations=translations,
                translated_indices=translated_indices,
                _raw_segments=segments,
            )
            save_task_result_to_disk(
                result_dir,
                {
                    "task_id": task_id,
                    "status": "awaiting_sentence_confirmation",
                    "process_mode": "sentence_translate",
                    "transcription_text": full_text,
                    "segments": serialized_segments,
                    "translations": translations,
                    "translated_indices": translated_indices,
                },
            )
            print(f"[Sentence] 任务 {task_id[:8]}... 暂停等待用户确认翻译")

    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已被终止")
    except Exception as e:
        traceback.print_exc()
        task = get_task(task_id) or {}
        update_task(
            task_id,
            status="error",
            message=f"处理出错: {str(e)}",
            _failed_step=task.get("step", "translate"),
        )
    finally:
        task_subprocesses.pop(task_id, None)


def _ensure_vocals_for_tts(task_id: str) -> str:
    """
    确保存在纯人声音频用于 TTS 参考音频提取。

    无论 keep_bgm 为何值、无论任务是否续跑，TTS 声音克隆都需要
    纯人声参考音频，否则原始音频中的 BGM 会被 Confucius4-TTS
    的声音克隆学进去。

    若已有 _vocals_path 则直接复用；否则执行 demucs 分离。

    Returns:
        str: vocals.wav 路径（纯人声），失败时返回原始 audio_path
    """
    task = get_task(task_id)
    if not task:
        return ""

    audio_path = task.get("_audio_path", "")
    if not audio_path:
        return ""

    # 已有缓存直接复用
    vocals_path = task.get("_vocals_path", "")
    if vocals_path and os.path.exists(vocals_path):
        return vocals_path

    # 执行 demucs 分离
    basename = task.get("_basename", "")
    result_dir = os.path.join(config.RESULT_DIR, basename)
    sep_cache_dir = vocal_cache_for_task(
        task, os.path.join(result_dir, "vocal_separation")
    )

    print(
        f"[VocalSep] keep_bgm={task.get('keep_bgm', False)}，"
        f"vocals_path 缺失，执行 demucs 分离..."
    )
    sep_result = separate_vocals(audio_path, sep_cache_dir)
    if sep_result.get("ok"):
        vocals_path = sep_result.get("vocals_path", "")
        update_task(task_id, _vocals_path=vocals_path)
        print(f"[VocalSep] 分离成功: vocals={vocals_path}")
        # 如果 keep_bgm=True，同时保存 bgm_path 用于最终混音
        if task.get("keep_bgm", False):
            bgm_path = sep_result.get("no_vocals_path", "")
            if bgm_path:
                update_task(task_id, _bgm_path=bgm_path)
        return vocals_path

    print(
        f"[VocalSep] 分离失败: {sep_result.get('error', '未知错误')}，"
        f"回退到原始音频（可能含 BGM）"
    )
    return audio_path


def _build_confucius_ref_map(
    task_id: str, segments: list, translated_indices: list
) -> dict:
    """
    为 Confucius4-TTS 构建参考音频映射。

    先从 _ensure_vocals_for_tts 获取纯人声，再调用参考音频提取。
    """
    task = get_task(task_id)
    if not task:
        return {}

    voice_clone = getattr(config, "SENTENCE_TTS_VOICE_CLONE", True)
    if not voice_clone:
        return {}

    ref_audio_source = _ensure_vocals_for_tts(task_id)
    if not ref_audio_source:
        return {}

    basename = task.get("_basename", "")
    result_dir = os.path.join(config.RESULT_DIR, basename)
    confucius_ref_dir = os.path.join(result_dir, "tts_confucius_cache", "ref_audio")
    os.makedirs(confucius_ref_dir, exist_ok=True)

    analysis = task.get("_speaker_analysis")
    if not analysis:
        raise ValueError("Source speaker analysis required for voice cloning")
    ensure_dubbing_ready(analysis)
    pool = extract_reference_pool(ref_audio_source, analysis, confucius_ref_dir)
    missing = {
        segments[i].get("speaker")
        for i in translated_indices
        if segments[i].get("speaker") not in pool
    }
    if missing:
        raise ValueError(
            f"Insufficient clean same-speaker reference: {sorted(missing)}"
        )
    mode = validate_reference_mode(task.get("tts_reference_mode", "speaker"))
    ref_map, report = build_reference_map(
        ref_audio_source,
        analysis,
        segments,
        translated_indices,
        pool,
        confucius_ref_dir,
        mode,
    )
    update_task(task_id, _reference_manifest=pool, _tts_reference_manifest=report)
    save_task_result_to_disk(result_dir, get_task(task_id))
    for unit in report["units"]:
        if unit["fallback_reason"]:
            print(
                f"[TTSRef] unit={unit['unit_id'] or unit['segment_index']} "
                f"speaker={unit['speaker']} fallback={unit['fallback_reason']}"
            )
    return ref_map


def continue_after_sentence_confirmation(task_id: str):
    """句子翻译模式后半段：TTS 合成 → 音频组装 → 完成"""
    try:
        task = get_task(task_id)
        if not task:
            return

        audio_path = task.get("_audio_path", "")
        basename = task.get("_basename", "")
        translations = task.get("translations", {})
        translated_indices = task.get("translated_indices", [])
        # 优先使用完整 _raw_segments（含 speaker 等字段，声音克隆需 speaker 做说话人匹配）；
        # _raw_segments 缺失时（如断点续传从磁盘恢复）回退到精简 segments
        segments = task.get("_raw_segments") or task.get("segments", [])

        full_text = task.get("transcription_text", "")
        result_dir = os.path.join(config.RESULT_DIR, basename)
        os.makedirs(result_dir, exist_ok=True)

        # 确保 TTS 参考音频使用纯人声（不受 keep_bgm 开关影响）
        ref_audio_source = _ensure_vocals_for_tts(task_id) or audio_path

        translations = {int(k): v for k, v in translations.items()}
        translated_indices = sorted(
            [idx for idx in translated_indices if idx in translations]
        )

        # 翻译已就绪，落盘以便 kill 后断点续传跳过翻译步骤
        mode = "sentence_translate"
        save_task_result_to_disk(
            result_dir,
            {
                "task_id": task_id,
                "status": "processing",
                "process_mode": "sentence_translate",
                "transcription_text": full_text,
                "segments": segments,
                "difficult_words": task.get("difficult_words", []),
                "translations": translations,
                "translated_indices": translated_indices,
            },
        )

        if not translations:
            update_task(
                task_id,
                status="completed",
                progress=100,
                step="done",
                message="没有翻译内容，已完成。",
            )
            return

        if task_id not in cancel_flags:
            cancel_flags[task_id] = threading.Event()

        # ---- Step 3: TTS 合成 ----
        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        voice_clone = getattr(config, "SENTENCE_TTS_VOICE_CLONE", True)
        skip_tts = getattr(config, "SKIP_TTS", False)

        update_task(
            task_id,
            status="processing",
            step="synthesize",
            progress=60,
            message="Step 3/4: 合成中文语音..."
            if not skip_tts
            else "跳过 TTS，使用原声",
        )

        tts_audio_map = {}

        if skip_tts:
            print(f"[TTS] SKIP_TTS=True，跳过语音合成")
        else:
            # Confucius4-TTS-CPU: 零样本多语言声音克隆
            confucius_cache_dir = os.path.join(result_dir, "tts_confucius_cache")
            confucius_ref_dir = os.path.join(confucius_cache_dir, "ref_audio")
            os.makedirs(confucius_ref_dir, exist_ok=True)

            pseudo_replacements = [
                {"segment_index": idx}
                for idx in translated_indices
                if idx < len(segments)
            ]
            confucius_ref_map = {}
            if voice_clone and pseudo_replacements:
                confucius_ref_map = _build_confucius_ref_map(
                    task_id, segments, translated_indices
                )

            def _confucius_progress(current, total):
                # 防止 TTS 完成后回调仍然修改 message 导致 step/message 不同步
                task_cur = get_task(task_id)
                if task_cur and task_cur.get("step") != "synthesize":
                    return
                pct = 60 + int((current / max(total, 1)) * 20)
                update_task(
                    task_id,
                    step="synthesize",
                    progress=pct,
                    message=f"Step 3/4: Confucius4-TTS 句子合成 ({current}/{total})",
                    _tts_completed_count=current,
                )

            def _confucius_cancel():
                return is_cancelled(task_id)

            tts_audio_map = synthesize_sentences_with_confucius_tts(
                segments,
                translated_indices,
                translations,
                audio_path,
                confucius_cache_dir,
                ref_audio_map=confucius_ref_map if voice_clone else {},
                reference_mode=task.get("tts_reference_mode", "speaker"),
                cancel_check=_confucius_cancel,
                progress_cb=_confucius_progress,
                task_id=task_id,
            )

        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        if len(translated_indices) > 0 and not tts_audio_map:
            if getattr(config, "SKIP_TTS", False):
                print(f"[TTS] SKIP_TTS=True，跳过 TTS，使用原始音频继续")
            else:
                raise RuntimeError(
                    f"TTS 合成完全失败：{len(translated_indices)} 个句子未生成任何音频，"
                    f"请检查 TTS 引擎配置和环境依赖"
                )

        update_task(
            task_id,
            progress=80,
            message=f"语音合成完成: {len(tts_audio_map)} 条中文语音",
        )

        # ---- Step 4: 中英交替音频组装 ----
        update_task(
            task_id, step="merge", progress=82, message="Step 4/4: 组装中英交替音频..."
        )

        output_audio_path = os.path.join(
            result_dir, f"{basename}_sentence.{config.OUTPUT_FORMAT}"
        )

        # 背景音乐保留：优先复用 Step0 提前分离好的伴奏轨（_bgm_path），
        # 避免重复调用 demucs；仅当提前分离未执行/未成功时才在此处补做一次
        bgm_path = task.get("_bgm_path", "") or None
        keep_bgm = task.get("keep_bgm", False)
        if keep_bgm and not bgm_path and audio_path and os.path.exists(audio_path):
            update_task(task_id, message="正在分离人声与背景音...")
            sep_cache_dir = vocal_cache_for_task(
                task, os.path.join(result_dir, "vocal_separation")
            )
            sep_result = separate_vocals(audio_path, sep_cache_dir)
            if sep_result.get("ok"):
                bgm_path = sep_result.get("no_vocals_path", "")
                print(f"[VocalSep] 背景音分离成功: {bgm_path}")
            else:
                print(f"[VocalSep] 分离失败，跳过背景音保留: {sep_result.get('error')}")
        elif bgm_path:
            print(f"[VocalSep] 复用 Step0 提前分离的背景音轨: {bgm_path}")

        mix_result = mix_sentence_audio(
            audio_path=audio_path,
            segments=segments,
            translated_indices=translated_indices,
            translations=translations,
            tts_audio_map=tts_audio_map,
            output_path=output_audio_path,
            bgm_path=bgm_path,
        )

        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        # ---- 完成 ----
        result_data = {
            "basename": basename,
            "original_audio": audio_path,
            "mixed_audio": output_audio_path,
            "original_duration": mix_result["original_duration"],
            "mixed_duration": mix_result["mixed_duration"],
            "total_segments": mix_result["total_segments"],
            "translated_segments": mix_result["translated_segments"],
            "process_mode": task.get("process_mode", "sentence_translate"),
        }

        # 构建混合时间轴的 segments（start/end 替换为混合音频上的位置）
        segments_mixed = build_segments_with_mixed_time(
            segments, translations, mix_result["time_mapping"]
        )

        sentence_pairs = []
        for seg_idx in translated_indices:
            if seg_idx in translations and seg_idx < len(segments_mixed):
                sentence_pairs.append(
                    {
                        "index": seg_idx,
                        "english": segments_mixed[seg_idx].get("text", "").strip(),
                        "chinese": translations[seg_idx],
                        "start": segments_mixed[seg_idx].get("start", 0),
                        "end": segments_mixed[seg_idx].get("end", 0),
                    }
                )

        mode = "sentence_translate"
        is_video = task.get("type") == "video"

        update_task(
            task_id,
            status="processing" if is_video else "completed",
            step="assemble" if is_video else "done",
            progress=95 if is_video else 100,
            message="音频合成完成，正在组装视频..." if is_video else "全部完成！",
            result=result_data,
            sentence_pairs=sentence_pairs,
            time_mapping=mix_result["time_mapping"],
            segments_mixed=segments_mixed,
        )

        # 回写关联单集状态为已转录
        try:
            update_episode_status_by_task(task_id, "transcribed")
        except Exception:
            print(f"[WARN] 更新单集 transcribed 状态失败（非关键）")

        save_task_result_to_disk(
            result_dir,
            {
                "task_id": task_id,
                "status": "completed",
                "process_mode": "sentence_translate",
                "transcription_text": full_text,
                "segments": segments,
                "segments_mixed": segments_mixed,
                "difficult_words": task.get("difficult_words", []),
                "translations": translations,
                "translated_indices": translated_indices,
                "sentence_pairs": sentence_pairs,
                "result": result_data,
                "time_mapping": mix_result["time_mapping"],
                # 持久化 tts_audio_map 避免重启丢失
                "tts_audio_map": tts_audio_map,
                # 步骤耗时
                "_step_timing": task.get("_step_timing", []),
                # 视频任务：保存原始视频路径，用于「原始」标签页播放
                "_video_path": task.get("_video_path", ""),
                "_subtitle_mode": task.get("_subtitle_mode", "bilingual"),
                "_subtitle_font_size": task.get("_subtitle_font_size"),
            },
        )

        _cleanup_intermediate_files(result_dir)

        try:
            dw = task.get("difficult_words", [])
            if dw:
                task_title = task.get("title", "") or basename
                print(
                    f"[Vocab] 已保存 {len(dw)} 个生词到全局生词库 (任务 {task_id[:8]}...)"
                )
        except Exception as ve:
            print(f"[Vocab] 保存生词库失败: {ve}")

    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已被终止")
    except Exception as e:
        traceback.print_exc()
        task = get_task(task_id) or {}
        update_task(
            task_id,
            status="error",
            message=f"处理出错: {str(e)}",
            _failed_step=task.get("step", "synthesize"),
        )
    finally:
        cancel_flags.pop(task_id, None)
        task_subprocesses.pop(task_id, None)


def _run_video_post_process(task_id):
    """视频任务后处理：生成双语字幕 + 组装配音视频。

    从任务 state 读取 result + _video_path，执行 Step 5 并更新状态。
    返回 True/False 表示是否成功。
    """
    try:
        task = get_task(task_id)
        if not task:
            update_task(task_id, status="error", message="任务不存在，视频后处理失败")
            return False
        result = task.get("result") or {}
        if not result:
            print(f"[Video] 无混音结果，跳过后处理")
            update_task(task_id, status="error", message="无混音结果，视频后处理失败")
            return False

        mode = task.get("_subtitle_mode", "bilingual")
        sub_font_size = task.get("_subtitle_font_size", None)
        if sub_font_size is not None and sub_font_size <= 0:
            sub_font_size = None  # -1/0 表示自动计算
        basename = task.get("_basename", "")
        video_path = task.get("_video_path", "")
        mixed_audio = result.get("mixed_audio", "")
        result_dir = os.path.join(config.RESULT_DIR, basename)

        if not video_path or not os.path.isfile(video_path):
            print(f"[Video] 视频文件缺失: {video_path}")
            update_task(task_id, status="error", message=f"视频文件缺失")
            return False
        if not mixed_audio or not os.path.isfile(mixed_audio):
            print(f"[Video] 混音文件缺失: {mixed_audio}")
            update_task(task_id, status="error", message=f"混音文件缺失")
            return False

        # Step 5a: 生成双语 ASS 字幕
        update_task(task_id, step="subtitle", progress=93, message="正在生成字幕...")
        segments = task.get("segments", [])
        translations = task.get("translations", {})
        time_mapping = task.get("time_mapping", [])
        subtitle_path = os.path.join(result_dir, f"{basename}.ass")
        video_w, video_h = _probe_video_size(video_path)
        subtitle_result = generate_bilingual_ass(
            segments,
            translations,
            time_mapping,
            subtitle_path,
            subtitle_mode=mode,
            video_height=video_h,
            video_width=video_w,
            subtitle_font_size=sub_font_size,
        )
        if not subtitle_result:
            print(f"[Video] 字幕生成失败")
            update_task(task_id, status="error", message="字幕生成失败")
            return False

        # Step 5b: 组装视频
        update_task(
            task_id,
            step="assemble",
            progress=96,
            message="正在合成视频（可能需要几分钟）...",
        )
        output_video = os.path.join(result_dir, f"{basename}_dubbed.mp4")
        if os.path.exists(output_video):
            try:
                os.remove(output_video)
            except Exception:
                pass
        assembled = assemble_video(
            video_path,
            mixed_audio,
            subtitle_path,
            output_video,
            time_mapping=time_mapping,
            segments=segments,
            translations=translations,
            subtitle_mode=mode,
            subtitle_font_size=sub_font_size,
            video_width=video_w,
            video_height=video_h,
        )

        if not assembled or not os.path.exists(assembled):
            update_task(
                task_id,
                status="error",
                message="视频组装失败，请检查 ffmpeg 和磁盘空间",
            )
            return False

        # 存储视频结果
        video_result = {
            "video_url": f"/api/audio/{basename}/{basename}_dubbed.mp4",
            "subtitle_url": f"/api/audio/{basename}/{basename}.ass",
            "video_path": assembled,
            "subtitle_path": subtitle_path,
        }
        update_task(
            task_id,
            status="completed",
            step="done",
            progress=100,
            message="视频配音完成！",
            video_result=video_result,
        )

        save_task_result_to_disk(result_dir, get_task(task_id))

        return True

    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已被终止")
        return False
    except Exception as e:
        traceback.print_exc()
        update_task(task_id, status="error", message=f"视频后处理出错: {str(e)}")
        return False


def _finish_resumed_audio(
    task_id,
    audio_path,
    output_path,
    segments,
    translations,
    translated_indices,
    tts_audio_map,
    mix_result,
):
    """Audio is an intermediate for video tasks, never a successful substitute."""
    if is_cancelled(task_id):
        update_task(task_id, status="cancelled", message="重试已取消")
        return False
    task = get_task(task_id)
    identity = task_identity(task)
    if identity["type"] == "analysis":
        raise ValueError("Analysis tasks cannot enter speech synthesis")
    mapping = mix_result["time_mapping"]
    mixed = build_segments_with_mixed_time(segments, translations, mapping)
    pairs = [
        dict(
            index=i,
            english=mixed[i].get("text", "").strip(),
            chinese=translations[i],
            start=mixed[i].get("start", 0),
            end=mixed[i].get("end", 0),
        )
        for i in translated_indices
        if i in translations and i < len(mixed)
    ]
    result = dict(
        basename=task["_basename"],
        original_audio=audio_path,
        mixed_audio=output_path,
        original_duration=mix_result["original_duration"],
        mixed_duration=mix_result["mixed_duration"],
        total_segments=len(segments),
        translated_segments=len(translated_indices),
        process_mode=identity["process_mode"],
    )
    video = identity["type"] == "video"
    update_task(
        task_id,
        **identity,
        result=result,
        segments=segments,
        translations=translations,
        translated_indices=translated_indices,
        time_mapping=mapping,
        segments_mixed=mixed,
        sentence_pairs=pairs,
        tts_audio_map=tts_audio_map,
        video_result=None,
        status="processing" if video else "completed",
        step="assemble" if video else "done",
        progress=93 if video else 100,
        message="音频已就绪，继续合成原视频…" if video else "断点续传完成！",
    )
    folder = os.path.join(config.RESULT_DIR, task["_basename"])
    save_task_result_to_disk(folder, get_task(task_id))
    if video:
        success = _run_video_post_process(task_id)
        if not success and get_task(task_id).get("status") not in (
            "error",
            "cancelled",
        ):
            update_task(
                task_id,
                status="error",
                message="视频组装未完成，保留原视频任务等待重试",
            )
        save_task_result_to_disk(folder, get_task(task_id))
        return success
    return True


def _synthesis_resume(task_id):
    """断点续传：跳过转录+翻译，只补充合成缺失的 TTS 文件并重新混合"""
    task = get_task(task_id)
    if not task:
        return
    if task_identity(task)["type"] == "analysis":
        raise ValueError("人物分析任务不能进入配音合成重试")
    segments = task.get("segments", [])
    translated_indices = task.get("translated_indices", [])
    translations = task.get("translations", {})
    translations = {int(k): v for k, v in translations.items()}
    audio_path = task.get("_audio_path", "")
    basename = task.get("_basename", "")
    result_dir = os.path.join(config.RESULT_DIR, basename)

    existing_tts = task.get("tts_audio_map", {}) or {}
    missing_indices = list(translated_indices)  # content-addressed cache decides reuse

    if not missing_indices:
        tts_audio_map = existing_tts
        print(f"[SynthesisResume] 全部 {len(tts_audio_map)} 条 TTS 已就绪，直接混合")
    else:
        missing_translations = {
            k: translations[k] for k in missing_indices if k in translations
        }
        confucius_cache_dir = os.path.join(result_dir, "tts_confucius_cache")
        os.makedirs(confucius_cache_dir, exist_ok=True)

        def _progress(current, total):
            task_cur = get_task(task_id)
            if task_cur and task_cur.get("step") != "synthesize":
                return
            update_task(
                task_id,
                step="synthesize",
                progress=60 + int((current / max(total, 1)) * 20),
                message=f"补充合成 TTS ({current}/{total})",
            )

        def _cancel():
            return is_cancelled(task_id)

        update_task(
            task_id,
            status="processing",
            step="synthesize",
            progress=60,
            message=f"补充合成 {len(missing_indices)} 条 TTS...",
        )
        try:
            confucius_ref_map = _build_confucius_ref_map(
                task_id, segments, missing_indices
            )
            new_tts = synthesize_sentences_with_confucius_tts(
                segments,
                missing_indices,
                missing_translations,
                audio_path,
                confucius_cache_dir,
                ref_audio_map=confucius_ref_map,
                reference_mode=task.get("tts_reference_mode", "speaker"),
                cancel_check=_cancel,
                progress_cb=_progress,
                task_id=task_id,
            )
        except InterruptedError:
            update_task(task_id, status="cancelled", message="重试已被取消")
            return
        tts_audio_map = dict(existing_tts)
        tts_audio_map.update(new_tts)
        update_task(task_id, tts_audio_map=tts_audio_map)

    update_task(
        task_id,
        status="processing",
        step="merge",
        progress=82,
        message="重新组装音频...",
    )
    output_path = os.path.join(
        result_dir, f"{basename}_sentence.{config.OUTPUT_FORMAT}"
    )
    bgm_path = task.get("_bgm_path", "") or None
    if (
        task.get("keep_bgm", False)
        and not bgm_path
        and audio_path
        and os.path.exists(audio_path)
    ):
        sep_cache_dir = vocal_cache_for_task(
            task, os.path.join(result_dir, "vocal_separation")
        )
        sep_result = separate_vocals(audio_path, sep_cache_dir)
        if sep_result.get("ok"):
            bgm_path = sep_result.get("no_vocals_path", "")
    mix_result = mix_sentence_audio(
        audio_path,
        segments,
        translated_indices,
        translations,
        tts_audio_map,
        output_path,
        bgm_path=bgm_path,
    )
    return _finish_resumed_audio(
        task_id,
        audio_path,
        output_path,
        segments,
        translations,
        translated_indices,
        tts_audio_map,
        mix_result,
    )
