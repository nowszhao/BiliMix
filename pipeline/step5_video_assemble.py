"""
Step 5: 视频组装模块
将混音音频与视频合成，烧录双语字幕，输出最终配音视频。

策略（视频连续流畅版）：
  1. 用 PIL 把每条字幕渲染为带描边透明 PNG
  2. 用 ffmpeg filter_complex 一次性叠加所有字幕到原视频
     - 链式 overlay + enable=between(t\,start\,end) 控制每条字幕的可见时间
     - 视频以单流连续播放，**不会跳跃**
  3. 与混合音频合并，按用户规则对齐时长：
     - 混音 ≤ 原视频 → 截断视频到混音时长
     - 混音 > 原视频 → tpad=clone 延长末帧
"""
import os
import re
import subprocess
import shutil
import tempfile
from typing import Optional


# ============================================================
# 字幕渲染
# ============================================================

def _parse_srt(srt_path: str) -> list[dict]:
    if not os.path.isfile(srt_path):
        return []
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()
    entries = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0].strip())
        except ValueError:
            continue
        m = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            lines[1].strip()
        )
        if not m:
            continue
        sh, sm, ss, sms = (int(m.group(i)) for i in (1, 2, 3, 4))
        eh, em, es, ems = (int(m.group(i)) for i in (5, 6, 7, 8))
        start = sh * 3600 + sm * 60 + ss + sms / 1000
        end = eh * 3600 + em * 60 + es + ems / 1000
        text = "\n".join(lines[2:]).strip()
        entries.append({"index": idx, "start": start, "end": end, "text": text})
    return entries


