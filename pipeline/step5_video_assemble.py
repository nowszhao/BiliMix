"""
Step 5: 视频组装模块
将混音音频与视频合成，烧录双语字幕，输出最终配音视频。

策略：
  1. 生成 ASS 字幕文件（含字体/描边/位置样式），时间戳对齐混合后的音频时间轴
  2. 逐句从原视频裁剪画面，按 TTS 时长变速（setpts），拼接后一次性烧录字幕
     —— 使用 -/filter_complex 从文件读取 filter graph，支持数百个 segment
  3. 与混合音频合并，替换音轨
  4. 限制 ffmpeg 编码线程数，避免抢占全部 CPU 核心拖慢整机
"""
import os
import re
import subprocess
import shutil
import tempfile
from typing import Optional, List

from core import config

# 限制 ffmpeg 使用的线程数（避免抢占所有 CPU 核心导致系统卡顿）
_FFMPEG_THREADS_CAP = getattr(config, "FFMPEG_THREADS_CAP", 4)
_FFMPEG_THREADS = str(max(1, min(_FFMPEG_THREADS_CAP, os.cpu_count() or 4)))

# 编码器探测缓存（启动时探测一次，后续复用）
_ENCODER_CACHE: Optional[dict] = None


def _detect_encoders() -> dict:
    """探测系统可用的视频/音频编码器，按优先级回退，结果缓存。"""
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE

    try:
        out = subprocess.check_output(
            ["ffmpeg", "-encoders"], text=True, timeout=10, stderr=subprocess.DEVNULL
        )
    except Exception:
        _ENCODER_CACHE = {"video": None, "audio": None}
        return _ENCODER_CACHE

    # 视频编码器优先级：libx264 > libopenh264（两者均输出浏览器兼容的 H.264）
    for enc in ["libx264", "libopenh264"]:
        if enc in out:
            _ENCODER_CACHE = {"video": enc, "audio": "aac"}
            return _ENCODER_CACHE

    # 无可用 H.264 编码器 → 记录为 None，后续报错提示用户安装
    _ENCODER_CACHE = {"video": None, "audio": "aac"}
    return _ENCODER_CACHE


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
    video_height: Optional[int] = None,
) -> str:
    """
    生成 ASS 字幕文件（尽管函数名/参数保留 srt 命名以兼容旧调用方，
    实际输出内容为 ASS 格式，支持行内颜色标签 + 精确对齐控制）。

    英文行使用浅灰白色，中文行使用金黄色，视觉上清晰区分两种语言。
    字幕固定底部居中对齐（Alignment=2），边距按视频高度百分比计算，
    避免不同分辨率下字幕位置视觉不一致。
    """
    if video_height is None:
        video_height = getattr(config, "ASS_DEFAULT_VIDEO_HEIGHT", 720)
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

    font_size_min = getattr(config, "ASS_FONT_SIZE_MIN", 28)
    font_size_max = getattr(config, "ASS_FONT_SIZE_MAX", 80)
    font_size = min(max(font_size_min, video_height // 28), font_size_max)
    margin_v_min = getattr(config, "ASS_MARGIN_V_MIN", 30)
    margin_v_ratio = getattr(config, "ASS_MARGIN_V_RATIO", 0.07)
    margin_v = max(margin_v_min, int(video_height * margin_v_ratio))
    margin_en_ratio = getattr(config, "ASS_MARGIN_EN_RATIO", 1.4)
    margin_en = margin_v + int(font_size * margin_en_ratio)
    margin_cn = margin_v

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: {video_height if video_height >= 1080 else 1080}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: English,Noto Sans CJK SC,{font_size},{_ASS_COLOR_ENGLISH},&H000000FF&,&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,1,{getattr(config, "ASS_OUTLINE", 2.5)},{getattr(config, "ASS_SHADOW", 0)},2,20,20,{margin_en},1
Style: Chinese,Noto Sans CJK SC,{font_size},{_ASS_COLOR_CHINESE},&H000000FF&,&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,1,{getattr(config, "ASS_OUTLINE", 2.5)},{getattr(config, "ASS_SHADOW", 0)},2,20,20,{margin_cn},1

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


# ============================================================
# 逐句变速 filter graph 生成
# ============================================================

def _build_filter_graph(
    time_mapping: list,
    srt_path: str = None,
) -> str:
    """
    从 time_mapping 生成逐句 trim + setpts + concat 的 filter graph。

    对每个 tts_chinese 条目：
      - 从原视频裁剪对应时间段（trim）
      - 按混音时长/原始时长比例变速（setpts）
      - 拼接所有片段（concat）
      - 可选烧录字幕（ass）

    filter graph 写入临时文件，通过 -/filter_complex 传给 ffmpeg，
    避免命令行长度限制。

    Args:
        time_mapping: mix_sentence_audio 返回的 time_mapping 列表
        srt_path: ASS 字幕文件路径，None 则不烧录字幕

    Returns:
        str: filter graph 临时文件路径
    """
    # 只处理 type=="tts_chinese" 的条目
    tts_entries = [
        e for e in time_mapping
        if e.get("type") == "tts_chinese" and e.get("orig_end", 0) > e.get("orig_start", 0)
    ]
    if not tts_entries:
        return None

    parts = []
    for i, entry in enumerate(tts_entries):
        orig_start = entry["orig_start"]
        orig_end = entry["orig_end"]
        mixed_start = entry["mixed_start"]
        mixed_end = entry["mixed_end"]

        orig_dur = orig_end - orig_start
        mixed_dur = mixed_end - mixed_start
        if orig_dur <= 0 or mixed_dur <= 0:
            continue

        speed_ratio = mixed_dur / orig_dur
        # PTS-STARTPTS 重置时间戳（trim 输出仍带原始 PTS），
        # 然后用 speed_ratio 变速，format 统一像素格式
        parts.append(
            f"[0:v]trim=start={orig_start:.3f}:duration={orig_dur:.3f},"
            f"setpts=PTS-STARTPTS,"
            f"setpts={speed_ratio:.6f}*PTS,"
            f"format=yuv420p[v{i}]"
        )

    if not parts:
        return None

    n = len(parts)
    # 拼接所有视频片段
    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    concat_part = f"{concat_inputs}concat=n={n}:v=1:a=0"

    # 可选字幕烧录
    if srt_path and os.path.isfile(srt_path):
        escaped_ass = _escape_path_for_filter(srt_path)
        graph = ";\n".join(parts) + f";\n{concat_part},ass=filename='{escaped_ass}'[outv]"
    else:
        graph = ";\n".join(parts) + f";\n{concat_part}[outv]"

    # 写入临时文件
    fd, graph_path = tempfile.mkstemp(suffix=".txt", prefix="bilimix_filter_")
    with os.fdopen(fd, "w") as f:
        f.write(graph)

    print(f"[Step5] 生成 filter graph: {n} 个 segment, "
          f"{len(graph)} 字节 → {graph_path}")
    return graph_path


def assemble_video(
    video_path: str,
    mixed_audio_path: str,
    srt_path: str,
    output_path: str,
    subtitle_style: Optional[str] = None,
    timeout: Optional[int] = None,
    time_mapping: Optional[list] = None,
) -> str:
    """
    视频组装（单次 ffmpeg 调用）：

    当提供 time_mapping 时，使用逐句 trim+setpts+concat 变速拼接画面，
    使画面与混音音频/字幕精确对齐。

    当 time_mapping 为空时，回退到简单模式：ass= 字幕烧录 + 音轨替换。
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    if timeout is None:
        timeout = getattr(config, "VIDEO_ASSEMBLE_TIMEOUT", 1800)

    if os.path.isfile(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass

    dialogue_count = _parse_ass_dialogue_count(srt_path)
    video_duration = _probe_video_duration(video_path)
    mixed_duration = _probe_video_duration(mixed_audio_path)
    has_srt = dialogue_count > 0

    # ---- 尝试生成逐句变速 filter graph ----
    filter_graph_path = None
    if time_mapping and has_srt:
        filter_graph_path = _build_filter_graph(time_mapping, srt_path)

    encoders = _detect_encoders()
    video_enc = encoders.get("video")
    audio_enc = encoders.get("audio", "aac")

    if not video_enc:
        print("[Step5] 失败: 未找到可用的 H.264 视频编码器（libx264 / libopenh264）")
        print("[Step5] 请安装带 H.264 编码支持的 FFmpeg：")
        print("[Step5]   CentOS 8+:  dnf install https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm && dnf install ffmpeg")
        print("[Step5]   其他发行版: apt install ffmpeg / 或从 https://ffmpeg.org 编译安装")
        return ""

    video_bitrate = getattr(config, "VIDEO_BITRATE", "1500k")
    audio_bitrate = getattr(config, "VIDEO_AUDIO_BITRATE", "128k")
    audio_sample_rate = getattr(config, "VIDEO_AUDIO_SAMPLE_RATE", 44100)
    audio_channels = getattr(config, "VIDEO_AUDIO_CHANNELS", 2)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-threads", _FFMPEG_THREADS,
        "-i", video_path, "-i", mixed_audio_path,
    ]

    if filter_graph_path:
        # 变速模式：使用 -/filter_complex 从文件读取 filter graph
        print(f"[Step5] 视频时长: {video_duration:.2f}s  混音时长: {mixed_duration:.2f}s  字幕数: {dialogue_count}")
        print(f"[Step5] 逐句变速模式: {len([e for e in time_mapping if e.get('type') == 'tts_chinese'])} 个 TTS 句段")
        cmd += ["-/filter_complex", filter_graph_path]
        cmd += ["-map", "[outv]", "-map", "1:a:0"]
    else:
        # 回退模式：简单 ass= 字幕烧录
        diff = video_duration - mixed_duration
        diff_tolerance = getattr(config, "VIDEO_DURATION_TOLERANCE", 0.3)
        print(f"[Step5] 视频时长: {video_duration:.2f}s  混音时长: {mixed_duration:.2f}s  字幕数: {dialogue_count}")
        print(f"[Step5] 回退模式: 简单字幕烧录 + 音轨替换")

        vf_parts = []
        if diff < -diff_tolerance:
            print(f"[Step5] 视频 < 混音 -> 延长末帧")
            vf_parts.append(f"tpad=stop_mode=clone:stop_duration={abs(diff):.3f}")
        elif diff > diff_tolerance:
            print(f"[Step5] 视频 > 混音 -> 截断")

        if has_srt:
            escaped_ass = _escape_path_for_filter(srt_path)
            vf_parts.append(f"ass=filename='{escaped_ass}'")
        vf_parts.append("format=yuv420p")

        cmd += ["-vf", ",".join(vf_parts)]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        if diff > diff_tolerance:
            cmd.append("-shortest")

    cmd += [
        "-c:v", video_enc, "-b:v", video_bitrate,
    ]
    if video_enc == "libx264":
        cmd += ["-profile:v", "high", "-level", "5.0"]
    cmd += [
        "-pix_fmt", "yuv420p",
        "-c:a", audio_enc, "-b:a", audio_bitrate,
        "-ar", str(audio_sample_rate), "-ac", str(audio_channels),
        "-movflags", "+faststart", output_path,
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # 清理临时 filter graph 文件
    if filter_graph_path and os.path.exists(filter_graph_path):
        try:
            os.remove(filter_graph_path)
        except OSError:
            pass

    if r.returncode != 0:
        print(f"[Step5] 失败: {r.stderr.strip()[-500:]}")
        return ""

    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        final_dur = _probe_video_duration(output_path)
        print(f"[Step5] 组装完成: {os.path.basename(output_path)} ({size_mb:.1f} MB, {final_dur:.2f}s)")
        return output_path
    return ""
