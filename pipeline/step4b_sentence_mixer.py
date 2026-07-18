"""
Step 4b: 全翻译音频组装
将全部句子的中文 TTS 音频按顺序拼接，不做时间拉伸，
保证每句中文以自然语速播放，听感连贯。

时间轴处理：
  - 输出音频总时长 = 所有 TTS 片段之和 + 句间间隙
  - 每句 TTS 在新时间轴上的位置记录在 time_mapping 中
  - build_segments_with_mixed_time() 将所有 segment 的时间戳
    从 WhisperX 原始值替换为混合音频时间轴的对应值，
    前端字幕和 ASS 字幕均使用新时间戳，与音频精确同步

句间间隙策略：
  - 动态间隙：基于原始音频中相邻 segment 的实际 inter-segment gap，
    夹在 [DYNAMIC_GAP_MIN_MS, DYNAMIC_GAP_MAX_MS] 范围内
  - 当无法获取原始 gap 时回退到固定间隙（_DEFAULT_GAP_MS）
"""
import os
from typing import Optional

from pydub import AudioSegment

from core import config

# TTS 音频目标响度（dBFS），统一所有句子的音量
_TTS_TARGET_DBFS = getattr(config, "TTS_TARGET_DBFS", -20.0)

# 句间固定间隙（毫秒），作为无原始 gap 信息时的回退默认值
_DEFAULT_GAP_MS = getattr(config, "MIXER_DEFAULT_GAP_MS", 150)

# 句首/句尾淡入淡出（毫秒），平滑音色衔接
_FADE_MS = getattr(config, "MIXER_FADE_MS", 60)

# 动态间隙约束：基于原始音频 inter-segment gap 的 min/max 硬限制
_DYNAMIC_GAP_MIN_MS = getattr(config, "DYNAMIC_GAP_MIN_MS", 120)
_DYNAMIC_GAP_MAX_MS = getattr(config, "DYNAMIC_GAP_MAX_MS", 1200)

# LUFS 响度归一化目标（EBU R128 标准：-23 LUFS 为广播标准）
_TARGET_LUFS = getattr(config, "TTS_TARGET_LUFS", -23.0)

# Compressor 参数
_COMPRESSOR_THRESHOLD_DB = getattr(config, "COMPRESSOR_THRESHOLD_DB", -18.0)
_COMPRESSOR_RATIO = getattr(config, "COMPRESSOR_RATIO", 2.0)
_COMPRESSOR_ATTACK_MS = getattr(config, "COMPRESSOR_ATTACK_MS", 5.0)
_COMPRESSOR_RELEASE_MS = getattr(config, "COMPRESSOR_RELEASE_MS", 50.0)

# Limiter 参数（硬限幅，防止爆音）
_LIMITER_THRESHOLD_DB = getattr(config, "LIMITER_THRESHOLD_DB", -1.0)


def _normalize_tts_audio(audio: AudioSegment,
                         target_dbfs: float = _TTS_TARGET_DBFS) -> AudioSegment:
    """
    将 TTS 音频统一到目标响度，消除不同句子之间的音量差异。

    使用 ITU-R BS.1770 (LUFS) 响度标准替代简单的 dBFS RMS 估值，
    对短句（如单字/两字应答）的感知响度归一化更精确。
    """
    import numpy as np
    try:
        import pyloudnorm as pyln
    except ImportError:
        # pyloudnorm 不可用时回退到 pydub dBFS
        if audio.dBFS == float('-inf'):
            return audio
        gain = target_dbfs - audio.dBFS
        return audio.apply_gain(gain)

    # 将 pydub AudioSegment 转为 numpy 数组供 pyloudnorm 处理
    samples = np.array(audio.get_array_of_samples(), dtype=np.float64)
    # 归一化到 [-1, 1] 范围
    max_val = float(2 ** (audio.sample_width * 8 - 1))
    samples = samples / max_val

    # 测量当前响度
    meter = pyln.Meter(audio.frame_rate)
    current_loudness = meter.integrated_loudness(samples)

    # 计算需要的增益并应用
    if np.isfinite(current_loudness):
        gain_db = _TARGET_LUFS - current_loudness
        gain_linear = 10.0 ** (gain_db / 20.0)
        samples = samples * gain_linear
    # else: 静音片段，不做增益

    # 转回 pydub AudioSegment
    samples = np.clip(samples, -1.0, 1.0)
    samples_int = (samples * max_val).astype(np.int16)
    return AudioSegment(
        samples_int.tobytes(),
        frame_rate=audio.frame_rate,
        sample_width=audio.sample_width,
        channels=audio.channels,
    )


