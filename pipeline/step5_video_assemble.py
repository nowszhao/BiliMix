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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict

from core import config

# 限制 ffmpeg 使用的线程数（避免抢占所有 CPU 核心导致系统卡顿）
_FFMPEG_THREADS_CAP = getattr(config, "FFMPEG_THREADS_CAP", 4)
_FFMPEG_THREADS = str(max(1, min(_FFMPEG_THREADS_CAP, os.cpu_count() or 4)))

# 编码器探测缓存（启动时探测一次，后续复用）
_ENCODER_CACHE: Optional[dict] = None


def _detect_encoders() -> dict:
    """探测系统可用的视频/音频编码器。

    视频编码器强制要求 libx264（libopenh264 编码太慢，会导致超时）。
    不可用时打印具体平台的安装指引并返回 None。
    """
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE

    try:
        enc_out = subprocess.check_output(
            ["ffmpeg", "-encoders"], text=True, timeout=10, stderr=subprocess.DEVNULL
        )
    except Exception:
        _ENCODER_CACHE = {"video": None, "audio": None}
        return _ENCODER_CACHE

    has_x264 = "libx264" in enc_out
    has_openh264 = "libopenh264" in enc_out
    video_enc = "libx264" if has_x264 else None

    if not video_enc:
        print("[Step5] ═══════════════════════════════════════════")
        print("[Step5] 错误: 未找到 libx264 视频编码器")
        print("[Step5]")
        if has_openh264:
            print("[Step5] 检测到 libopenh264，但该编码器性能不足，")
            print("[Step5] 逐句变速模式下极易超时（编码速度慢 2-3 倍）。")
            print("[Step5]")
        print("[Step5] 请安装带 libx264 的 FFmpeg:")
        print("[Step5]   Ubuntu/Debian:  apt install ffmpeg")
        print("[Step5]   CentOS/RHEL 8+: yum install epel-release && yum install ffmpeg")
        print("[Step5]   macOS:          brew install ffmpeg")
        print("[Step5]   conda:          conda install -c conda-forge ffmpeg")
        print("[Step5] ═══════════════════════════════════════════")

    _ENCODER_CACHE = {"video": video_enc, "audio": "aac"}
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
    time_offset: float = 0.0,
    subtitle_font_size: Optional[int] = None,
) -> str:
    """
    生成 ASS 字幕文件（尽管函数名/参数保留 srt 命名以兼容旧调用方，
    实际输出内容为 ASS 格式，支持行内颜色标签 + 精确对齐控制）。

    英文行使用浅灰白色，中文行使用金黄色，视觉上清晰区分两种语言。
    字幕固定底部居中对齐（Alignment=2），边距按视频高度百分比计算，
    避免不同分辨率下字幕位置视觉不一致。

    time_offset: 用于分块模式，所有时间戳减去该偏移量（块内音频从 0 开始）。
    """
    if video_height is None:
        video_height = getattr(config, "ASS_DEFAULT_VIDEO_HEIGHT", 720)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    tts_time_map = {}
    for entry in time_mapping:
        if entry.get("type") == "tts_chinese" and entry.get("segment_index", -1) >= 0:
            tts_time_map[entry["segment_index"]] = {
                "start": max(0, entry["mixed_start"] - time_offset),
                "end": max(0, entry["mixed_end"] - time_offset),
            }
    orig_time_map = {}
    for entry in time_mapping:
        if entry.get("type") == "original" and entry.get("segment_index", -1) >= 0:
            sidx = entry["segment_index"]
            orig_time_map[sidx] = {
                "start": max(0, entry.get("orig_start", entry["mixed_start"]) - time_offset),
                "end": max(0, entry.get("orig_end", entry["mixed_end"]) - time_offset),
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
            start = max(0, seg.get("start", 0) - time_offset)
            end = max(0, seg.get("end", 0) - time_offset)
        if end <= start:
            continue
        entries.append({"index": len(entries) + 1, "start": start, "end": end,
                        "english": eng, "chinese": chn})

    if not entries:
        return ""

    font_size_min = getattr(config, "ASS_FONT_SIZE_MIN", 28)
    font_size_max = getattr(config, "ASS_FONT_SIZE_MAX", 80)
    if subtitle_font_size is not None:
        font_size = int(subtitle_font_size)
        font_size = max(getattr(config, "ASS_FONT_SIZE_USER_MIN", 14), font_size)
        font_size = min(font_size, getattr(config, "ASS_FONT_SIZE_USER_MAX", 120))
    else:
        font_size = min(max(font_size_min, video_height // 22), font_size_max)
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
                # 英文在上/中文在下，顺序固定
                # 英文: q0=按词换行  中文: q1=按字符换行（中文无空格需q1才能换行）
                lines.append(f"Dialogue: 0,{start_ts},{end_ts},English,,0,0,0,,{{\\q0}}{eng_text}")
                lines.append(f"Dialogue: 0,{start_ts},{end_ts},Chinese,,0,0,0,,{{\\q1}}{chn_text}")
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


def _apply_watermark(video_path: str) -> str:
    """对视频叠加半透明文字水印（drawtext 滤镜）。

    水印位置为右下角，带轻微描边以保证深浅背景均可读。
    配置项: WATERMARK_ENABLED, WATERMARK_TEXT, WATERMARK_OPACITY
    返回带水印的新文件路径，或原路径（水印禁用时）。
    """
    enabled = getattr(config, "WATERMARK_ENABLED", True)
    text = getattr(config, "WATERMARK_TEXT", "BiliMix")
    opacity = getattr(config, "WATERMARK_OPACITY", 0.5)

    if not enabled or not text or not text.strip():
        return video_path

    # 转义单引号和冒号，避免 ffmpeg 解析错误
    escaped_text = text.replace("'", "\\'").replace(":", "\\:")

    drawtext = (
        f"drawtext=text='{escaped_text}':"
        f"fontsize=24:"
        f"fontcolor=white@{opacity}:"
        f"bordercolor=black@0.5:"
        f"borderw=1:"
        f"x=W-tw-20:"
        f"y=H-th-20"
    )

    tmp_path = video_path + ".watermark.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-threads", _FFMPEG_THREADS,
        "-i", video_path,
        "-vf", drawtext,
        "-c:a", "copy",
        tmp_path,
    ]

    print(f"[Step5] 叠加水印: \"{text}\" (opacity={opacity})")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)

    if r.returncode != 0:
        print(f"[Step5] 水印叠加失败: {r.stderr.strip()[-500:]}")
        # 清理失败的临时文件
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return video_path  # 返回原文件

    # 原子替换
    try:
        os.replace(tmp_path, video_path)
    except OSError:
        try:
            os.remove(video_path)
            os.rename(tmp_path, video_path)
        except OSError:
            print("[Step5] 水印文件替换失败，保留原文件")
            return video_path

    print(f"[Step5] 水印叠加完成")
    return video_path


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
      - 从原视频裁剪对应 segment 时间段（trim）
      - 按混音时长/原始时长比例变速（setpts）
      - 相邻 segment 之间：裁剪原视频中的停顿间隙，也做变速
      - 拼接所有片段（segment + 间隙），保持 concat 时间轴与混音音频一致

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
    part_idx = 0

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
        parts.append(
            f"[0:v]trim=start={orig_start:.3f}:duration={orig_dur:.3f},"
            f"setpts=PTS-STARTPTS,"
            f"setpts={speed_ratio:.6f}*PTS,"
            f"format=yuv420p[v{part_idx}]"
        )
        part_idx += 1

        # 句子间间隙：从原视频中取停顿画面，也做变速
        # 这样 concat 后的视频时间轴与混音音频（含句间静音）一致
        if i < len(tts_entries) - 1:
            next_entry = tts_entries[i + 1]
            gap_orig_start = orig_end
            gap_orig_end = next_entry["orig_start"]
            gap_mixed_start = mixed_end
            gap_mixed_end = next_entry["mixed_start"]
            gap_orig_dur = gap_orig_end - gap_orig_start
            gap_mixed_dur = gap_mixed_end - gap_mixed_start

            if gap_orig_dur > 0 and gap_mixed_dur > 0:
                gap_ratio = gap_mixed_dur / gap_orig_dur
                parts.append(
                    f"[0:v]trim=start={gap_orig_start:.3f}:duration={gap_orig_dur:.3f},"
                    f"setpts=PTS-STARTPTS,"
                    f"setpts={gap_ratio:.6f}*PTS,"
                    f"format=yuv420p[v{part_idx}]"
                )
                part_idx += 1

    if not parts:
        return None

    n = len(parts)
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

    seg_count = sum(1 for e in tts_entries
                    if e.get("orig_end", 0) > e.get("orig_start", 0)
                    and (e["mixed_end"] - e["mixed_start"]) > 0)
    print(f"[Step5] 生成 filter graph: {seg_count} 个 segment + "
          f"{n - seg_count} 个间隙, {n} 条, {len(graph)} 字节 → {graph_path}")
    return graph_path


# ============================================================
# 分块并行视频组装（用于长视频，避免 ffmpeg concat filter 卡死）
# ============================================================

def _build_block_filter_graph(tts_entries, ass_path=None, include_trailing_gap=True):
    """为单块构建 filter graph。返回 (graph_path, chain_count)。

    每个 sentence 生成一个 trim+setpts+format 链，句间间隙也做变速。
    include_trailing_gap: 非末块为 True（包含到下一块的间隙），末块为 False。
    每块 ~50 句 → ~100 条 chain，避免 ffmpeg concat filter O(n²) 问题。
    """
    parts = []
    pidx = 0
    if include_trailing_gap:
        n_process = len(tts_entries) - 1  # 最后一条仅用于间隙计算
    else:
        n_process = len(tts_entries)

    for i in range(n_process):
        entry = tts_entries[i]
        orig_start = entry["orig_start"]
        orig_end = entry["orig_end"]
        mixed_start = entry["mixed_start"]
        mixed_end = entry["mixed_end"]
        od = orig_end - orig_start
        md = mixed_end - mixed_start
        if od <= 0 or md <= 0:
            continue

        sr = md / od
        parts.append(
            f"[0:v]trim=start={orig_start:.3f}:duration={od:.3f},"
            f"setpts=PTS-STARTPTS,"
            f"setpts={sr:.6f}*PTS,"
            f"format=yuv420p[v{pidx}]"
        )
        pidx += 1

        if i + 1 < len(tts_entries):
            ne = tts_entries[i + 1]
            gos, goe = orig_end, ne["orig_start"]
            gms, gme = mixed_end, ne["mixed_start"]
            god, gmd = goe - gos, gme - gms
            if god > 0 and gmd > 0:
                gr = gmd / god
                parts.append(
                    f"[0:v]trim=start={gos:.3f}:duration={god:.3f},"
                    f"setpts=PTS-STARTPTS,"
                    f"setpts={gr:.6f}*PTS,"
                    f"format=yuv420p[v{pidx}]"
                )
                pidx += 1

    if not parts:
        return None, 0

    n = len(parts)
    ci = "".join(f"[v{i}]" for i in range(n))
    cp = f"{ci}concat=n={n}:v=1:a=0"
    if ass_path and os.path.isfile(ass_path):
        ea = _escape_path_for_filter(ass_path)
        graph = ";\n".join(parts) + f";\n{cp},ass=filename='{ea}'[outv]"
    else:
        graph = ";\n".join(parts) + f";\n{cp}[outv]"

    fd, gp = tempfile.mkstemp(suffix=".txt", prefix="bilimix_blk_")
    with os.fdopen(fd, "w") as f:
        f.write(graph)
    return gp, n


def _run_one_block(video_path, chunk_audio_path, filter_graph_path, out_path,
                   block_threads, timeout):
    """运行单个 ffmpeg 块。返回 out_path 或 raise。"""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-threads", str(block_threads),
        "-i", video_path, "-i", chunk_audio_path,
        "-/filter_complex", filter_graph_path,
        "-map", "[outv]", "-map", "1:a:0",
        "-c:v", "libx264",
        "-b:v", getattr(config, "VIDEO_BITRATE", "1500k"),
        "-preset", getattr(config, "VIDEO_X264_PRESET", "veryfast"),
        "-profile:v", "high", "-level", "5.0",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", getattr(config, "VIDEO_AUDIO_BITRATE", "128k"),
        "-ar", str(getattr(config, "VIDEO_AUDIO_SAMPLE_RATE", 44100)),
        "-ac", str(getattr(config, "VIDEO_AUDIO_CHANNELS", 2)),
        "-movflags", "+faststart", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        err = (r.stderr or "no output").strip()[-400:]
        raise RuntimeError(f"ffmpeg block failed: {err}")
    if not os.path.isfile(out_path):
        raise RuntimeError(f"Block output missing: {out_path}")
    return out_path


def _assemble_video_blocks(
    video_path: str,
    mixed_audio_path: str,
    srt_path: str,
    output_path: str,
    time_mapping: list,
    timeout: int,
    segments: Optional[list] = None,
    translations: Optional[dict] = None,
    subtitle_mode: str = "chinese_only",
    subtitle_font_size: Optional[int] = None,
) -> str:
    """分块并行视频组装：将长视频按句子数切块，逐块变速拼接后合并。

    当 time_mapping 句子数超过 VIDEO_MAX_CONCAT_SEGMENTS 时自动启用。
    每块 ~50 句，filter chain ~100 条，ffmpeg concat 稳定运行。
    """
    block_size = getattr(config, "VIDEO_BLOCK_SIZE", 50)
    block_threads = getattr(config, "VIDEO_BLOCK_FFMPEG_THREADS", 2)
    block_workers = getattr(config, "VIDEO_BLOCK_WORKERS", 2)
    block_timeout = max(timeout // 4, 1800)  # 每块超时，最少 30 分钟

    tts_entries = [
        e for e in time_mapping
        if e.get("type") == "tts_chinese" and e.get("orig_end", 0) > e.get("orig_start", 0)
    ]
    total = len(tts_entries)
    n_blocks = (total + block_size - 1) // block_size

    print(f"[Step5] ══ 分块并行模式 ══")
    print(f"[Step5] 句子总数: {total}, 块大小: {block_size}, 块数: {n_blocks}")
    print(f"[Step5] 并行 workers: {block_workers}, 线程/块: {block_threads}")

    # ---- 构建块元数据 ----
    blocks = []
    for bi in range(n_blocks):
        s = bi * block_size
        e = min(s + block_size, total)
        is_last = (bi == n_blocks - 1)

        if is_last:
            entries = tts_entries[s:e]
        else:
            entries = tts_entries[s:e + 1]  # +1 用于末尾间隙

        if is_last:
            sub_entries = entries
        else:
            sub_entries = entries[:-1]  # 字幕不包含末尾间隙条目

        audio_start = tts_entries[s]["mixed_start"]
        if is_last:
            audio_end = tts_entries[-1]["mixed_end"]
        else:
            audio_end = tts_entries[e]["mixed_start"]
        audio_dur = audio_end - audio_start

        # 收集段索引
        seg_indices = sorted(set(
            x.get("segment_index", -1) for x in sub_entries if x.get("segment_index", -1) >= 0
        ))

        blocks.append({
            "idx": bi,
            "entries": entries,
            "sub_entries": sub_entries,
            "is_last": is_last,
            "audio_start": audio_start,
            "audio_dur": audio_dur,
            "seg_indices": seg_indices,
            "n_sent": e - s,
        })

    # 打印概览
    for blk in blocks:
        print(f"[Step5]   块 {blk['idx']:02d}: 句 {blk['seg_indices'][0]}-{blk['seg_indices'][-1]}"
              f" ({blk['n_sent']}句), 音频 {blk['audio_dur']/60:.1f}min")

    # ---- 需要 segments + translations 用于字幕 ----
    # 此处依赖调用方提供，但 assemble_video 接口不含 segments/translations
    # 需要从 time_mapping 反向构建
    # 处理: 优先从 time_mapping 取 chinese 字段，segments 信息从 task 获取较复杂
    # 简化: 使用已有的 srt_path 为每块切出子字幕

    base_dir = os.path.dirname(output_path)
    block_outputs = [None] * n_blocks

    def _process_block(blk):
        """处理单个块的完整流程。"""
        bi = blk["idx"]
        bd = os.path.join(base_dir, f"_block_{bi:03d}")
        os.makedirs(bd, exist_ok=True)

        # 1. 提取音频
        ap = os.path.join(bd, "audio.mp3")
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", str(blk["audio_start"]),
            "-i", mixed_audio_path,
            "-t", str(blk["audio_dur"]),
            "-c:a", "copy", ap,
        ], check=True, capture_output=True, text=True, timeout=120)

        # 2. 构建 filter graph（先不传字幕）
        fgp, nc = _build_block_filter_graph(
            blk["entries"], None,
            include_trailing_gap=not blk["is_last"],
        )
        if fgp is None:
            raise RuntimeError(f"Block {bi}: empty filter graph")

        # 3. 生成字幕（有 ASS 滤镜的场景）
        # 注意: 分块模式下字幕需在 filter graph 中通过 ass= 滤镜烧录
        # 由于 generate_bilingual_srt 需要 segments/translations，这里用简化的
        # ASS 构建逻辑（从 time_mapping 中直接取 chinese 字段）
        if blk["sub_entries"]:
            asp = os.path.join(bd, "subs.ass")
            _build_block_ass(blk["sub_entries"], blk["audio_start"], asp,
                             subtitle_mode=subtitle_mode, font_size=subtitle_font_size,
                             segments=segments, translations=translations)

            # 用带字幕的 filter graph 替换
            try:
                os.remove(fgp)
            except OSError:
                pass
            fgp, nc = _build_block_filter_graph(
                blk["entries"], asp,
                include_trailing_gap=not blk["is_last"],
            )

        op = os.path.join(bd, "output.mp4")

        # 4. 运行 ffmpeg
        _run_one_block(video_path, ap, fgp, op, block_threads, block_timeout)

        # 清理
        try:
            os.remove(fgp)
        except OSError:
            pass

        return bi, op

    # ---- 并行处理 ----
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=block_workers) as executor:
        futures = {executor.submit(_process_block, blk): blk["idx"] for blk in blocks}
        for future in as_completed(futures):
            bi, opath = future.result()
            block_outputs[bi] = opath
            elapsed = time.time() - t_start
            done = sum(1 for x in block_outputs if x is not None)
            print(f"[Step5]   块 {bi:02d} 完成 ({done}/{n_blocks}, {elapsed/60:.1f}min)")

    total_elapsed = time.time() - t_start
    print(f"[Step5] 所有块完成，耗时 {total_elapsed/60:.1f}min")

    # ---- 拼接 ----
    concat_list = os.path.join(base_dir, "_block_concat.txt")
    with open(concat_list, "w") as f:
        for opath in block_outputs:
            f.write(f"file '{opath}'\n")

    print(f"[Step5] 拼接 {n_blocks} 个块 ...")
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c", "copy", output_path,
    ], check=True, capture_output=True, text=True, timeout=600)

    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        final_dur = _probe_video_duration(output_path)
        print(f"[Step5] 组装完成: {os.path.basename(output_path)} ({size_mb:.1f} MB, {final_dur:.2f}s)")
        return _apply_watermark(output_path)
    return ""


