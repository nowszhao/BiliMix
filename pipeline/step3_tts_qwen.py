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


def _estimate_original_speech_rate(segments: list) -> float:
    """
    从 WhisperX segments 估算原始说话人的语速（words per second）。

    用于指导 TTS 合成的 max_new_tokens，使中文 TTS 的时长
    尽可能匹配原始英文的节奏。

    Args:
        segments: WhisperX segments 列表 [{text, start, end}, ...]

    Returns:
        float: 每秒单词数，默认 2.5（英语正常语速）
    """
    if not segments:
        return 2.5

    total_words = 0
    total_dur = 0.0
    for seg in segments:
        text = seg.get("text", "").strip()
        if text:
            total_words += len(text.split())
        dur = seg.get("end", 0) - seg.get("start", 0)
        total_dur += dur

    if total_dur > 0:
        wps = total_words / total_dur
        print(f"[Step3-Qwen] 原始语速: {wps:.1f} words/sec "
              f"({total_words} 词 / {total_dur:.1f}s)")
        return wps
    return 2.5


def _build_tts_text(group_indices: list, replacements: list) -> str:
    """
    根据配置构建 TTS 合成文本。

    当 config.TTS_TEXT_FORMAT == "mixed" 时，生成中英混合格式（如 "villain，反派"），
    利用英文参考音频的声音克隆，让模型自然从英文过渡到中文，产生"外国人说中文"的效果。

    当 config.TTS_TEXT_FORMAT == "chinese_only" 时，生成纯中文格式（旧模式）。

    Args:
        group_indices: 组内 replacement 索引列表
        replacements: 完整的替换列表

    Returns:
        str: 拼接好的合成文本
    """
    text_format = getattr(config, "TTS_TEXT_FORMAT", "mixed")

    if text_format == "mixed":
        # 中英混合格式：每个词生成 "english，chinese"，多个词之间用逗号连接
        parts = []
        for idx in group_indices:
            r = replacements[idx]
            english = r["english"]
            chinese = r["chinese"]
            parts.append(f"{english}，{chinese}")
        return "，".join(parts)
    else:
        # 纯中文格式（旧模式）
        return "".join(replacements[idx]["chinese"] for idx in group_indices)


def _group_segments_into_turns(segments: list, same_speaker_gap: float = 0.3) -> list:
    """
    Group ALL consecutive segments into speaker turns based on inter-segment gaps.

    In conversation, consecutive segments from the same speaker typically have
    very short gaps (< same_speaker_gap, e.g., one person saying multiple
    sentences in a row). Gaps larger than this are likely speaker turns.

    Returns:
        list of groups, each group is a list of segment indices.
        e.g., [[0,1,2], [3], [4,5], [6], ...]
    """
    if not segments:
        return []

    groups = []
    current_group = [0]
    prev_end = segments[0].get("end", 0)

    for i in range(1, len(segments)):
        seg_start = segments[i].get("start", 0)
        gap = seg_start - prev_end

        if gap <= same_speaker_gap:
            # Very short gap: same speaker continuing in the same turn
            current_group.append(i)
        else:
            # Speaker turn
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
        min_ref = getattr(config, "SEGMENT_REF_MIN_DURATION", 0.3)
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


def group_adjacent_replacements(replacements: list) -> list:
    """
    检测并分组紧挨着的相邻替换点。

    如果两个替换点在时间上紧紧相邻（间隔小于 ADJACENT_MERGE_GAP），
    则合并为一个组，TTS 合成时将它们的中文翻译拼在一起作为一句话合成，
    避免独立合成导致的语调割裂。

    Args:
        replacements: 已按时间排序的替换列表
            [{start, end, chinese, english, type, segment_index}, ...]

    Returns:
        list[list[int]]: 分组结果，每组是 replacement 索引列表。
            例如 [[0], [1, 2], [3], [4, 5, 6]] 表示：
            - 索引 0 单独一组
            - 索引 1 和 2 是相邻词，合并为一组
            - 索引 3 单独一组
            - 索引 4, 5, 6 三个连续紧挨着，合并为一组
    """
    if not replacements:
        return []

    gap_threshold = getattr(config, "ADJACENT_MERGE_GAP", 0.12)

    groups = []
    current_group = [0]

    for i in range(1, len(replacements)):
        prev = replacements[i - 1]
        curr = replacements[i]

        # 判断是否紧挨着：
        # 1. 同一个 segment 内
        # 2. 前一个词的 end 到后一个词的 start 间隔很小
        gap = curr["start"] - prev["end"]

        if prev["segment_index"] == curr["segment_index"] and gap <= gap_threshold:
            current_group.append(i)
        else:
            groups.append(current_group)
            current_group = [i]

    groups.append(current_group)

    # 打印分组信息
    merged_groups = [g for g in groups if len(g) > 1]
    if merged_groups:
        print(f"[Step3-Qwen] 检测到 {len(merged_groups)} 组相邻词需要合并合成:")
        for g in merged_groups:
            words = " + ".join(
                f"{replacements[idx]['english']}({replacements[idx]['chinese']})"
                for idx in g
            )
            print(f"  合并: {words}")
    else:
        print(f"[Step3-Qwen] 没有检测到相邻词，全部独立合成")

    return groups