def _apply_dynamics_processing(audio: AudioSegment) -> AudioSegment:
    """
    对 TTS 音频做 compressor + limiter 后处理。

    压缩器缩小动态范围（避免忽高忽低），限幅器削掉偶发的爆音峰值。
    使用 pydub 内置的 compress_dynamic_range 实现。
    """
    # Compressor: 压缩动态范围
    audio = audio.compress_dynamic_range(
        threshold=_COMPRESSOR_THRESHOLD_DB,
        ratio=_COMPRESSOR_RATIO,
        attack=_COMPRESSOR_ATTACK_MS,
        release=_COMPRESSOR_RELEASE_MS,
    )
    # Limiter: 硬限幅，防止爆音
    # pydub 没有内置 limiter，用 apply_gain 后 clip 近似
    # 先把整体降到 limiter 阈值以下再切掉超出的部分
    headroom = audio.max_dBFS - _LIMITER_THRESHOLD_DB
    if headroom > 0:
        audio = audio.apply_gain(-headroom)
    return audio


def _compute_dynamic_gap_ms(segments: list, seg_idx: int,
                           next_seg_idx: int) -> float:
    """
    计算两个连续 segment 之间的动态间隙。

    基于原始音频中相邻 segment 的 inter-segment gap（next.start - current.end），
    夹在 [_DYNAMIC_GAP_MIN_MS, _DYNAMIC_GAP_MAX_MS] 范围内。

    当无法获取原始 gap（segments 缺失或 gap 异常）时，回退到 _DEFAULT_GAP_MS。

    Args:
        segments: WhisperX segments 列表
        seg_idx: 当前 segment 索引
        next_seg_idx: 下一个 segment 索引

    Returns:
        float: 间隙时长（毫秒）
    """
    if (seg_idx >= len(segments) or next_seg_idx >= len(segments)):
        return _DEFAULT_GAP_MS

    current_end = segments[seg_idx].get("end", 0)
    next_start = segments[next_seg_idx].get("start", 0)
    raw_gap_s = next_start - current_end

    # 异常 gap（负数或过大）→ 回退固定间隙
    if raw_gap_s <= 0 or raw_gap_s > 10.0:
        return _DEFAULT_GAP_MS

    raw_gap_ms = raw_gap_s * 1000.0
    return max(_DYNAMIC_GAP_MIN_MS, min(raw_gap_ms, _DYNAMIC_GAP_MAX_MS))


