"""
Step 5: 视频组装模块
将混音音频与视频合成，烧录双语字幕，输出最终配音视频。

策略：
  1. 生成 ASS 字幕文件（含字体/描边/位置样式），时间戳对齐混合后的音频时间轴
  2. 用 ffmpeg 的 ass= 滤镜（libass 引擎）一次性烧录全部字幕到视频画面
     —— 不再使用逐条 PNG 渲染 + 链式 overlay，避免字幕数量增多时
        filter graph 呈线性/超线性增长导致的 CPU 占满、处理时间暴涨问题
  3. 与混合音频合并，按用户规则对齐时长：
     - 混音 ≤ 原视频 → 截断视频到混音时长
     - 混音 > 原视频 → tpad=clone 延长末帧
  4. 限制 ffmpeg 编码线程数，避免抢占全部 CPU 核心拖慢整机
"""
import os
import re
import subprocess
import shutil
import tempfile
from typing import Optional

# 限制 ffmpeg 使用的线程数（避免抢占所有 CPU 核心导致系统卡顿）
_FFMPEG_THREADS = str(max(1, min(4, os.cpu_count() or 4)))


# ============================================================
# 字幕渲染
# ============================================================

def _parse_ass_dialogue_count(ass_path: str) -> int:
    """统计 ASS 字幕文件中的 Dialogue 条目数量（用于判断是否存在有效字幕）。"""
    if not os.path.isfile(ass_path):
        return 0
    count = 0
    with open(ass_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("Dialogue:"):
                count += 1
    return count


# ============================================================
# ASS 字幕生成（支持中英文分色 + 底部对齐）
# ============================================================

def _seconds_to_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds - int(seconds)) * 100)  # ASS 用百分之一秒
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


# ASS 颜色格式为 &HBBGGRR&（BGR 顺序，与 SRT/HTML 的 RGB 相反）
_ASS_COLOR_ENGLISH = "&H00E6E6E6&"   # 浅灰白：英文原文
_ASS_COLOR_CHINESE = "&H0000D7FF&"   # 金黄色：中文翻译，与英文形成区分


