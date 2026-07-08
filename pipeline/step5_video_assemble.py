"""
Step 5: 视频组装模块
将混音音频与视频合成，烧录双语字幕，输出最终配音视频。

功能:
1. 从 time_mapping + segments + translations 生成双语 SRT 字幕
2. 使用 ffmpeg 将 video + mixed_audio + subtitles 合成为最终 MP4
"""
import os
import subprocess
from typing import Optional


def _seconds_to_srt_time(seconds: float) -> str:
    """将秒数转换为 SRT 时间戳格式 HH:MM:SS,mmm。"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_bilingual_srt(
    segments: list[dict],
    translations: dict[int, str],
    time_mapping: list[dict],
    output_path: str,
    subtitle_mode: str = "bilingual",
) -> str:
    """
    从管道输出生成双语 SRT 字幕文件。

    Args:
        segments: WhisperX segments 列表 [{text, start, end, ...}]
        translations: {segment_index: chinese_text}
        time_mapping: Step 4b mix_sentence_audio 返回的 time_mapping
        output_path: SRT 输出文件路径
        subtitle_mode: "bilingual" / "chinese_only" / "english_only"

    Returns:
        str: SRT 文件路径（失败返回空字符串）
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # 构建 TTS 段的时间映射: seg_idx → mixed_start, mixed_end
    tts_time_map = {}
    for entry in time_mapping:
        if entry.get("type") == "tts_chinese" and entry.get("segment_index", -1) >= 0:
            sidx = entry["segment_index"]
            tts_time_map[sidx] = {
                "start": entry["mixed_start"],
                "end": entry["mixed_end"],
            }

    # 构建原始段时间映射（从 time_mapping 的 original 类型获取，或使用 segments 原始时间）
    orig_time_map = {}
    for entry in time_mapping:
        if entry.get("type") == "original" and entry.get("segment_index", -1) >= 0:
            sidx = entry["segment_index"]
            orig_time_map[sidx] = {
                "start": entry.get("orig_start", entry["mixed_start"]),
                "end": entry.get("orig_end", entry["mixed_end"]),
            }

    entries = []
    for idx, seg in enumerate(segments):
        eng_text = seg.get("text", "").strip()
        chn_text = translations.get(idx, "").strip()
        if not eng_text and not chn_text:
            continue

        # 确定时间：优先用 time_mapping 中的混合时间
        if idx in tts_time_map:
            start = tts_time_map[idx]["start"]
            end = tts_time_map[idx]["end"]
        elif idx in orig_time_map:
            start = orig_time_map[idx]["start"]
            end = orig_time_map[idx]["end"]
        else:
            start = seg.get("start", 0)
            end = seg.get("end", 0)

        if end <= start:
            continue

        entries.append({
            "index": len(entries) + 1,
            "start": start,
            "end": end,
            "english": eng_text,
            "chinese": chn_text,
        })

    if not entries:
        return ""

    # 生成 SRT
    lines = []
    for entry in entries:
        lines.append(str(entry["index"]))
        lines.append(f"{_seconds_to_srt_time(entry['start'])} --> "
                     f"{_seconds_to_srt_time(entry['end'])}")

        if subtitle_mode == "bilingual":
            if entry["english"] and entry["chinese"]:
                lines.append(f"{entry['english']}\n{entry['chinese']}")
            elif entry["chinese"]:
                lines.append(entry["chinese"])
            else:
                lines.append(entry["english"])
        elif subtitle_mode == "chinese_only":
            lines.append(entry["chinese"] or entry["english"])
        else:  # english_only
            lines.append(entry["english"] or "")

        lines.append("")  # 空行分隔

    srt_content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    print(f"[Step5] 生成字幕: {len(entries)} 条 → {output_path}")
    return output_path


def assemble_video(
    video_path: str,
    mixed_audio_path: str,
    srt_path: str,
    output_path: str,
    subtitle_style: Optional[str] = None,
    timeout: int = 1800,
) -> str:
    """
    将视频、混音音频、字幕合成为最终 MP4。

    策略:
    - 使用 ffmpeg subtitles 滤镜烧录字幕到视频帧
    - 替换原始音轨为混合音频
    - -shortest 以较短的流为准（通常混音 ≤ 原视频）
    - 如果混音音频长于视频，补最后一帧静态画面

    Args:
        video_path: 原始视频文件
        mixed_audio_path: Step 4b 输出的混合音频
        srt_path: SRT 字幕文件
        output_path: 输出 MP4 文件路径
        subtitle_style: ASS 样式字符串（可选）
        timeout: ffmpeg 超时（秒）

    Returns:
        str: 输出文件路径（失败返回空字符串）
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if not subtitle_style:
        subtitle_style = (
            "FontName=Arial,"
            "FontSize=20,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,"
            "Outline=2,"
            "Shadow=1,"
            "MarginV=40"
        )

    # ffmpeg 组装:
    # - 视频流来自 video_path，烧录字幕
    # - 音频流来自 mixed_audio_path
    # - -shortest 保证以短流为准
    # - 音频需要重采样到 AAC 以兼容 MP4

    # subtitles 滤镜需要视频解码，使用 libx264 编码
    srt_abs = os.path.abspath(srt_path)
    # Windows 路径需要转义
    srt_escaped = srt_abs.replace("\\", "/").replace(":", "\\:")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video_path,
        "-i", mixed_audio_path,
        "-vf", f"subtitles='{srt_escaped}':force_style='{subtitle_style}'",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "192k",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]

    print(f"[Step5] 开始视频组装...")
    print(f"  视频: {os.path.basename(video_path)}")
    print(f"  音频: {os.path.basename(mixed_audio_path)}")
    print(f"  字幕: {os.path.basename(srt_path)}")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        if proc.returncode != 0:
            # 可能是路径转义问题，尝试不转义
            print(f"[Step5] 首次组装失败，尝试备用方式...")
            cmd2 = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", video_path,
                "-i", mixed_audio_path,
                "-vf", f"subtitles={srt_abs}:force_style='{subtitle_style}'",
                "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest", "-movflags", "+faststart",
                output_path,
            ]
            proc = subprocess.run(cmd2, capture_output=True, text=True, timeout=timeout)

            if proc.returncode != 0:
                print(f"[Step5] 组装失败: {proc.stderr.strip()[-500:]}")
                return ""

    except subprocess.TimeoutExpired:
        print(f"[Step5] 组装超时 ({timeout}s)")
        return ""

    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[Step5] 组装完成: {os.path.basename(output_path)} ({size_mb:.1f} MB)")
        return output_path

    return ""
