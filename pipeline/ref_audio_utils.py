"""
Step 3 (Qwen3-TTS): 声音克隆语音合成模块
使用 Qwen3-TTS 从原音频中克隆说话人声音，合成生词语音。

核心策略:
- Segment 级别参考音频：每个生词使用其所在句子的原声作为克隆参考
  * 多说话人场景下，每个角色的声音自动匹配
  * 参考音频的语气、语速与目标句子一致，合成更自然
  * 极短 segment 回退到全篇最长纯净 segment 作为 fallback
- 紧挨着的相邻替换合并为一句话一起合成，避免割裂感
- 支持中英混合合成格式（如 "villain，反派"），用英文参考音频驱动，
  自然产生"外国人说中文"的语调效果，与原音频音色和谐一致
- 通过 subprocess 调用 qwen3-tts conda 环境中的 worker 脚本
"""
import hashlib
import json
import os
import subprocess
import tempfile
import threading

from core import config


def _get_audio_duration_ms(audio_path: str) -> int:
    """
    Get audio duration in milliseconds without loading the full file into memory.
    Uses pydub's mediainfo (ffprobe under the hood) — reads only metadata.
    """
    from pydub.utils import mediainfo
    info = mediainfo(audio_path)
    return int(float(info["duration"]) * 1000)


def _extract_audio_clip(audio_path: str, start_ms: int, end_ms: int,
                        output_path: str, timeout: int = 120) -> None:
    """
    Extract an audio clip using ffmpeg subprocess.
    Does NOT load the full source file into memory — ffmpeg handles seeking.

    Uses input seeking (-ss before -i) for fast extraction. Slight keyframe
    imprecision is acceptable for voice cloning reference audio.

    Args:
        audio_path: Path to source audio file
        start_ms: Start time in milliseconds
        end_ms: End time in milliseconds
        output_path: Output WAV file path
        timeout: Maximum seconds for ffmpeg to run

    Raises:
        subprocess.TimeoutExpired: if ffmpeg takes too long
        subprocess.CalledProcessError: if ffmpeg fails
    """
    start_s = start_ms / 1000.0
    duration_s = (end_ms - start_ms) / 1000.0
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(start_s),
        "-i", audio_path,
        "-t", str(duration_s),
        "-acodec", "pcm_s16le",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)


def _group_segments_into_turns(segments: list, same_speaker_gap: float = 0.3) -> list:
    """Group consecutive segments into speaker turns based on inter-segment gaps."""
    if not segments:
        return []
    groups = []
    current_group = [0]
    prev_end = segments[0].get("end", 0)
    for i in range(1, len(segments)):
        seg_start = segments[i].get("start", 0)
        gap = seg_start - prev_end
        if gap <= same_speaker_gap:
            current_group.append(i)
        else:
            groups.append(current_group)
            current_group = [i]
        prev_end = segments[i].get("end", 0)
    groups.append(current_group)
    return groups