def _build_block_ass(tts_entries, time_offset, out_path, video_height=720,
                     subtitle_mode="chinese_only", font_size=None, segments=None, translations=None):
    """Build block-internal ASS subtitles from time_mapping entries.

    Supports both bilingual and chinese_only modes.
    In bilingual mode, English text is read from segments, Chinese from translations/time_mapping.
    font_size: user-specified size (None = auto-calculate from video_height).
    """
    entries = []
    for e in tts_entries:
        si = e.get("segment_index", -1)
        if si < 0:
            continue
        ms = max(0, e["mixed_start"] - time_offset)
        me = max(0, e["mixed_end"] - time_offset)
        if me <= ms:
            continue
        chn = e.get("chinese", "").strip()
        if not chn and translations and si in translations:
            chn = str(translations[si]).strip()
        eng = ""
        if subtitle_mode == "bilingual" and segments and 0 <= si < len(segments):
            eng = segments[si].get("text", "").strip()
        if not chn and not eng:
            continue
        entries.append({"start": ms, "end": me, "chinese": chn, "english": eng})

    if not entries:
        return None

    if font_size is not None:
        fs = int(font_size)
        fs = max(getattr(config, "ASS_FONT_SIZE_USER_MIN", 14), fs)
        fs = min(fs, getattr(config, "ASS_FONT_SIZE_USER_MAX", 120))
    else:
        fs = min(max(getattr(config, "ASS_FONT_SIZE_MIN", 28), video_height // 22),
                 getattr(config, "ASS_FONT_SIZE_MAX", 80))
    mv = max(getattr(config, "ASS_MARGIN_V_MIN", 30),
             int(video_height * getattr(config, "ASS_MARGIN_V_RATIO", 0.07)))
    m_en = mv + int(fs * getattr(config, "ASS_MARGIN_EN_RATIO", 1.4))
    ol = getattr(config, "ASS_OUTLINE", 2.5)
    sh = getattr(config, "ASS_SHADOW", 0)
    use_bilingual = subtitle_mode == "bilingual"

    styles = f"Style: Chinese,Noto Sans CJK SC,{fs},&H0000D7FF&,&H000000FF&,&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,1,{ol},{sh},2,20,20,{mv},1\n"
    if use_bilingual:
        styles += f"Style: English,Noto Sans CJK SC,{fs},&H00E6E6E6&,&H000000FF&,&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,1,{ol},{sh},2,20,20,{m_en},1\n"

    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: {video_height if video_height >= 1080 else 1080}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
""" + styles + """
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [hdr]
    for ent in entries:
        ts = _seconds_to_ass_time(ent["start"])
        te = _seconds_to_ass_time(ent["end"])
        eng = ent["english"]
        chn = ent["chinese"]
        if use_bilingual and eng and chn:
            eng_safe = eng.replace("\n", "\\N")
            chn_safe = chn.replace("\n", "\\N")
            lines.append(fr"Dialogue: 0,{ts},{te},English,,0,0,0,,{{\q0}}{eng_safe}")
            lines.append(fr"Dialogue: 0,{ts},{te},Chinese,,0,0,0,,{{\q1}}{chn_safe}")
        else:
            text = chn or eng
            style = "Chinese" if chn else "English"
            text_safe = text.replace("\n", "\\N")
            qmark = "q1" if (chn and not eng) else "q0"
            lines.append(fr"Dialogue: 0,{ts},{te},{style},,0,0,0,,{{\{qmark}}}{text_safe}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def assemble_video(
    video_path: str,
    mixed_audio_path: str,
    srt_path: str,
    output_path: str,
    subtitle_style: Optional[str] = None,
    timeout: Optional[int] = None,
    time_mapping: Optional[list] = None,
    segments: Optional[list] = None,
    translations: Optional[dict] = None,
    subtitle_mode: str = "chinese_only",
    subtitle_font_size: Optional[int] = None,
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
        # 检测是否超过 concat 阈值，自动切换分块并行模式
        tts_count = len([
            e for e in time_mapping
            if e.get("type") == "tts_chinese" and e.get("orig_end", 0) > e.get("orig_start", 0)
        ])
        max_concat = getattr(config, "VIDEO_MAX_CONCAT_SEGMENTS", 200)
        if tts_count > max_concat:
            print(f"[Step5] 检测到长视频 ({tts_count} 句 > {max_concat})，启用分块并行模式")
            # 注意: 分块模式内部自行生成字幕，不依赖外部 srt_path
            return _assemble_video_blocks(
                video_path, mixed_audio_path, srt_path,
                output_path, time_mapping, timeout,
                segments=segments, translations=translations,
                subtitle_mode=subtitle_mode, subtitle_font_size=subtitle_font_size,
            )
        filter_graph_path = _build_filter_graph(time_mapping, srt_path)

    encoders = _detect_encoders()
    video_enc = encoders.get("video")
    audio_enc = encoders.get("audio", "aac")

    if not video_enc:
        # 错误信息已在 _detect_encoders 中打印
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
        preset = getattr(config, "VIDEO_X264_PRESET", "veryfast")
        cmd += ["-preset", preset, "-profile:v", "high", "-level", "5.0"]
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
        return _apply_watermark(output_path)
    return ""