def generate_bilingual_srt(
    segments: list[dict],
    translations: dict[int, str],
    time_mapping: list[dict],
    output_path: str,
    subtitle_mode: str = "bilingual",
    video_height: int = 720,
) -> str:
    """
    生成 ASS 字幕文件（尽管函数名/参数保留 srt 命名以兼容旧调用方，
    实际输出内容为 ASS 格式，支持行内颜色标签 + 精确对齐控制）。

    英文行使用浅灰白色，中文行使用金黄色，视觉上清晰区分两种语言。
    字幕固定底部居中对齐（Alignment=2），边距按视频高度百分比计算，
    避免不同分辨率下字幕位置视觉不一致。
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    tts_time_map = {}
    for entry in time_mapping:
        if entry.get("type") == "tts_chinese" and entry.get("segment_index", -1) >= 0:
            tts_time_map[entry["segment_index"]] = {
                "start": entry["mixed_start"],
                "end": entry["mixed_end"],
            }
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
        eng = seg.get("text", "").strip()
        chn = translations.get(idx, "").strip()
        if not eng and not chn:
            continue
        if idx in tts_time_map:
            start, end = tts_time_map[idx]["start"], tts_time_map[idx]["end"]
        elif idx in orig_time_map:
            start, end = orig_time_map[idx]["start"], orig_time_map[idx]["end"]
        else:
            start, end = seg.get("start", 0), seg.get("end", 0)
        if end <= start:
            continue
        entries.append({"index": len(entries) + 1, "start": start, "end": end,
                        "english": eng, "chinese": chn})

    if not entries:
        return ""

    font_size = min(max(28, video_height // 28), 80)   # 限制最大字号并降低比例
    margin_v = max(30, int(video_height * 0.07))
    # 双语模式下英文上/中文下，margin 固定不随分辨率跳动
    margin_en = margin_v + int(font_size * 1.4)
    margin_cn = margin_v

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: {video_height if video_height >= 1080 else 1080}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: English,Noto Sans CJK SC,{font_size},{_ASS_COLOR_ENGLISH},&H000000FF&,&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,1,2.5,0,2,20,20,{margin_en},1
Style: Chinese,Noto Sans CJK SC,{font_size},{_ASS_COLOR_CHINESE},&H000000FF&,&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,1,2.5,0,2,20,20,{margin_cn},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    for e in entries:
        start_ts = _seconds_to_ass_time(e["start"])
        end_ts = _seconds_to_ass_time(e["end"])
        eng_text = e["english"].replace("\n", "\\N") if e["english"] else ""
        chn_text = e["chinese"].replace("\n", "\\N") if e["chinese"] else ""
        if subtitle_mode == "bilingual":
            if eng_text and chn_text:
                # 英文在上/中文在下，顺序固定，加 q0 自动换行
                lines.append(f"Dialogue: 0,{start_ts},{end_ts},English,,0,0,0,,{{\\q0}}{eng_text}")
                lines.append(f"Dialogue: 0,{start_ts},{end_ts},Chinese,,0,0,0,,{{\\q0}}{chn_text}")
            else:
                text = chn_text or eng_text
                style = "Chinese" if chn_text else "English"
                lines.append(f"Dialogue: 0,{start_ts},{end_ts},{style},,0,0,0,,{{\\q0}}{text}")
        elif subtitle_mode == "chinese_only":
            text = chn_text or eng_text
            lines.append(f"Dialogue: 0,{start_ts},{end_ts},Chinese,,0,0,0,,{{\\q0}}{text}")
        else:
            if eng_text:
                lines.append(f"Dialogue: 0,{start_ts},{end_ts},English,,0,0,0,,{{\\q0}}{eng_text}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[Step5] 生成 ASS 字幕: {len(entries)} 条 → {output_path}")
    return output_path

# ============================================================
# 视频组装：字幕烧录（libass 一次性渲染）+ 音视频对齐合并
# ============================================================

def _probe_video_size(video_path: str) -> tuple[int, int]:
    cmd = ["ffprobe", "-v", "error",
           "-select_streams", "v:0",
           "-show_entries", "stream=width,height",
           "-of", "csv=s=x:p=0",
           video_path]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=30).strip()
        w, h = out.split("x")
        return int(w), int(h)
    except Exception:
        return 1280, 720


def _probe_video_duration(video_path: str) -> float:
    cmd = ["ffprobe", "-v", "error",
           "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1",
           video_path]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=30).strip()
        return float(out)
    except Exception:
        return 0.0


def _escape_path_for_filter(path: str) -> str:
    """转义路径中的特殊字符，用于嵌入 ffmpeg filter 参数（单引号包裹）。"""
    path = os.path.abspath(path)
    path = path.replace("\\", "\\\\")
    path = path.replace(":", "\\:")
    path = path.replace("'", "\\'")
    return path


def assemble_video(
    video_path: str,
    mixed_audio_path: str,
    srt_path: str,
    output_path: str,
    subtitle_style: Optional[str] = None,
    timeout: int = 1800,
) -> str:
    """
    视频组装（单次 ffmpeg 调用）：
      1. 用 ass= 滤镜（libass 引擎）一次性烧录全部字幕 —— 样式（字体/颜色/位置）
         已内嵌在 ASS 文件的 [V4+ Styles] 中，无需 force_style 覆盖；
         无论字幕条数多少，都是一次线性扫描，不会随字幕数量增长而变慢
      2. 同一条 filter 链内按需 tpad 延长末帧（混音 > 原视频时）
      3. 替换音轨为混合音频，按需 -shortest 截断（混音 ≤ 原视频时）
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    if os.path.isfile(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass

    dialogue_count = _parse_ass_dialogue_count(srt_path)
    video_duration = _probe_video_duration(video_path)
    mixed_duration = _probe_video_duration(mixed_audio_path)
    print(f"[Step5] 视频时长: {video_duration:.2f}s  混音时长: {mixed_duration:.2f}s  字幕数: {dialogue_count}")

    has_srt = dialogue_count > 0
    diff = video_duration - mixed_duration  # >0: 视频更长(需截断) <0: 视频更短(需延长末帧)

    vf_parts = []
    if diff < -0.3:
        print(f"[Step5] 视频 < 混音 ({video_duration:.2f}s < {mixed_duration:.2f}s) -> 延长末帧")
        vf_parts.append(f"tpad=stop_mode=clone:stop_duration={abs(diff):.3f}")
    elif diff > 0.3:
        print(f"[Step5] 视频 > 混音 ({video_duration:.2f}s > {mixed_duration:.2f}s) -> 截断")

    if has_srt:
        escaped_ass = _escape_path_for_filter(srt_path)
        vf_parts.append(f"ass=filename='{escaped_ass}'")
        print(f"[Step5] 字幕烧录: {dialogue_count} 条 (libass ass= 滤镜，样式已内嵌)")
    else:
        print("[Step5] 无字幕，纯拼接音视频")

    vf_parts.append("format=yuv420p")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-threads", _FFMPEG_THREADS,
        "-i", video_path, "-i", mixed_audio_path,
        "-vf", ",".join(vf_parts),
        "-c:v", "libx264", "-b:v", "1500k",
        "-profile:v", "high", "-level", "5.0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-map", "0:v:0", "-map", "1:a:0",
    ]
    if diff > 0.3:
        cmd.append("-shortest")
    cmd += ["-movflags", "+faststart", output_path]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        print(f"[Step5] 失败: {r.stderr.strip()[-500:]}")
        return ""

    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        final_dur = _probe_video_duration(output_path)
        print(f"[Step5] 组装完成: {os.path.basename(output_path)} ({size_mb:.1f} MB, {final_dur:.2f}s)")
        return output_path
    return ""
