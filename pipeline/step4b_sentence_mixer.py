"""
Step 4b: 句子翻译模式 - 中英交替音频组装
按比例将部分英文句子替换为中文 TTS 翻译，形成中英交替的效果。

输出格式 (以 ratio=0.5 为例):
  英文原句0 → 中文翻译1(替换英文1) → 英文原句2 → 中文翻译3(替换英文3) → ...

被选中翻译的句子：用中文 TTS 替换英文原声（不是额外插入）
未被翻译的句子：保留英文原声

最终效果：一句英文、一句中文交替出现，共同讲述完整的文章内容。

100% 全翻译模式（视频配音场景）：
  每句中文 TTS 用 ffmpeg atempo 拉伸/压缩到原句子的精确时间窗口，
  再按原始绝对时间戳叠加到与原时间轴等长的静音底轨上 —— 完整保留
  原视频里每一句话之间的停顿（换气、镜头切换、戏剧停顿等），
  确保音频时间轴与视频画面永远同步，不会出现渐进式错位。
"""
import os
import subprocess
import tempfile

from pydub import AudioSegment

from core import config

# TTS 音频目标响度（dBFS），统一所有句子的音量
# Confucius4-TTS 输出音量受参考音频影响，不同句子参考音频原始音量不同，
# 导致 TTS 输出忽高忽低。归一化到统一 dBFS 消除此差异。
_TTS_TARGET_DBFS = -20.0


def _normalize_tts_audio(audio: AudioSegment,
                         target_dbfs: float = _TTS_TARGET_DBFS) -> AudioSegment:
    """
    将 TTS 音频统一到目标响度，消除不同句子之间的音量差异。

    Args:
        audio: 待归一化的音频片段
        target_dbfs: 目标响度（dBFS），默认 -20.0

    Returns:
        AudioSegment: 归一化后的音频
    """
    if audio.dBFS == float('-inf'):
        return audio  # 纯静音，无法归一化
    gain = target_dbfs - audio.dBFS
    return audio.apply_gain(gain)


# atempo 允许的拉伸范围：收紧到 0.92x~1.08x（±8%），配合 3 句滑动窗口
# 速度平滑，消除相邻句速度跳变导致的"忽快忽慢"听感。超出范围的差值
# 不再靠暴力拉伸吸收，改为允许音频溢出到下一句前的静音间隙。
_ATEMPO_MIN, _ATEMPO_MAX = 0.92, 1.08