def synthesize_with_qwen_tts(replacements: list, ref_audio_map: dict,
                              segments: list, cache_dir: str,
                              adjacent_groups: list = None,
                              ref_source_map: dict = None,
                              cancel_check=None, progress_cb=None,
                              task_id: str = None) -> dict:
    """
    使用 Qwen3-TTS 声音克隆合成中文语音。
    支持相邻词合并合成：紧挨着的生词拼成一句话一起合成，语调更自然。

    Args:
        replacements: 替换列表 [{english, chinese, type, start, end, segment_index}, ...]
        ref_audio_map: {segment_index: ref_audio_path} 说话人参考音频映射
        segments: WhisperX segments 列表
        cache_dir: TTS 缓存目录
        adjacent_groups: 相邻词分组 [[idx, ...], ...]，None 则每个词独立
        ref_source_map: {segment_index: source_segment_index} ICL 模式下定位参考文本
        cancel_check: 终止检查回调
        progress_cb: 进度回调 (current, total)

    Returns:
        dict: {group_key: tts_audio_path} 映射
            - 单词组: group_key = "single_{replacement_index}"
            - 合并组: group_key = "merged_{first_index}_{last_index}"
    """
    os.makedirs(cache_dir, exist_ok=True)
    icl_mode = getattr(config, "QWEN3_TTS_ICL_MODE", False)
    mode_tag = "icl" if icl_mode else "xvec"

    if icl_mode:
        print(f"[Step3-Qwen] ⚡ ICL 模式：保留参考音频的语气和韵律特征")

    # 如果没有传入分组，则默认每个词独立一组
    if adjacent_groups is None:
        adjacent_groups = [[i] for i in range(len(replacements))]

    # 构建合成任务：每个组一个 job
    jobs = []
    for group in adjacent_groups:
        # 构建合成文本
        merged_text = _build_tts_text(group, replacements)

        # 用组内第一个替换点的 segment 来确定参考音频
        first_r = replacements[group[0]]
        seg_idx = first_r["segment_index"]
        ref_audio = ref_audio_map.get(seg_idx, "")
        if not ref_audio:
            print(f"  [警告] seg[{seg_idx}] 无参考音频，跳过 '{merged_text}'")
            continue

        # 确定参考文本：ICL 模式下需从实际参考音频来源 segment 获取文本
        if icl_mode and ref_source_map:
            source_seg = ref_source_map.get(seg_idx, seg_idx)
        else:
            source_seg = seg_idx
        ref_text = segments[source_seg].get("text", "").strip() if source_seg < len(segments) else ""

        # 构建 group_key
        if len(group) == 1:
            group_key = f"single_{group[0]}"
        else:
            group_key = f"merged_{group[0]}_{group[-1]}"

        # 缓存 key: 合成文本 + 参考音频 + 合成模式标识 + 文本格式标识
        text_format = getattr(config, "TTS_TEXT_FORMAT", "mixed")
        cache_key = hashlib.md5(
            f"{merged_text}|{ref_audio}|{mode_tag}|{text_format}".encode()
        ).hexdigest()[:12]
        output_path = os.path.join(cache_dir, f"qwen_tts_{cache_key}.wav")

        jobs.append({
            "text": merged_text,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "output_path": output_path,
            "segment_index": seg_idx,
            "_group_key": group_key,
            "_group": group,
        })

    if not jobs:
        print("[Step3-Qwen] 没有需要合成的任务")
        return {}

    # 过滤掉已有缓存的 job
    pending_jobs = []
    cached_results = {}
    for job in jobs:
        if os.path.exists(job["output_path"]):
            cached_results[job["_group_key"]] = job["output_path"]
            print(f"  [缓存] {job['text']} -> {os.path.basename(job['output_path'])}")
        else:
            pending_jobs.append(job)

    if not pending_jobs:
        print(f"[Step3-Qwen] 全部命中缓存 ({len(cached_results)} 条)")
        return cached_results

    # 去重：相同 output_path（即相同文本+相同参考音频）只合成一次
    # 但需要记住所有被去重 job 的 group_key -> output_path 映射
    seen_outputs = set()
    unique_pending = []
    dup_count = 0
    output_to_group_keys = {}  # output_path -> [group_key, ...]
    for job in pending_jobs:
        out = job["output_path"]
        gk = job["_group_key"]
        if out not in output_to_group_keys:
            output_to_group_keys[out] = []
        output_to_group_keys[out].append(gk)

        if out not in seen_outputs:
            seen_outputs.add(out)
            unique_pending.append(job)
        else:
            dup_count += 1

    if dup_count > 0:
        print(f"[Step3-Qwen] 去重: {len(pending_jobs)} -> {len(unique_pending)} "
              f"（{dup_count} 条重复文本跳过）")
    pending_jobs = unique_pending

    print(f"[Step3-Qwen] 需要合成 {len(pending_jobs)} 条"
          f"（缓存命中 {len(cached_results)} 条）")

    if cancel_check and cancel_check():
        raise InterruptedError("任务已被用户终止")

    # 构建 worker 任务 JSON
    icl_mode = getattr(config, "QWEN3_TTS_ICL_MODE", False)
    worker_task = {
        "model_path": config.QWEN3_TTS_MODEL_PATH,
        "device": getattr(config, "QWEN3_TTS_DEVICE", "cpu"),
        "language": getattr(config, "QWEN3_TTS_LANGUAGE", "Chinese"),
        "icl_mode": icl_mode,
        "jobs": [
            {
                "text": j["text"],
                "ref_audio": j["ref_audio"],
                "ref_text": j["ref_text"],
                "output_path": j["output_path"],
                "target_duration_s": j.get("target_duration_s"),
            }
            for j in unique_pending
        ],
    }

    # 写入临时文件
    task_file = os.path.join(cache_dir, "qwen_tts_task.json")
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(worker_task, f, ensure_ascii=False, indent=2)

    # 调用 worker 脚本
    worker_script = os.path.join(config.BASE_DIR, "workers", "qwen_tts_worker.py")
    python_bin = getattr(config, "QWEN3_TTS_PYTHON",
                         "/root/miniconda3/envs/qwen3-tts/bin/python")

    cmd = [python_bin, worker_script, task_file]
    print(f"[Step3-Qwen] 启动 worker: {' '.join(cmd)}")

    if progress_cb:
        progress_cb(0, len(pending_jobs))

    # 使用 Popen + 实时读取 stderr 以跟踪进度
    # 超时按每个任务最多 120 秒计算（CPU 模式下单词合成可能较慢）
    per_job_timeout = getattr(config, "QWEN3_TTS_PER_JOB_TIMEOUT", 120)
    total_timeout = max(600, len(pending_jobs) * per_job_timeout + 300)  # 至少 600s
    # 单条任务无输出的最大等待时间（防止 worker 卡住导致 stderr 阻塞）
    stall_timeout = max(per_job_timeout * 2, 300)
    print(f"[Step3-Qwen] 超时设置: 总计{total_timeout}s, 单条卡住{stall_timeout}s "
          f"({len(pending_jobs)} 任务 × {per_job_timeout}s/任务 + 300s 模型加载)")

    import select as _select
    import time as _time
    start_time = _time.time()
    last_output_time = start_time  # 上次收到 stderr 输出的时间
    done_count = 0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,  # 使用 bytes 模式以支持非阻塞读取
    )

    # 后台线程持续读取 stdout，防止管道缓冲区填满导致 worker 子进程死锁
    # PyTorch / Qwen3TTS 可能在模型加载时向 stdout 输出大量日志，
    # 如果管道缓冲区（默认 64KB）填满而父进程未读取，worker 将永久阻塞
    _stdout_chunks = []
    def _drain_stdout():
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                _stdout_chunks.append(chunk)
        except Exception:
            pass
    _stdout_reader = threading.Thread(target=_drain_stdout, daemon=True)
    _stdout_reader.start()

    # 注册 worker 进程到 task_subprocesses 以便用户终止时能杀掉
    if task_id:
        try:
            from core.task_manager import task_subprocesses
            task_subprocesses[task_id] = proc
        except Exception:
            pass

    # 实时读取 stderr 以监控进度和检测取消
    # 使用 select 实现带超时的读取，避免 for line in proc.stderr 的永久阻塞
    stderr_lines = []
    stderr_buffer = b""
    worker_stall_but_done = False  # 标记：worker 卡住但所有文件已生成
    try:
        while True:
            # 使用 select 等待 stderr 有数据可读，最多等待 5 秒
            ready, _, _ = _select.select([proc.stderr], [], [], 5.0)

            if ready:
                chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, 'read1') else proc.stderr.read(4096)
                if not chunk:
                    # stderr 关闭（进程即将结束）
                    break
                last_output_time = _time.time()
                stderr_buffer += chunk
                # 按行处理
                while b"\n" in stderr_buffer:
                    line_bytes, stderr_buffer = stderr_buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                    stderr_lines.append(line)
                    print(f"  {line}")

                    # 从 stderr 解析进度: "[QwenTTS] [3/79] 合成: ..."
                    if "[QwenTTS] [" in line and "/" in line:
                        try:
                            part = line.split("[QwenTTS] [")[1].split("]")[0]
                            current_str, total_str = part.split("/")
                            done_count = int(current_str)
                            if progress_cb:
                                progress_cb(done_count, len(pending_jobs))
                        except (ValueError, IndexError):
                            pass

            # 检查是否被用户取消
            if cancel_check and cancel_check():
                print("[Step3-Qwen] 用户取消，终止 worker 进程")
                proc.kill()
                proc.wait()
                raise InterruptedError("任务已被用户终止")

            # 检查总超时
            elapsed = _time.time() - start_time
            if elapsed > total_timeout:
                print(f"[Step3-Qwen] 总超时 ({elapsed:.0f}s > {total_timeout}s)，终止 worker")
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"Qwen3-TTS worker 总超时 ({elapsed:.0f}s)，"
                    f"已完成 {done_count}/{len(pending_jobs)} 条")

            # 检查单条卡住超时（长时间无任何输出）
            stall_elapsed = _time.time() - last_output_time
            if stall_elapsed > stall_timeout:
                # 检查是否所有任务已在磁盘上完成（worker 只是退出时卡住）
                disk_done = sum(1 for j in unique_pending if os.path.exists(j["output_path"]))
                if disk_done >= len(unique_pending):
                    print(f"[Step3-Qwen] Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                          f"但磁盘上已有 {disk_done}/{len(unique_pending)} 个文件，"
                          f"强制终止 worker 并继续")
                    proc.kill()
                    proc.wait()
                    worker_stall_but_done = True
                    break
                else:
                    print(f"[Step3-Qwen] Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                          f"已完成 {done_count}/{len(pending_jobs)} 条"
                          f"（磁盘 {disk_done}/{len(unique_pending)}），终止 worker")
                    proc.kill()
                    proc.wait()
                    raise RuntimeError(
                        f"Qwen3-TTS worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                        f"已完成 {done_count}/{len(pending_jobs)} 条")

            # 检查进程是否已退出（但 stderr 可能还有缓冲数据）
            if proc.poll() is not None and not ready:
                break

        # 处理 stderr_buffer 中剩余的不完整行
        if stderr_buffer:
            line = stderr_buffer.decode("utf-8", errors="replace").rstrip()
            if line:
                stderr_lines.append(line)
                print(f"  {line}")

        # 收集 stdout（后台线程已持续读取到 _stdout_chunks）
        proc.stderr.close()
        _stdout_reader.join(timeout=10)
        if not worker_stall_but_done:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stdout_data = b"".join(_stdout_chunks).decode("utf-8", errors="replace") if _stdout_chunks else ""
        else:
            stdout_data = ""

    except InterruptedError:
        raise
    except Exception as e:
        # 确保进程被清理
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        # 清理 task_subprocesses 注册
        if task_id:
            try:
                from core.task_manager import task_subprocesses
                task_subprocesses.pop(task_id, None)
            except Exception:
                pass

    # 尝试从 stdout JSON 解析结果
    result_json = None
    if not worker_stall_but_done:
        if proc.returncode != 0:
            print(f"[Step3-Qwen] Worker 失败 (code={proc.returncode})")
            stderr_tail = "\n".join(stderr_lines[-10:]) if stderr_lines else "无日志"
            raise RuntimeError(f"Qwen3-TTS worker 失败 (code={proc.returncode}):\n{stderr_tail}")

        try:
            stdout_lines = stdout_data.strip().split("\n") if stdout_data else []
            result_json = json.loads(stdout_lines[-1])
        except (json.JSONDecodeError, IndexError) as e:
            print(f"[Step3-Qwen] 解析 worker 输出失败: {e}")
            print(f"  stdout: {(stdout_data or '')[-300:]}")
            # 不再直接抛异常，尝试从磁盘回收结果
            result_json = None

    # 构建最终结果映射
    tts_map = dict(cached_results)  # 先放入缓存结果

    if result_json:
        success_count = result_json.get("success", 0)
        failed_count = result_json.get("failed", 0)
        print(f"[Step3-Qwen] Worker 完成: {success_count} 成功, {failed_count} 失败")

        worker_results = result_json.get("results", [])
        for wr, job in zip(worker_results, unique_pending):
            out_path = wr.get("output_path", "")
            if out_path and os.path.exists(out_path):
                for gk in output_to_group_keys.get(job["output_path"], [job["_group_key"]]):
                    tts_map[gk] = out_path
            elif wr.get("error"):
                print(f"  [失败] {job['text']}: {wr['error']}")
    else:
        # 从磁盘回收已生成的文件（worker 卡住或输出解析失败时的降级方案）
        print("[Step3-Qwen] 从磁盘回收已生成的合成文件...")
        disk_ok = 0
        disk_miss = 0
        for job in unique_pending:
            if os.path.exists(job["output_path"]):
                disk_ok += 1
                for gk in output_to_group_keys.get(job["output_path"], [job["_group_key"]]):
                    tts_map[gk] = job["output_path"]
            else:
                disk_miss += 1
                print(f"  [缺失] {job['text'][:30]}...")
        print(f"[Step3-Qwen] 磁盘回收: {disk_ok} 成功, {disk_miss} 缺失")
        if disk_miss > 0:
            print(f"[Step3-Qwen] 警告: {disk_miss} 条合成文件缺失，对应句段将无 TTS 音频")

    if progress_cb:
        progress_cb(len(pending_jobs), len(pending_jobs))

    return tts_map


def synthesize_sentences_with_qwen_tts(
    segments: list,
    translated_indices: list,
    translations: dict,
    audio_path: str,
    cache_dir: str,
    voice_clone: bool = True,
    cancel_check=None,
    progress_cb=None,
    task_id: str = None,
) -> dict:
    """
    为句子翻译模式合成中文 TTS 音频。
    每个被翻译的句子独立合成，使用对应英文句子的原声作为声音克隆参考。
    多说话人场景下，每个说话人的声音自动匹配。

    Args:
        segments: WhisperX segments 列表
        translated_indices: 需要翻译的 segment 索引列表
        translations: {segment_index: chinese_text} 翻译结果
        audio_path: 原始音频文件路径
        cache_dir: TTS 缓存目录
        voice_clone: 是否克隆原声音色（否则无参考音频合成）
        cancel_check: 终止检查回调
        progress_cb: 进度回调 (current, total)

    Returns:
        dict: {segment_index: tts_wav_path} 映射
    """
    os.makedirs(cache_dir, exist_ok=True)

    if not translated_indices or not translations:
        print("[Step3-Qwen-Sentence] 没有需要合成的句子")
        return {}

    print(f"[Step3-Qwen-Sentence] 准备为 {len(translated_indices)} 个句子合成中文 TTS")

    # 提取参考音频（每个句子用自己的原声，区分多说话人）
    ref_audio_map = {}
    ref_source_map = {}
    if voice_clone:
        # 检查是否有用户手动指定的高质量参考音频
        custom_ref = getattr(config, "QWEN3_TTS_CUSTOM_REF_AUDIO", "")
        if custom_ref and os.path.isfile(custom_ref):
            print(f"[Step3-Qwen-Sentence] 使用自定义参考音频: {custom_ref}")
            # 所有 segment 共用同一个高质量参考音频
            for idx in translated_indices:
                if idx < len(segments):
                    ref_audio_map[idx] = custom_ref
                    ref_source_map[idx] = idx
        else:
            ref_dir = os.path.join(cache_dir, "ref_audio")
            pseudo_replacements = [
                {"segment_index": idx}
                for idx in translated_indices
                if idx < len(segments)
            ]
            if pseudo_replacements:
                ref_audio_map, ref_source_map = extract_ref_audio_for_segments(
                    audio_path, segments, pseudo_replacements, ref_dir)
            print(f"[Step3-Qwen-Sentence] 提取了 {len(ref_audio_map)} 个句子参考音频")

    icl_mode = getattr(config, "QWEN3_TTS_ICL_MODE", False)
    mode_tag = "icl" if icl_mode else "xvec"

    # 计算原始说话人的平均语速（words/sec），用于节奏匹配
    original_wps = _estimate_original_speech_rate(segments)

    # 构建 TTS 合成任务（每个句子用自己的参考音频）
    jobs = []
    for seg_idx in translated_indices:
        chinese_text = translations.get(seg_idx, "")
        if not chinese_text:
            print(f"  [跳过] seg[{seg_idx}] 翻译文本为空，跳过 TTS 合成")
            continue

        ref_audio = ref_audio_map.get(seg_idx, "") if voice_clone else ""
        if voice_clone and not ref_audio:
            print(f"  [跳过] seg[{seg_idx}] 无参考音频，跳过 TTS 合成")
            continue
        # ICL 模式下从实际参考音频来源 segment 获取文本
        if icl_mode and ref_source_map:
            source_seg = ref_source_map.get(seg_idx, seg_idx)
        else:
            source_seg = seg_idx
        ref_text = segments[source_seg].get("text", "").strip() if source_seg < len(segments) else ""

        # 计算目标时长：基于原始英文 segment 的时长
        target_duration_s = None
        if seg_idx < len(segments):
            seg = segments[seg_idx]
            target_duration_s = round(seg.get("end", 0) - seg.get("start", 0), 1)

        cache_key = hashlib.md5(
            f"sent_{chinese_text}|{ref_audio}|{mode_tag}".encode()
        ).hexdigest()[:12]
        output_path = os.path.join(cache_dir, f"qwen_sent_{cache_key}.wav")

        jobs.append({
            "text": chinese_text,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "output_path": output_path,
            "segment_index": seg_idx,
            "target_duration_s": target_duration_s,
        })

    if not jobs:
        print("[Step3-Qwen-Sentence] 没有有效的合成任务")
        return {}

    # 检查缓存
    pending_jobs = []
    cached_results = {}
    for job in jobs:
        if os.path.exists(job["output_path"]):
            cached_results[job["segment_index"]] = job["output_path"]
            print(f"  [缓存] seg[{job['segment_index']}] {job['text'][:20]}...")
        else:
            pending_jobs.append(job)

    if not pending_jobs:
        print(f"[Step3-Qwen-Sentence] 全部命中缓存 ({len(cached_results)} 条)")
        return cached_results

    print(f"[Step3-Qwen-Sentence] 需合成 {len(pending_jobs)} 条"
          f"（缓存 {len(cached_results)} 条）")

    if cancel_check and cancel_check():
        raise InterruptedError("任务已被用户终止")

    # ---- 自动重试循环 ----
    tts_map = dict(cached_results)
    max_retries = getattr(config, "QWEN3_TTS_RETRY_MAX", 2)
    for retry_attempt in range(max_retries + 1):
        if retry_attempt > 0:
            print(f"[Step3-Qwen-Sentence] 第 {retry_attempt}/{max_retries} 次重试，"
                  f"剩余 {len(pending_jobs)} 条未合成")

        if not pending_jobs:
            print("[Step3-Qwen-Sentence] 全部合成完成，无需更多重试")
            break

        # 构建 worker 任务
        worker_task = {
            "model_path": config.QWEN3_TTS_MODEL_PATH,
            "device": getattr(config, "QWEN3_TTS_DEVICE", "cpu"),
            "language": getattr(config, "QWEN3_TTS_LANGUAGE", "Chinese"),
            "icl_mode": icl_mode,
            "jobs": [
                {"text": j["text"], "ref_audio": j["ref_audio"],
                 "ref_text": j["ref_text"], "output_path": j["output_path"],
                 "target_duration_s": j.get("target_duration_s")}
                for j in pending_jobs
            ],
        }

        task_file = os.path.join(cache_dir, f"qwen_sent_task_retry{retry_attempt}.json")
        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(worker_task, f, ensure_ascii=False, indent=2)

        worker_script = os.path.join(config.BASE_DIR, "workers", "qwen_tts_worker.py")
        python_bin = getattr(config, "QWEN3_TTS_PYTHON",
                             "/root/miniconda3/envs/qwen3-tts/bin/python")

        cmd = [python_bin, worker_script, task_file]
        print(f"[Step3-Qwen-Sentence] 启动 worker: {' '.join(cmd)}")

        if progress_cb:
            progress_cb(0, len(pending_jobs))

        import select as _select
        import time as _time
        per_job_timeout = getattr(config, "QWEN3_TTS_PER_JOB_TIMEOUT", 120)
        total_timeout = max(600, len(pending_jobs) * per_job_timeout + 300)
        stall_timeout = max(per_job_timeout * 2, 300)
        start_time = _time.time()
        last_output_time = start_time
        done_count = 0

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        # 后台线程持续读取 stdout，防止管道缓冲区填满导致 worker 子进程死锁
        _stdout_chunks_retry = []
        def _drain_stdout_retry():
            try:
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    _stdout_chunks_retry.append(chunk)
            except Exception:
                pass
        _stdout_reader_retry = threading.Thread(target=_drain_stdout_retry, daemon=True)
        _stdout_reader_retry.start()
        # 注册 worker 进程方便用户取消
        if task_id:
            try:
                from core.task_manager import task_subprocesses
                task_subprocesses[task_id] = proc
            except Exception:
                pass

        stderr_lines = []
        stderr_buffer = b""
        worker_stall_but_done = False
        this_attempt_error = None
        try:
            while True:
                ready, _, _ = _select.select([proc.stderr], [], [], 5.0)
                if ready:
                    chunk = (proc.stderr.read1(4096) if hasattr(proc.stderr, 'read1')
                             else proc.stderr.read(4096))
                    if not chunk:
                        break
                    last_output_time = _time.time()
                    stderr_buffer += chunk
                    while b"\n" in stderr_buffer:
                        line_bytes, stderr_buffer = stderr_buffer.split(b"\n", 1)
                        line = line_bytes.decode("utf-8", errors="replace").rstrip()
                        stderr_lines.append(line)
                        print(f"  {line}")
                        if "[QwenTTS] [" in line and "/" in line:
                            try:
                                part = line.split("[QwenTTS] [")[1].split("]")[0]
                                current_str, total_str = part.split("/")
                                done_count = int(current_str)
                                if progress_cb:
                                    progress_cb(done_count, len(pending_jobs))
                            except (ValueError, IndexError):
                                pass

                if cancel_check and cancel_check():
                    print("[Step3-Qwen-Sentence] 用户取消，终止 worker")
                    proc.kill()
                    proc.wait()
                    raise InterruptedError("任务已被用户终止")

                elapsed = _time.time() - start_time
                if elapsed > total_timeout:
                    print(f"[Step3-Qwen-Sentence] 总超时 ({elapsed:.0f}s)")
                    proc.kill()
                    proc.wait()
                    this_attempt_error = RuntimeError(
                        f"总超时 ({elapsed:.0f}s)，已完成 {done_count}/{len(pending_jobs)} 条")
                    break

                stall_elapsed = _time.time() - last_output_time
                if stall_elapsed > stall_timeout:
                    disk_done = sum(1 for j in pending_jobs if os.path.exists(j["output_path"]))
                    if disk_done >= len(pending_jobs):
                        print(f"[Step3-Qwen-Sentence] Worker 卡住但文件已全部生成，继续")
                        proc.kill()
                        proc.wait()
                        worker_stall_but_done = True
                        break
                    else:
                        print(f"[Step3-Qwen-Sentence] Worker 卡住 "
                              f"({stall_elapsed:.0f}s 无输出)，磁盘 {disk_done}/{len(pending_jobs)}")
                        proc.kill()
                        proc.wait()
                        this_attempt_error = RuntimeError(
                            f"Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                            f"已完成 {done_count}/{len(pending_jobs)} 条")
                        break

                if proc.poll() is not None and not ready:
                    break

            # 读取剩余 stderr
            if stderr_buffer:
                line = stderr_buffer.decode("utf-8", errors="replace").rstrip()
                if line:
                    stderr_lines.append(line)
                    print(f"  {line}")

            # 收集 stdout（后台线程已持续读取到 _stdout_chunks_retry）
            proc.stderr.close()
            _stdout_reader_retry.join(timeout=10)
            if not worker_stall_but_done:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                stdout_data = b"".join(_stdout_chunks_retry).decode("utf-8", errors="replace") if _stdout_chunks_retry else ""
            else:
                stdout_data = ""

        except InterruptedError:
            raise
        except Exception as e:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            this_attempt_error = e
        finally:
            if task_id:
                try:
                    from core.task_manager import task_subprocesses
                    task_subprocesses.pop(task_id, None)
                except Exception:
                    pass

        # ---- 收集本次尝试的结果 ----
        if not worker_stall_but_done:
            if proc.returncode != 0 and not this_attempt_error:
                this_attempt_error = RuntimeError(f"Worker 失败 (code={proc.returncode})")
            if not this_attempt_error:
                try:
                    stdout_lines = stdout_data.strip().split("\n") if stdout_data else []
                    result_json = json.loads(stdout_lines[-1])
                except (json.JSONDecodeError, IndexError):
                    result_json = None

        # 收集磁盘文件
        disk_ok, disk_miss = 0, 0
        for job in pending_jobs:
            if os.path.exists(job["output_path"]):
                disk_ok += 1
                tts_map[job["segment_index"]] = job["output_path"]
            else:
                disk_miss += 1

        if disk_ok > 0:
            print(f"[Step3-Qwen-Sentence] 本次收集: {disk_ok} 成功, {disk_miss} 缺失")

        # 更新剩余待合成任务
        pending_jobs = [j for j in pending_jobs if j["segment_index"] not in tts_map]

        # 如果全部成功或已无重试次数，退出循环
        if not pending_jobs or retry_attempt >= max_retries:
            if pending_jobs:
                print(f"[Step3-Qwen-Sentence] 重试次数耗尽，仍有 {len(pending_jobs)} 条未合成")
                for j in pending_jobs:
                    print(f"  [最终失败] seg[{j['segment_index']}] {j['text'][:30]}...")
            break

    if progress_cb:
        progress_cb(len(tts_map), len(tts_map))

    return tts_map


def build_tts_audio_map_for_replacements(replacements: list,
                                          tts_map: dict,
                                          adjacent_groups: list = None) -> dict:
    """
    将 group_key 映射转换为 step4 需要的替换索引映射。

    支持两种模式：
    - 有 adjacent_groups: 合并组用一个 TTS 音频替换整段
    - 无 adjacent_groups: 每个替换独立映射（兼容旧逻辑）

    Args:
        replacements: 替换列表
        tts_map: {group_key: tts_audio_path}
        adjacent_groups: 相邻词分组 [[idx, ...], ...]

    Returns:
        dict: {group_key: {"path": tts_audio_path, "indices": [idx, ...]}}
              供 step4 的 apply_replacements 使用
    """
    if adjacent_groups is None:
        adjacent_groups = [[i] for i in range(len(replacements))]

    index_map = {}
    for group in adjacent_groups:
        if len(group) == 1:
            group_key = f"single_{group[0]}"
        else:
            group_key = f"merged_{group[0]}_{group[-1]}"

        if group_key in tts_map:
            index_map[group_key] = {
                "path": tts_map[group_key],
                "indices": group,
            }

    return index_map