def _load_font(size: int):
    from PIL import ImageFont
    for path in [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _render_subtitle_png(text: str, video_w: int, font_size: int) -> str:
    """渲染一条字幕为带描边的透明 PNG（高度由文本行数自动计算）"""
    from PIL import Image, ImageDraw
    lines = text.split("\n")
    line_h = int(font_size * 1.3)
    canvas_h = line_h * len(lines) + 12
    img = Image.new("RGBA", (video_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(font_size)
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (video_w - line_w) // 2
        y = 6 + i * line_h
        for dx, dy in [(-2, 0), (2, 0), (0, -2), (0, 2),
                       (-1, -1), (1, -1), (-1, 1), (1, 1)]:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 230))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
    out = tempfile.mktemp(suffix=".png", prefix="sub_")
    img.save(out, "PNG")
    return out


# ============================================================
# SRT 生成
# ============================================================

def _seconds_to_srt_time(seconds: float) -> str:
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
    lines = []
    for e in entries:
        lines.append(str(e["index"]))
        lines.append(f"{_seconds_to_srt_time(e['start'])} --> {_seconds_to_srt_time(e['end'])}")
        if subtitle_mode == "bilingual":
            lines.append(f"{e['english']}\n{e['chinese']}" if (e['english'] and e['chinese'])
                         else e['chinese'] or e['english'])
        elif subtitle_mode == "chinese_only":
            lines.append(e['chinese'] or e['english'])
        else:
            lines.append(e['english'] or "")
        lines.append("")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[Step5] 生成字幕: {len(entries)} 条 → {output_path}")
    return output_path


# ============================================================
# 视频组装：连续流 + 链式 overlay
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


def _build_continuous_overlay(
    video_path: str,
    entries: list[dict],
    vw: int,
    vh: int,
    font_size: int,
    work_dir: str,
) -> str:
    """用链式 overlay + enable=between() 一次性烧录所有字幕到原视频，输出连续视频文件路径。"""
    from PIL import Image
    # 渲染所有 PNG
    png_paths = []
    for e in entries:
        png = _render_subtitle_png(e["text"], vw, font_size)
        png_paths.append((e, png))

    # 构建 filter_complex
    # 链式 overlay，每条字幕独立控制
    # 表达式内的 , 用 \ 转义
    fc_parts = []
    for i, (e, png) in enumerate(png_paths):
        png_h = Image.open(png).size[1]
        y_expr = f"H-{40 + png_h}"
        enable_expr = f"between(t\\,{e['start']}\\,{e['end']})"
        fc_parts.append(f"[{i+1}:v]format=rgba,setpts=PTS-STARTPTS[sub{i}];")
        if i == 0:
            fc_parts.append(f"[0:v][sub0]overlay=0:{y_expr}:enable={enable_expr}[v0];")
        else:
            fc_parts.append(f"[v{i-1}][sub{i}]overlay=0:{y_expr}:enable={enable_expr}[v{i}];")

    last_label = f"[v{len(png_paths)-1}]"
    fc_parts.append(f"{last_label}format=yuv420p[vout]")
    filter_complex = "".join(fc_parts)

    # 构建 ffmpeg 命令
    inputs = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path]
    for _, png in png_paths:
        inputs += ["-loop", "1", "-i", png]
    inputs += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0",
        "-an",
        "-r", "30",
    ]
    overlay_video = os.path.join(work_dir, "overlay.mp4")
    inputs.append(overlay_video)
    r = subprocess.run(inputs, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        print(f"[Step5] 链式 overlay 失败: {r.stderr[-500:]}")
        return ""
    print(f"[Step5] 链式 overlay 成功: {len(png_paths)} 条字幕")
    return overlay_video


def _align_video_to_audio(video_path: str, audio_path: str, target_dur: float, output_path: str) -> bool:
    """对齐规则：混音 ≤ 原视频 → 截断 / 混音 > 原视频 → 延长最后一帧。"""
    video_dur = _probe_video_duration(video_path)
    diff = video_dur - target_dur

    tmp = output_path + ".re.mp4"
    if diff > 0.3:
        # 混音较短：截断视频
        print(f"[Step5] 视频 > 混音 ({video_dur:.2f}s > {target_dur:.2f}s) → 截断")
        r = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path, "-i", audio_path,
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest", "-movflags", "+faststart",
            tmp,
        ], capture_output=True, text=True, timeout=300)
    elif diff < -0.3:
        # 混音较长：延长最后一帧
        print(f"[Step5] 视频 < 混音 ({video_dur:.2f}s < {target_dur:.2f}s) → 延长末帧")
        r = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path, "-i", audio_path,
            "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={abs(diff):.3f}[v]",
            "-map", "[v]", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
            tmp,
        ], capture_output=True, text=True, timeout=300)
    else:
        # 偏差 < 0.3s，直接合并
        r = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path, "-i", audio_path,
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0",
            "-movflags", "+faststart",
            output_path,
        ], capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and os.path.exists(output_path):
            return True
        return False

    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        if os.path.exists(output_path):
            os.remove(output_path)
        os.rename(tmp, output_path)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def assemble_video(
    video_path: str,
    mixed_audio_path: str,
    srt_path: str,
    output_path: str,
    subtitle_style: Optional[str] = None,
    timeout: int = 1800,
) -> str:
    """连续流视频组装：原视频单流播放 + 字幕按时间烧录 + 替换音轨。"""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    if os.path.isfile(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass

    entries = _parse_srt(srt_path)
    video_duration = _probe_video_duration(video_path)
    mixed_duration = _probe_video_duration(mixed_audio_path)
    print(f"[Step5] 视频时长: {video_duration:.2f}s  混音时长: {mixed_duration:.2f}s  字幕数: {len(entries)}")

    if not entries:
        print("[Step5] 无字幕，纯拼接音视频")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path, "-i", mixed_audio_path,
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest", "-movflags", "+faststart",
            output_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"[Step5] 失败: {r.stderr.strip()[-300:]}")
            return ""
        return output_path

    vw, vh = _probe_video_size(video_path)
    font_size = max(20, vh // 18)
    print(f"[Step5] 视频尺寸: {vw}x{vh}  字号: {font_size}")

    work_dir = tempfile.mkdtemp(prefix="bilimix_step5_")
    try:
        # 1) 一次性烧录所有字幕 → 连续视频
        overlay_video = _build_continuous_overlay(
            video_path, entries, vw, vh, font_size, work_dir
        )
        if not overlay_video:
            print("[Step5] overlay 失败")
            return ""

        overlay_dur = _probe_video_duration(overlay_video)
        print(f"[Step5] Overlay 视频时长: {overlay_dur:.2f}s")

        # 2) 合并音视频并按规则对齐
        ok = _align_video_to_audio(overlay_video, mixed_audio_path, mixed_duration, output_path)
        if not ok:
            print(f"[Step5] 音视频合并失败")
            return ""

        if os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            final_dur = _probe_video_duration(output_path)
            print(f"[Step5] 组装完成: {os.path.basename(output_path)} ({size_mb:.1f} MB, {final_dur:.2f}s)")
            return output_path
        return ""
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