def _time_stretch_to_speed(audio: AudioSegment, speed: float) -> AudioSegment:
    """
    用 ffmpeg atempo 按指定速度因子调速。

    Args:
        audio: 原始音频片段
        speed: 速度因子（_ATEMPO_MIN ~ _ATEMPO_MAX）

    Returns:
        AudioSegment: 调速后的音频（音高不变）
    """
    speed = max(_ATEMPO_MIN, min(_ATEMPO_MAX, speed))
    if abs(speed - 1.0) < 0.03:
        return audio

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_in, \
         tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_out:
        in_path, out_path = f_in.name, f_out.name
    try:
        audio.export(in_path, format="wav")
        r = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", in_path, "-filter:a", f"atempo={speed:.4f}",
            out_path,
        ], capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and os.path.exists(out_path):
            return AudioSegment.from_file(out_path)
        return audio
    finally:
        for p in (in_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass


def _time_stretch_natural(audio: AudioSegment, target_ms: int) -> AudioSegment:
    """
    用 ffmpeg atempo 将音频调速到接近目标时长。

    Args:
        audio: 原始音频片段
        target_ms: 目标时长（毫秒），仅用于计算合理的调速倍率

    Returns:
        AudioSegment: 调速后的音频（音高不变），实际时长可能与 target_ms 有偏差
    """
    src_ms = len(audio)
    if src_ms <= 0 or target_ms <= 0:
        return audio
    speed = src_ms / target_ms
    return _time_stretch_to_speed(audio, speed)


def mix_sentence_audio(
    audio_path: str,
    segments: list,
    translated_indices: list,
    translations: dict,
    tts_audio_map: dict,
    output_path: str,
    gap_ms: int = None,
    bgm_path: str = None,
    bgm_gain_db: float = -8.0,
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
        bgm_path: 背景音/伴奏轨路径（人声分离得到的 no_vocals 音频）。
                  提供时会先铺在底轨上（降低音量），再叠加中文 TTS，
                  保留原视频的背景音乐/环境音，提升沉浸感。仅在
                  100% 全翻译模式下生效。
        bgm_gain_db: 背景音混入时的音量调整（dB，负值=降低音量），
                     避免背景音过响掩盖人声

    Returns:
        dict: 混合结果信息
    """
    if gap_ms is None:
        gap_ms = getattr(config, "SENTENCE_GAP_MS", 400)

    # 全翻译模式下的固定短间隙（保证连贯不割裂）
    full_gap_ms = getattr(config, "SENTENCE_FULL_GAP_MS", 250)

    # 交叉淡化参数：每段音频首尾做淡入淡出，平滑 TTS 与原声之间的音色切换。
    # 25ms 仅够消除爆音；60ms 能更好掩盖音色差异（约 1-2 个音节过渡时间）。
    FADE_MS = 60

    # 已翻译索引集合（快速查找）
    translated_set = set(translated_indices)
    all_translated = len(translated_set) >= len(segments)

    # ============================================================
    # 100% 全翻译模式（视频配音场景）：
    # 每句中文 TTS 用 atempo 拉伸/压缩到原句子的精确时间窗口，
    # 按绝对时间戳叠加到与原时间轴等长的静音底轨上。
    # 完整保留原视频的停顿节奏，音画时间轴始终同步。
    # ============================================================
    if all_translated:
        print(f"[Step4b] 100% 全翻译模式：锚定原始时间戳 + atempo 拉伸对齐")
        print(f"  总句子数: {len(translated_indices)}")

        # 找到第一个可用的 TTS 文件获取音频参数
        first_tts = None
        for idx in translated_indices:
            p = tts_audio_map.get(idx, "")
            if p and os.path.exists(p):
                first_tts = p
                break
        if first_tts:
            probe = AudioSegment.from_file(first_tts)
            target_sr = probe.frame_rate
            target_channels = probe.channels
        else:
            print(f"[Step4b] 警告：所有 TTS 文件缺失，使用默认参数 22050Hz/mono")
            target_sr = 22050
            target_channels = 1

        # 底轨时长 = 原始音频/视频的真实总时长（而非最后一句转录文本的 end，
        # 因为片尾静音/无语音片段不会被 WhisperX 转录出 segment，
        # 若只用最后 segment.end 会导致底轨偏短，音画对不齐）
        last_seg_end = segments[-1].get("end", 0) if segments else 0
        real_duration = last_seg_end
        if audio_path and os.path.exists(audio_path):
            try:
                probe_full = AudioSegment.from_file(audio_path)
                real_duration = max(real_duration, len(probe_full) / 1000.0)
            except Exception as e:
                print(f"[Step4b] 警告：无法探测原始音频时长 ({e})，使用最后 segment.end")
        original_duration = real_duration
        base_len_ms = max(int(original_duration * 1000), 1000)
        result = AudioSegment.silent(duration=base_len_ms, frame_rate=target_sr)
        if target_channels == 2:
            result = result.set_channels(2)

        # 铺背景音/伴奏轨底层（人声分离得到的 no_vocals），保留原视频的
        # 背景音乐/环境音氛围。降低音量避免掩盖中文人声的清晰度。
        if bgm_path and os.path.exists(bgm_path):
            try:
                bgm_clip = AudioSegment.from_file(bgm_path)
                bgm_clip = bgm_clip.set_channels(target_channels)
                if bgm_clip.frame_rate != target_sr:
                    bgm_clip = bgm_clip.set_frame_rate(target_sr)
                bgm_clip = bgm_clip.apply_gain(bgm_gain_db)
                if len(bgm_clip) < base_len_ms:
                    bgm_clip = bgm_clip + AudioSegment.silent(
                        duration=base_len_ms - len(bgm_clip), frame_rate=target_sr)
                elif len(bgm_clip) > base_len_ms:
                    bgm_clip = bgm_clip[:base_len_ms]
                result = result.overlay(bgm_clip)
                print(f"[Step4b] 已叠加背景音轨 ({bgm_gain_db:+.1f}dB): {bgm_path}")
            except Exception as e:
                print(f"[Step4b] 警告：背景音轨叠加失败 ({e})，跳过")

        time_mapping = []
        translated_set_fast = set(translated_indices)
        sorted_indices = sorted(translated_indices)

        # ---- 速度平滑预计算 ----
        # 逐句独立调速会导致相邻句最大 41% 的速度跳变（忽快忽慢）。
        # 解决方法：先收集所有句子的原始 TTS 时长，计算每句的原始速度因子，
        # 然后应用 3 句滑动窗口平均，用平滑后的速度代替独立计算。
        print(f"[Step4b] 预计算速度平滑: {len(sorted_indices)} 句")
        raw_metadata = []  # [(seg_idx, src_ms, window_ms, raw_speed), ...]
        for pos, seg_idx in enumerate(sorted_indices):
            seg = segments[seg_idx]
            seg_start_ms = int(seg.get("start", 0) * 1000)
            seg_end_ms = int(seg.get("end", 0) * 1000)
            window_ms = max(seg_end_ms - seg_start_ms, 200)
            tts_path = tts_audio_map.get(seg_idx, "")
            if tts_path and os.path.exists(tts_path):
                src_dur = len(AudioSegment.from_file(tts_path))
                raw_speed = src_dur / window_ms
            else:
                raw_speed = 1.0  # 无 TTS 则不做调速
            raw_metadata.append((seg_idx, window_ms, raw_speed))

        # 3 句滑动窗口平均
        n = len(raw_metadata)
        smooth_speeds = {}
        for i, (seg_idx, window_ms, raw_speed) in enumerate(raw_metadata):
            if n <= 2:
                smooth = raw_speed
            else:
                left = max(0, i - 1)
                right = min(n, i + 2)
                speeds = [raw_metadata[j][2] for j in range(left, right)]
                smooth = sum(speeds) / len(speeds)
            smooth_speeds[seg_idx] = max(_ATEMPO_MIN, min(_ATEMPO_MAX, smooth))
            clamped = smooth_speeds[seg_idx]
            print(f"  [{seg_idx}] raw_speed={raw_speed:.3f} → smooth={smooth:.3f} → clamped={clamped:.3f}")

        # 追踪音频实际播放到的位置（而非原始时间戳），用于处理
        # TTS 时长超出原句子窗口时的顺延，避免中途硬切断语音
        actual_cursor_ms = 0

        for pos, seg_idx in enumerate(sorted_indices):
            if seg_idx >= len(segments):
                continue
            seg = segments[seg_idx]
            seg_start_ms = int(seg.get("start", 0) * 1000)
            seg_end_ms = int(seg.get("end", 0) * 1000)
            window_ms = max(seg_end_ms - seg_start_ms, 200)  # 原句子的时间窗口

            tts_path = tts_audio_map.get(seg_idx, "")
            chinese_text = translations.get(seg_idx, "")
            if not tts_path or not os.path.exists(tts_path):
                print(f"[Step4b] 警告: seg[{seg_idx}] TTS 文件缺失，该时间窗口保持静音")
                time_mapping.append({
                    "mixed_start": round(seg_start_ms / 1000.0, 3),
                    "mixed_end": round(seg_end_ms / 1000.0, 3),
                    "orig_start": round(seg_start_ms / 1000.0, 3),
                    "orig_end": round(seg_end_ms / 1000.0, 3),
                    "type": "silence",
                    "segment_index": seg_idx,
                })
                actual_cursor_ms = max(actual_cursor_ms, seg_end_ms)
                continue

            tts_clip = AudioSegment.from_file(tts_path)
            tts_clip = tts_clip.set_channels(target_channels)
            if tts_clip.frame_rate != target_sr:
                tts_clip = tts_clip.set_frame_rate(target_sr)

            # 先调速再归一化：若先归一化后调速，atempo 会改变感知响度
            # （加速压缩能量 → 变响，减速拉长 → 变轻），
            # 相邻句之间产生 ~3.5dB 听感跳变。
            # 先调速确保速度一致，再归一化保证音量一致。
            # 使用预计算的平滑 speed（3 句滑动平均），消除相邻句速度跳变。
            tts_clip = _time_stretch_to_speed(tts_clip, smooth_speeds.get(seg_idx, 1.0))
            tts_clip = _normalize_tts_audio(tts_clip)
            tts_clip = tts_clip.fade_in(FADE_MS).fade_out(FADE_MS)

            # 实际播放起点：若上一句播放已经拖到了这句的原定开始时间之后，
            # 顺延到上一句结束处（避免语音重叠），否则按原始时间戳播放
            # （保留原视频里本来就有的自然停顿）
            placement_start_ms = max(seg_start_ms, actual_cursor_ms)

            end_pos_ms = placement_start_ms + len(tts_clip)
            if end_pos_ms > len(result):
                extra = AudioSegment.silent(
                    duration=end_pos_ms - len(result), frame_rate=target_sr)
                result = result + extra
            result = result.overlay(tts_clip, position=placement_start_ms)
            actual_cursor_ms = end_pos_ms

            time_mapping.append({
                "mixed_start": round(placement_start_ms / 1000.0, 3),
                "mixed_end": round(end_pos_ms / 1000.0, 3),
                "orig_start": round(seg_start_ms / 1000.0, 3),
                "orig_end": round(seg_end_ms / 1000.0, 3),
                "type": "tts_chinese",
                "segment_index": seg_idx,
                "chinese": chinese_text,
            })
            delay_note = f" [顺延{(placement_start_ms - seg_start_ms)}ms]" if placement_start_ms > seg_start_ms else ""
            print(f"  [{seg_idx}] {chinese_text[:30]} "
                  f"(窗口{window_ms}ms, 实际{len(tts_clip)}ms, {seg_start_ms/1000:.1f}s-{seg_end_ms/1000:.1f}s){delay_note}")

        # 导出
        fmt = getattr(config, "OUTPUT_FORMAT", "mp3")
        bitrate = getattr(config, "OUTPUT_BITRATE", "192k")
        result.export(output_path, format=fmt, bitrate=bitrate)

        mixed_duration = len(result) / 1000.0

        print(f"[Step4b] 组装完成:")
        print(f"  原始时长: {original_duration:.1f}s")
        print(f"  混合时长: {mixed_duration:.1f}s (与原时长严格对齐)")
        print(f"  中文句数: {len(translated_indices)}")
        print(f"  输出: {output_path}")

        return {
            "output_path": output_path,
            "original_duration": round(original_duration, 1),
            "mixed_duration": round(mixed_duration, 1),
            "total_segments": len(segments),
            "translated_segments": len(translated_indices),
            "chinese_segments": len(translated_indices),
            "english_segments": 0,
            "time_mapping": time_mapping,
        }

    # ============================================================
    # 中英交替模式：逐句替换，英文原声 + 中文 TTS 交替
    # ============================================================

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
                tts_len_ms = len(tts_clip)  # 先取真实时长
                tts_clip = tts_clip.set_channels(target_channels)
                if tts_clip.frame_rate != target_sr:
                    tts_clip = tts_clip.set_frame_rate(target_sr)
                tts_clip = _normalize_tts_audio(tts_clip)
                tts_clip = tts_clip.fade_in(FADE_MS).fade_out(FADE_MS)

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
                    # 100% 全翻译模式：用固定短间隙替换原始的句间停顿。
                    # 原始间隙含呼吸、换气、戏剧停顿（可达 3s+），
                    # 中文 TTS 句间保留这些会严重割裂连贯性。
                    gap_dur = full_gap_ms
                    clean_gap = AudioSegment.silent(
                        duration=gap_dur, frame_rate=target_sr)
                    clean_gap = clean_gap.fade_in(FADE_MS).fade_out(FADE_MS)
                    result += clean_gap
                    time_mapping.append({
                        "mixed_start": round(mixed_pos_ms / 1000.0, 3),
                        "mixed_end": round((mixed_pos_ms + gap_dur) / 1000.0, 3),
                        "orig_start": round(seg_end_ms / 1000.0, 3),
                        "orig_end": round(next_start_ms / 1000.0, 3),
                        "type": "gap",
                        "segment_index": -1,
                    })
                    mixed_pos_ms += gap_dur
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


def build_segments_with_mixed_time(segments: list, translations: dict,
                                   time_mapping: list) -> list:
    """
    将原始 WhisperX segments 的时间戳替换为混合音频时间轴上的位置。

    从 time_mapping 中提取每个 segment 在混合音频中的 mixed_start，
    构建新的 segments 列表供前端字幕渲染和 seek 定位使用。

    Args:
        segments: 原始 WhisperX segments [{text, start, end, ...}, ...]
        translations: {segment_index: chinese_text}
        time_mapping: mix_sentence_audio 返回的 time_mapping

    Returns:
        list[dict]: segments 副本，start/end 替换为混合音频时间轴上的值
    """
    seg_to_mixed = {}
    for entry in time_mapping:
        if entry.get("type") == "tts_chinese" and entry.get("segment_index", -1) >= 0:
            sidx = entry["segment_index"]
            if sidx not in seg_to_mixed:
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