def mix_sentence_audio(
    audio_path: str,
    segments: list,
    translated_indices: list,
    translations: dict,
    tts_audio_map: dict,
    output_path: str,
    gap_ms: int = None,
    bgm_path: str = None,
    bgm_gain_db: Optional[float] = None,
) -> dict:
    """
    全翻译音频组装：逐句顺序拼接中文 TTS 音频。

    不做时间拉伸，每句中文以 TTS 引擎的自然语速播放。
    拼接后音频的总时长可能与原始音频不同（通常更短），
    但所有句子的时间戳会反向映射到新时间轴上。

    Args:
        audio_path: 原始音频文件路径（仅用于探测原始时长和采样参数）
        segments: WhisperX segments 列表 [{text, start, end}, ...]
        translated_indices: 已翻译的 segment 索引列表
        translations: {segment_index: chinese_text}
        tts_audio_map: {segment_index: tts_wav_path} 中文 TTS 音频映射
        output_path: 输出音频文件路径
        gap_ms: 句子之间的静音间隔（毫秒），None 则用 _DEFAULT_GAP_MS
        bgm_path: 背景音/伴奏轨路径（人声分离得到的 no_vocals）。
                  提供时会降低音量后叠加到整段拼接音频上。
        bgm_gain_db: 背景音混入时的音量调整（dB，负值=降低音量）

    Returns:
        dict: 混合结果信息
    """
    if gap_ms is None:
        gap_ms = getattr(config, "SENTENCE_GAP_MS", _DEFAULT_GAP_MS)
    if bgm_gain_db is None:
        bgm_gain_db = getattr(config, "MIXER_BGM_GAIN_DB", -10.0)

    sorted_indices = sorted(translated_indices)

    if not sorted_indices or not tts_audio_map:
        print("[Step4b] 没有 TTS 音频，无法组装")
        return {
            "output_path": "",
            "original_duration": 0,
            "mixed_duration": 0,
            "total_segments": len(segments),
            "translated_segments": len(translated_indices),
            "chinese_segments": 0,
            "english_segments": 0,
            "time_mapping": [],
        }

    # ---- 探测音频参数 ----
    first_tts = None
    for idx in sorted_indices:
        p = tts_audio_map.get(idx, "")
        if p and os.path.exists(p):
            first_tts = p
            break

    if first_tts:
        probe = AudioSegment.from_file(first_tts)
        target_sr = probe.frame_rate
        target_channels = probe.channels
    else:
        print("[Step4b] 所有 TTS 文件缺失，使用默认参数 22050Hz/mono")
        target_sr = 22050
        target_channels = 1

    # 探测原始音频时长（仅用于统计输出）
    original_duration = segments[-1].get("end", 0) if segments else 0
    if audio_path and os.path.exists(audio_path):
        try:
            probe_full = AudioSegment.from_file(audio_path)
            original_duration = max(original_duration, len(probe_full) / 1000.0)
        except Exception:
            pass

    # ---- 顺序拼接所有 TTS 片段 ----
    result = AudioSegment.empty()
    time_mapping = []
    mixed_pos_ms = 0

    # 统计间隙使用情况
    gap_stats = {"dynamic": 0, "fallback": 0}
    # 生成用于拼接的 silence 缓存：按需创建不同时长的静音片段
    _silence_cache = {}

    def _get_silence(duration_ms: float) -> AudioSegment:
        dur = int(round(duration_ms))
        if dur <= 0:
            return AudioSegment.empty()
        key = (dur, target_channels, target_sr)
        if key not in _silence_cache:
            s = AudioSegment.silent(duration=dur, frame_rate=target_sr)
            if target_channels == 2:
                s = s.set_channels(2)
            _silence_cache[key] = s
        return _silence_cache[key]

    use_dynamic = getattr(config, "DYNAMIC_GAP_ENABLED", True)

    print(f"[Step4b] 拼接模式: {len(sorted_indices)} 句中文 TTS, "
          f"间隙={'动态' if use_dynamic else '固定 ' + str(gap_ms) + 'ms'} "
          f"(范围 {_DYNAMIC_GAP_MIN_MS}-{_DYNAMIC_GAP_MAX_MS}ms)")

    for i, seg_idx in enumerate(sorted_indices):
        seg = segments[seg_idx] if seg_idx < len(segments) else {}
        tts_path = tts_audio_map.get(seg_idx, "")
        chinese_text = translations.get(seg_idx, "")

        if tts_path and os.path.exists(tts_path):
            tts_clip = AudioSegment.from_file(tts_path)
            tts_clip = tts_clip.set_channels(target_channels)
            if tts_clip.frame_rate != target_sr:
                tts_clip = tts_clip.set_frame_rate(target_sr)

            tts_clip = _normalize_tts_audio(tts_clip)
            tts_clip = _apply_dynamics_processing(tts_clip)
            tts_clip = tts_clip.fade_in(_FADE_MS).fade_out(_FADE_MS)
            tts_len_ms = len(tts_clip)

            result += tts_clip

            entry_type = "tts_chinese"
            print(f"  [{seg_idx}] {chinese_text[:30]} "
                  f"({tts_len_ms}ms)")
        else:
            # TTS 缺失：插入固定间隙，保持时间轴连贯
            tts_len_ms = _DEFAULT_GAP_MS
            entry_type = "silence"
            print(f"  [{seg_idx}] [跳过] TTS 文件缺失，停顿 {tts_len_ms}ms")

        start_s = round(mixed_pos_ms / 1000.0, 3)
        end_s = round((mixed_pos_ms + tts_len_ms) / 1000.0, 3)
        orig_start = round(seg.get("start", 0), 3)
        orig_end = round(seg.get("end", 0), 3)

        time_mapping.append({
            "mixed_start": start_s,
            "mixed_end": end_s,
            "orig_start": orig_start,
            "orig_end": orig_end,
            "type": entry_type,
            "segment_index": seg_idx,
            "chinese": chinese_text if entry_type == "tts_chinese" else "",
        })

        mixed_pos_ms += tts_len_ms

        # 句间间隙（最后一句后面不加）
        if i < len(sorted_indices) - 1:
            next_seg_idx = sorted_indices[i + 1]
            if use_dynamic:
                cur_gap_ms = _compute_dynamic_gap_ms(
                    segments, seg_idx, next_seg_idx)
                if cur_gap_ms == _DEFAULT_GAP_MS:
                    gap_stats["fallback"] += 1
                else:
                    gap_stats["dynamic"] += 1
            else:
                cur_gap_ms = gap_ms

            result += _get_silence(cur_gap_ms)
            mixed_pos_ms += cur_gap_ms

    # ---- 可选 BGM 叠加 ----
    if bgm_path and os.path.exists(bgm_path):
        try:
            bgm_clip = AudioSegment.from_file(bgm_path)
            bgm_clip = bgm_clip.set_channels(target_channels)
            if bgm_clip.frame_rate != target_sr:
                bgm_clip = bgm_clip.set_frame_rate(target_sr)
            bgm_clip = bgm_clip.apply_gain(bgm_gain_db)

            tts_duration = len(result)
            if len(bgm_clip) > tts_duration:
                bgm_clip = bgm_clip[:tts_duration]
                bgm_clip = bgm_clip.fade_out(min(500, tts_duration))
            elif len(bgm_clip) < tts_duration:
                bgm_clip = bgm_clip + AudioSegment.silent(
                    duration=tts_duration - len(bgm_clip),
                    frame_rate=target_sr)

            result = result.overlay(bgm_clip)
            print(f"[Step4b] 已叠加背景音轨 "
                  f"({bgm_gain_db:+.1f}dB, 截断到 {tts_duration/1000:.1f}s)")
        except Exception as e:
            print(f"[Step4b] 背景音叠加失败: {e}")

    # ---- 导出 ----
    fmt = getattr(config, "OUTPUT_FORMAT", "mp3")
    bitrate = getattr(config, "OUTPUT_BITRATE", "192k")
    result.export(output_path, format=fmt, bitrate=bitrate)

    mixed_duration = len(result) / 1000.0

    cn_count = sum(1 for t in time_mapping if t["type"] == "tts_chinese")
    change_pct = (mixed_duration / original_duration - 1) * 100 if original_duration > 0 else 0
    sign = "+" if change_pct >= 0 else ""

    print(f"[Step4b] 组装完成:")
    print(f"  原始时长: {original_duration:.1f}s")
    print(f"  混合时长: {mixed_duration:.1f}s ({sign}{change_pct:.0f}%)")
    print(f"  中文句数: {cn_count}/{len(sorted_indices)}")
    if use_dynamic:
        print(f"  动态间隙: {gap_stats['dynamic']} 处 | 回退固定间隙: {gap_stats['fallback']} 处 "
              f"(范围 {_DYNAMIC_GAP_MIN_MS}-{_DYNAMIC_GAP_MAX_MS}ms)")
    print(f"  输出: {output_path}")

    return {
        "output_path": output_path,
        "original_duration": round(original_duration, 1),
        "mixed_duration": round(mixed_duration, 1),
        "total_segments": len(segments),
        "translated_segments": len(translated_indices),
        "chinese_segments": cn_count,
        "english_segments": 0,
        "time_mapping": time_mapping,
    }