def extract_ref_audio_for_segments(audio_path: str, segments: list,
                                   replacements: list, output_dir: str,
                                   ref_duration: float = None) -> tuple:
    """
    提取参考音频片段用于声音克隆。

    优化策略（边界感知 + 说话人分组）：
    - 每个 segment 使用自身边界提取参考音频，不向外扩展到相邻 segment，
      杜绝跨说话人音色污染
    - 通过 inter-segment gap 检测说话人轮次，同轮次的连续短句共享
      最长 segment 的参考音频，保证同一说话人音色一致
    - 极短 segment（< 1 秒）回退到全篇最长纯净 segment 作为 fallback

    Args:
        audio_path: 原始音频文件路径
        segments: WhisperX 的 segments 列表
        replacements: 替换列表，每项含 segment_index
        output_dir: 参考音频输出目录
        ref_duration: 参考音频最大时长上限（秒），默认用 config 配置

    Returns:
        tuple: (ref_map, ref_source_map)
            ref_map: {segment_index: ref_audio_path}
            ref_source_map: {segment_index: source_segment_index}
                告诉调用方每个 segment 实际使用了哪个 segment 的参考音频
                （同轮次共享时 source 可能不同于 segment_index）
    """
    if ref_duration is None:
        ref_duration = getattr(config, "QWEN3_TTS_REF_DURATION", 8)

    os.makedirs(output_dir, exist_ok=True)

    # 找出所有涉及的 segment_index（需要 TTS 的 segment）
    seg_indices = sorted(set(r["segment_index"] for r in replacements))

    print(f"[Step3-Qwen] 涉及 {len(seg_indices)} 个 segment 的替换，"
          f"每个 segment 使用自己的原声作为参考")

    # 只读取元数据获取时长，不加载完整音频到内存
    audio_duration_ms = _get_audio_duration_ms(audio_path)
    ref_duration_ms = int(ref_duration * 1000)

    # ---- 为每个 segment 用自己的原声做参考音频 ----
    ref_map = {}
    ref_source_map = {}
    for seg_idx in seg_indices:
        seg = segments[seg_idx]
        seg_start = seg.get("start", 0)
        seg_end = seg.get("end", 0)
        seg_dur = seg_end - seg_start

        seg_start_ms = int(seg_start * 1000)
        seg_end_ms = int(seg_end * 1000)

        if seg_end_ms <= seg_start_ms:
            print(f"  seg[{seg_idx}] 无效时间范围，跳过")
            continue

        # 用自己的段边界做参考，不向外扩展
        # 极长段（>60s）从中间取 60s 防极端情况
        self_max = max(ref_duration_ms * 2, 60_000)
        clip_start = seg_start_ms
        clip_end = seg_end_ms
        if clip_end - clip_start > self_max:
            seg_mid_ms = (seg_start_ms + seg_end_ms) // 2
            clip_start = max(0, seg_mid_ms - self_max // 2)
            clip_end = min(audio_duration_ms, clip_start + self_max)

        ref_filename = f"ref_seg_{seg_idx}.wav"
        ref_path = os.path.join(output_dir, ref_filename)
        _extract_audio_clip(audio_path, clip_start, clip_end, ref_path)

        ref_map[seg_idx] = ref_path
        ref_source_map[seg_idx] = seg_idx
        clip_dur = (clip_end - clip_start) / 1000.0
        print(f"  seg[{seg_idx}] ({seg_start:.1f}s-{seg_end:.1f}s, "
              f"{seg_dur:.1f}s) -> {ref_filename} "
              f"(clip: {clip_start/1000:.1f}s-{clip_end/1000:.1f}s, "
              f"{clip_dur:.1f}s)")

    print(f"[Step3-Qwen] 共 {len(ref_map)} 个独立参考音频 "
          f"(每个 segment 用自己的原声)")

    return ref_map, ref_source_map


def _assign_speaker_labels(segments: list) -> list:
    """
    为每个 segment 分配说话人标签。

    优先使用 diarization 的 speaker 字段；缺失时按 inter-segment gap 估算
    说话人轮次作为兜底（gap > SAME_SPEAKER_GAP 视为换人）。

    Returns:
        list: 每个 segment 的说话人标签（字符串）
    """
    has_real_speaker = any(seg.get("speaker") for seg in segments)
    if has_real_speaker:
        return [seg.get("speaker") or "spk_unknown" for seg in segments]

    # 无 diarization：用 gap 分组作为伪说话人
    same_speaker_gap = getattr(config, "SAME_SPEAKER_GAP", 0.8)
    groups = _group_segments_into_turns(segments, same_speaker_gap)
    seg_to_label = {}
    for gi, group in enumerate(groups):
        for idx in group:
            seg_to_label[idx] = f"spk_auto_{gi}"
    print("[Step3] ⚠️ 未检测到 speaker 标签，按 inter-segment gap 估算说话人。"
          "建议开启 WhisperX diarization 以获得更稳定音色。")
    return [seg_to_label.get(i, "spk_auto_0") for i in range(len(segments))]


def _find_speaker_longest_segment(segments: list, speaker_labels: list,
                                  target_speaker: str) -> "int | None":
    """返回指定说话人全篇时长最长的 segment 索引，无则 None。"""
    best_idx = None
    best_dur = 0.0
    for idx, seg in enumerate(segments):
        if speaker_labels[idx] != target_speaker:
            continue
        dur = seg.get("end", 0) - seg.get("start", 0)
        if dur > best_dur:
            best_dur = dur
            best_idx = idx
    return best_idx


def _find_global_longest_segment(segments: list,
                                 exclude_idx: int = None) -> "int | None":
    """返回全篇时长最长的 segment 索引（可排除指定索引），无则 None。

    用于同 speaker 无可用长段时的最终兜底，会跨 speaker 取最长段，
    音色匹配度较差但保证参考音频时长足够（避免极短碎片直接做参考）。
    """
    best_idx = None
    best_dur = 0.0
    for idx, seg in enumerate(segments):
        if exclude_idx is not None and idx == exclude_idx:
            continue
        dur = seg.get("end", 0) - seg.get("start", 0)
        if dur > best_dur:
            best_dur = dur
            best_idx = idx
    return best_idx


def extract_ref_audio_speaker_local(audio_path: str, segments: list,
                                    replacements: list, output_dir: str,
                                    engine: str = "fish") -> tuple:
    """
    说话人感知的本地参考音频提取（音色一致 + 情绪保真）。

    策略：
    - 每句优先用自身原声做参考（情绪/节奏最贴合该句）。
    - 自身过短（< min_ref_duration）时，向同说话人的相邻 segment 扩展边界，
      单次 ffmpeg 提取覆盖 [首段.start, 末段.end]，inter-segment 静音自然保留，
      直到达到 target_ref_duration 或无更多同说话人相邻段。
    - 严格校验 speaker 一致 + gap ≤ SAME_SPEAKER_GAP，杜绝跨说话人污染。
    - 扩展后仍不足时，回退到该说话人全篇最长的一段（牺牲情绪换音色稳定）；
      若同 speaker 无更长段（如该 speaker 全篇仅此一短句），进一步放宽到
      全篇任意 speaker 最长段（牺牲音色匹配换可用性，避免极短碎片直接做参考）。
    - 极长段以目标句为中心截取到 max_ref_duration。

    Args:
        audio_path: 原始音频文件路径
        segments: WhisperX segments 列表
        replacements: 替换列表，每项含 segment_index
        output_dir: 参考音频输出目录
        engine: "fish" 或 "qwen"，决定参考时长默认值

    Returns:
        tuple: (ref_map, ref_source_map, ref_text_map)
            ref_map: {seg_idx: ref_audio_path}
            ref_source_map: {seg_idx: 主参考 segment 索引（扩展时为自身，fallback 时为实际源段）}
            ref_text_map: {seg_idx: 参考音频对应的英文转录（拼接段文本）}
    """
    os.makedirs(output_dir, exist_ok=True)

    if engine == "fish":
        min_ref = getattr(config, "FISH_SPEECH_MIN_REF_DURATION", 4)
        target_ref = getattr(config, "FISH_SPEECH_REF_DURATION", 12)
    else:
        min_ref = getattr(config, "SEGMENT_REF_MIN_DURATION", 2)
        target_ref = getattr(config, "REF_TARGET_DURATION", 5)
    max_ref = getattr(config, "REF_MAX_DURATION", 30)

    min_ref_ms = int(min_ref * 1000)
    target_ref_ms = int(target_ref * 1000)
    max_ref_ms = int(max_ref * 1000)
    same_speaker_gap = getattr(config, "SAME_SPEAKER_GAP", 0.8)

    seg_indices = sorted(set(r["segment_index"] for r in replacements
                             if r["segment_index"] < len(segments)))
    if not seg_indices:
        return {}, {}, {}

    speaker_labels = _assign_speaker_labels(segments)
    audio_duration_ms = _get_audio_duration_ms(audio_path)

    ref_map = {}
    ref_source_map = {}
    ref_text_map = {}

    print(f"[Step3-{engine}] 说话人感知参考提取: {len(seg_indices)} 个 segment, "
          f"min={min_ref}s target={target_ref}s max={max_ref}s")

    for seg_idx in seg_indices:
        seg = segments[seg_idx]
        spk = speaker_labels[seg_idx]
        seg_start_ms = int(seg.get("start", 0) * 1000)
        seg_end_ms = int(seg.get("end", 0) * 1000)
        if seg_end_ms <= seg_start_ms:
            print(f"  seg[{seg_idx}] 无效时间范围，跳过")
            continue

        self_dur_ms = seg_end_ms - seg_start_ms
        clip_start = seg_start_ms
        clip_end = seg_end_ms
        included_indices = [seg_idx]
        ref_source_seg = seg_idx  # 主参考 segment（扩展时为自身，fallback 时为 longest）

        # 自身过短 → 向同说话人相邻段扩展
        if self_dur_ms < min_ref_ms:
            # 向前扩展
            j = seg_idx - 1
            while j >= 0 and (clip_end - clip_start) < target_ref_ms:
                if speaker_labels[j] != spk:
                    break
                j_end_ms = int(segments[j].get("end", 0) * 1000)
                j_start_ms = int(segments[j].get("start", 0) * 1000)
                gap_s = (clip_start - j_end_ms) / 1000.0
                if gap_s > same_speaker_gap:
                    break
                clip_start = min(clip_start, j_start_ms)
                included_indices.insert(0, j)
                j -= 1
            # 向后扩展
            j = seg_idx + 1
            while j < len(segments) and (clip_end - clip_start) < target_ref_ms:
                if speaker_labels[j] != spk:
                    break
                j_start_ms = int(segments[j].get("start", 0) * 1000)
                j_end_ms = int(segments[j].get("end", 0) * 1000)
                gap_s = (j_start_ms - clip_end) / 1000.0
                if gap_s > same_speaker_gap:
                    break
                clip_end = max(clip_end, j_end_ms)
                included_indices.append(j)
                j += 1

            # 扩展后仍不足 → 回退到该说话人全篇最长段
            if (clip_end - clip_start) < min_ref_ms:
                longest = _find_speaker_longest_segment(segments, speaker_labels, spk)
                # 同 speaker 无更长段（== 自身或不存在）→ 放宽到全篇任意 speaker 最长段
                if longest is None or longest == seg_idx:
                    longest = _find_global_longest_segment(segments, exclude_idx=seg_idx)
                    if longest is not None:
                        lseg_tmp = segments[longest]
                        print(f"  ⚠️ seg[{seg_idx}] spk={spk} 同 speaker 无更长段，"
                              f"回退到全篇最长段 seg[{longest}] "
                              f"({lseg_tmp.get('end', 0) - lseg_tmp.get('start', 0):.1f}s, "
                              f"跨 speaker, 音色可能不匹配)")
                if longest is not None and longest != seg_idx:
                    lseg = segments[longest]
                    lstart = int(lseg.get("start", 0) * 1000)
                    lend = int(lseg.get("end", 0) * 1000)
                    if (lend - lstart) > (clip_end - clip_start):
                        clip_start = lstart
                        clip_end = lend
                        included_indices = [longest]
                        ref_source_seg = longest

        # 极长 → 以目标句为中心截取到 max_ref
        if (clip_end - clip_start) > max_ref_ms:
            mid_ms = (seg_start_ms + seg_end_ms) // 2
            clip_start = max(0, mid_ms - max_ref_ms // 2)
            clip_end = min(audio_duration_ms, clip_start + max_ref_ms)

        # 提取参考音频
        ref_filename = f"ref_seg_{seg_idx}.wav"
        ref_path = os.path.join(output_dir, ref_filename)
        _extract_audio_clip(audio_path, clip_start, clip_end, ref_path)

        ref_map[seg_idx] = ref_path
        ref_source_map[seg_idx] = ref_source_seg

        # 参考文本：仅包含最终 clip 范围内的段文本（拼接）
        parts = []
        for idx in included_indices:
            if idx >= len(segments):
                continue
            iseg = segments[idx]
            istart = int(iseg.get("start", 0) * 1000)
            iend = int(iseg.get("end", 0) * 1000)
            if iend < clip_start or istart > clip_end:
                continue
            t = iseg.get("text", "").strip()
            if t:
                parts.append(t)
        ref_text_map[seg_idx] = " ".join(parts)

        clip_dur = (clip_end - clip_start) / 1000.0
        extra = f", 含相邻 {len(included_indices)} 段" if len(included_indices) > 1 else ""
        print(f"  seg[{seg_idx}] spk={spk} {seg.get('start', 0):.1f}s-"
              f"{seg.get('end', 0):.1f}s -> {ref_filename} "
              f"(clip {clip_start/1000:.1f}s-{clip_end/1000:.1f}s, "
              f"{clip_dur:.1f}s{extra})")

    print(f"[Step3-{engine}] 共 {len(ref_map)} 个参考音频")
    return ref_map, ref_source_map, ref_text_map
