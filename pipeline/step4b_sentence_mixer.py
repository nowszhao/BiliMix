"""
Step 4b: 句子翻译模式 - 中英交替音频组装
按比例将部分英文句子替换为中文 TTS 翻译，形成中英交替的效果。

输出格式 (以 ratio=0.5 为例):
  英文原句0 → 中文翻译1(替换英文1) → 英文原句2 → 中文翻译3(替换英文3) → ...

被选中翻译的句子：用中文 TTS 替换英文原声（不是额外插入）
未被翻译的句子：保留英文原声

最终效果：一句英文、一句中文交替出现，共同讲述完整的文章内容。
"""
import os

from pydub import AudioSegment

from core import config


def mix_sentence_audio(
    audio_path: str,
    segments: list,
    translated_indices: list,
    translations: dict,
    tts_audio_map: dict,
    output_path: str,
    gap_ms: int = None,
) -> dict:
    """
    组装中英交替音频：被翻译的句子用中文 TTS 替换英文原声。

    Args:
        audio_path: 原始英文音频文件路径
        segments: WhisperX segments 列表 [{text, start, end}, ...]
        translated_indices: 被翻译的 segment 索引列表（已排序）
        translations: {segment_index: chinese_text} 翻译结果
        tts_audio_map: {segment_index: tts_wav_path} 中文 TTS 音频映射
        output_path: 输出音频文件路径
        gap_ms: 句子之间的静音间隔（毫秒），None 则用 config 配置

    Returns:
        dict: 混合结果信息
    """
    if gap_ms is None:
        gap_ms = getattr(config, "SENTENCE_GAP_MS", 400)

    # 交叉淡化参数：每段音频首尾做淡入淡出，平滑 TTS 与原声之间的音色切换。
    # 25ms 仅够消除爆音；60ms 能更好掩盖音色差异（约 1-2 个音节过渡时间）。
    FADE_MS = 60

    print(f"[Step4b] 开始中英交替音频组装（替换模式）")
    print(f"  原始音频: {audio_path}")
    print(f"  总句子数: {len(segments)}, 翻译句子数: {len(translated_indices)}")
    print(f"  句间间隔: {gap_ms}ms, 淡入淡出: {FADE_MS}ms")

    # 加载原始音频
    original_audio = AudioSegment.from_file(audio_path)
    original_duration_ms = len(original_audio)
    target_sr = original_audio.frame_rate
    target_channels = original_audio.channels

    # 静音片段
    silence = AudioSegment.silent(duration=gap_ms, frame_rate=target_sr)

    # 已翻译索引集合（快速查找）
    translated_set = set(translated_indices)
    all_translated = len(translated_set) >= len(segments)

    # 构建混合音频
    result = AudioSegment.empty()
    time_mapping = []
    mixed_pos_ms = 0  # 当前混合音频的写入位置

    for seg_idx, seg in enumerate(segments):
        seg_start_ms = int(seg.get("start", 0) * 1000)
        seg_end_ms = int(seg.get("end", 0) * 1000)

        # 确保不超过音频实际长度
        seg_start_ms = min(seg_start_ms, original_duration_ms)
        seg_end_ms = min(seg_end_ms, original_duration_ms)

        if seg_end_ms <= seg_start_ms:
            continue

        if seg_idx in translated_set and seg_idx in tts_audio_map:
            # ---- 有翻译的句子：用中文 TTS 替换英文原声 ----
            tts_path = tts_audio_map[seg_idx]
            if os.path.exists(tts_path):
                tts_clip = AudioSegment.from_file(tts_path)
                tts_clip = tts_clip.set_frame_rate(target_sr).set_channels(target_channels)
                tts_clip = tts_clip.fade_in(FADE_MS).fade_out(FADE_MS)
                tts_len_ms = len(tts_clip)

                # 用中文 TTS 替换这个位置
                result += tts_clip
                chinese_text = translations.get(seg_idx, "")
                time_mapping.append({
                    "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                    "mixed_end": round((mixed_pos_ms + tts_len_ms) / 1000.0, 3),
                    "orig_start": round(seg_start_ms / 1000.0, 3),
                    "orig_end": round(seg_end_ms / 1000.0, 3),
                    "type": "tts_chinese",
                    "segment_index": seg_idx,
                    "chinese": chinese_text,
                })
                mixed_pos_ms += tts_len_ms

                eng_text = seg.get("text", "").strip()[:40]
                print(f"  [{seg_idx}] 🇨🇳 替换: \"{chinese_text[:30]}\" "
                      f"({tts_len_ms}ms, 原英文: \"{eng_text}\")")
            else:
                # TTS 文件缺失，回退保留英文原声
                print(f"  [{seg_idx}] [警告] TTS 文件不存在: {tts_path}，保留英文")
                eng_clip = original_audio[seg_start_ms:seg_end_ms]
                eng_clip = eng_clip.fade_in(FADE_MS).fade_out(FADE_MS)
                eng_len_ms = len(eng_clip)
                result += eng_clip
                time_mapping.append({
                    "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                    "mixed_end": round((mixed_pos_ms + eng_len_ms) / 1000.0, 3),
                    "orig_start": round(seg_start_ms / 1000.0, 3),
                    "orig_end": round(seg_end_ms / 1000.0, 3),
                    "type": "original",
                    "segment_index": seg_idx,
                })
                mixed_pos_ms += eng_len_ms
        else:
            # ---- 无翻译或无 TTS 的句子 ----
            if seg_idx in translated_set:
                # 选了翻译但 TTS 合成失败（tts_audio_map 中无此条目）
                if all_translated:
                    # 100% 全翻译模式：不应包含英文，用静音替代
                    eng_len_ms = seg_end_ms - seg_start_ms
                    silent_clip = AudioSegment.silent(
                        duration=eng_len_ms, frame_rate=target_sr)
                    result += silent_clip
                    print(f"  [{seg_idx}] [100%模式] 已选翻译但缺少 TTS 音频，用静音替代 "
                          f"({eng_len_ms}ms)")
                    time_mapping.append({
                        "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                        "mixed_end": round((mixed_pos_ms + eng_len_ms) / 1000.0, 3),
                        "orig_start": round(seg_start_ms / 1000.0, 3),
                        "orig_end": round(seg_end_ms / 1000.0, 3),
                        "type": "silence",
                        "segment_index": seg_idx,
                    })
                    mixed_pos_ms += eng_len_ms
                else:
                    # 非全翻译模式：保留英文原声
                    print(f"  [{seg_idx}] [警告] 已选翻译但缺少 TTS 音频 (tts_audio_map 无此条目)，保留英文")
                    eng_clip = original_audio[seg_start_ms:seg_end_ms]
                    eng_clip = eng_clip.fade_in(FADE_MS).fade_out(FADE_MS)
                    eng_len_ms = len(eng_clip)
                    result += eng_clip
                    time_mapping.append({
                        "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                        "mixed_end": round((mixed_pos_ms + eng_len_ms) / 1000.0, 3),
                        "orig_start": round(seg_start_ms / 1000.0, 3),
                        "orig_end": round(seg_end_ms / 1000.0, 3),
                        "type": "original",
                        "segment_index": seg_idx,
                    })
                    mixed_pos_ms += eng_len_ms
            else:
                # 未被选中的句子（非翻译目标）：保留英文原声
                eng_clip = original_audio[seg_start_ms:seg_end_ms]
                eng_clip = eng_clip.fade_in(FADE_MS).fade_out(FADE_MS)
                eng_len_ms = len(eng_clip)
                result += eng_clip
                time_mapping.append({
                    "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                    "mixed_end": round((mixed_pos_ms + eng_len_ms) / 1000.0, 3),
                    "orig_start": round(seg_start_ms / 1000.0, 3),
                    "orig_end": round(seg_end_ms / 1000.0, 3),
                    "type": "original",
                    "segment_index": seg_idx,
                })
                mixed_pos_ms += eng_len_ms

            eng_text = seg.get("text", "").strip()[:40]
            print(f"  [{seg_idx}] 🇬🇧 保留: \"{eng_text}\"")

        # 句子之间添加间隔
        if seg_idx < len(segments) - 1:
            next_seg = segments[seg_idx + 1]
            next_start_ms = int(next_seg.get("start", 0) * 1000)
            orig_gap_ms = max(0, next_start_ms - seg_end_ms)

            if orig_gap_ms > 0:
                if all_translated:
                    # 100% 全翻译模式：跳过原始间隙（含呼吸/环境音/click），
                    # 插入一段小静音使 TTS 衔接更干净自然
                    clean_gap = AudioSegment.silent(
                        duration=orig_gap_ms, frame_rate=target_sr)
                    clean_gap = clean_gap.fade_in(FADE_MS).fade_out(FADE_MS)
                    result += clean_gap
                    time_mapping.append({
                        "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                        "mixed_end": round((mixed_pos_ms + orig_gap_ms) / 1000.0, 3),
                        "orig_start": round(seg_end_ms / 1000.0, 3),
                        "orig_end": round(next_start_ms / 1000.0, 3),
                        "type": "gap",
                        "segment_index": -1,
                    })
                    mixed_pos_ms += orig_gap_ms
                else:
                    # 中英交替模式：保留原始的句间间隔（环境音自然过渡）
                    gap_clip = original_audio[seg_end_ms:next_start_ms]
                    gap_clip = gap_clip.fade_in(FADE_MS).fade_out(FADE_MS)
                    gap_len_ms = len(gap_clip)
                    result += gap_clip
                    time_mapping.append({
                        "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                        "mixed_end": round((mixed_pos_ms + gap_len_ms) / 1000.0, 3),
                        "orig_start": round(seg_end_ms / 1000.0, 3),
                        "orig_end": round(next_start_ms / 1000.0, 3),
                        "type": "gap",
                        "segment_index": -1,
                    })
                    mixed_pos_ms += gap_len_ms

    # 添加最后一段原始音频尾部（仅中英交替模式需要）
    if segments and not all_translated:
        last_end_ms = int(segments[-1].get("end", 0) * 1000)
        if last_end_ms < original_duration_ms:
            tail_clip = original_audio[last_end_ms:]
            tail_clip = tail_clip.fade_in(FADE_MS).fade_out(FADE_MS)
            tail_len_ms = len(tail_clip)
            if tail_len_ms > 0:
                result += tail_clip
                time_mapping.append({
                    "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                    "mixed_end": round((mixed_pos_ms + tail_len_ms) / 1000.0, 3),
                    "orig_start": round(last_end_ms / 1000.0, 3),
                    "orig_end": round(original_duration_ms / 1000.0, 3),
                    "type": "original",
                    "segment_index": -1,
                })

    # 导出
    fmt = getattr(config, "OUTPUT_FORMAT", "mp3")
    bitrate = getattr(config, "OUTPUT_BITRATE", "192k")
    result.export(output_path, format=fmt, bitrate=bitrate)

    original_duration = original_duration_ms / 1000.0
    mixed_duration = len(result) / 1000.0

    # 统计
    cn_count = sum(1 for t in time_mapping if t["type"] == "tts_chinese")
    en_count = sum(1 for t in time_mapping if t["type"] == "original" and t["segment_index"] >= 0)

    print(f"[Step4b] 组装完成:")
    print(f"  原始时长: {original_duration:.1f}s")
    print(f"  混合时长: {mixed_duration:.1f}s")
    print(f"  中文替换: {cn_count} 句, 英文保留: {en_count} 句")
    change_pct = (mixed_duration / original_duration - 1) * 100
    sign = "+" if change_pct >= 0 else ""
    print(f"  时长变化: {sign}{change_pct:.0f}%")
    print(f"  输出: {output_path}")

    return {
        "output_path": output_path,
        "original_duration": round(original_duration, 1),
        "mixed_duration": round(mixed_duration, 1),
        "total_segments": len(segments),
        "translated_segments": len(translated_indices),
        "chinese_segments": cn_count,
        "english_segments": en_count,
        "time_mapping": time_mapping,
    }