def build_segments_with_mixed_time(segments: list, translations: dict,
                                   time_mapping: list) -> list:
    """
    将原始 WhisperX segments 的时间戳替换为混合音频时间轴上的位置。

    从 time_mapping 中提取每个 segment 在混合音频中的 start/end，
    构建新的 segments 列表供前端字幕渲染和 seek 定位使用。

    所有在 time_mapping 中有 entry 的 segment（无论 tts_chinese
    还是 silence 类型），都使用混合音频时间轴的新时间戳。

    Args:
        segments: 原始 WhisperX segments [{text, start, end, ...}, ...]
        translations: {segment_index: chinese_text}
        time_mapping: mix_sentence_audio 返回的 time_mapping

    Returns:
        list[dict]: segments 副本，start/end 替换为混合音频时间轴上的值
    """
    seg_to_mixed = {}
    for entry in time_mapping:
        sidx = entry.get("segment_index", -1)
        if sidx >= 0 and sidx not in seg_to_mixed:
            seg_to_mixed[sidx] = {
                "start": round(entry["mixed_start"], 3),
                "end": round(entry["mixed_end"], 3),
            }

    result = []
    for i, seg in enumerate(segments):
        s = {
            "text": seg.get("text", "").strip(),
            "speaker": seg.get("speaker", ""),
        }
        if i in seg_to_mixed:
            s["start"] = seg_to_mixed[i]["start"]
            s["end"] = seg_to_mixed[i]["end"]
            if i in translations:
                s["chinese"] = translations[i]
        else:
            s["start"] = round(seg.get("start", 0), 3)
            s["end"] = round(seg.get("end", 0), 3)
        result.append(s)
    return result
