"""
Step 0: 视频预处理模块
下载 YouTube 视频 / 提取本地视频中的音频流，供后续管道复用。

支持:
- YouTube URL → yt-dlp 下载最佳质量 mp4 → ffmpeg 提取音频 wav
- 本地视频文件 → ffmpeg 提取音频 wav
- 缓存: 同名视频/音频不重复下载
"""
import os
import re
import subprocess
import time
from typing import Optional


def _run_ffmpeg(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """运行 ffmpeg 命令，统一错误处理。"""
    return subprocess.run(
        args, capture_output=True, timeout=timeout, check=False,
        text=True,
    )


def _get_video_cache_key(source: str) -> str:
    """为视频源生成唯一缓存键。"""
    import hashlib
    return hashlib.md5(source.encode("utf-8")).hexdigest()


def _extract_audio(video_path: str, audio_output: str, timeout: int = 300) -> bool:
    """
    从视频文件中提取音频为 16kHz mono wav（WhisperX 兼容）。

    Args:
        video_path: 输入视频文件路径
        audio_output: 输出音频文件路径
        timeout: ffmpeg 超时（秒）

    Returns:
        bool: 提取成功
    """
    # 始终重新提取音频，避免旧任务遗留的音频文件被复用导致内容错误
    os.makedirs(os.path.dirname(audio_output) or ".", exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video_path,
        "-vn",                          # 只取音频
        "-acodec", "pcm_s16le",        # 16-bit PCM
        "-ar", "16000",                # 16kHz 采样率
        "-ac", "1",                    # 单声道
        audio_output,
    ]
    proc = _run_ffmpeg(cmd, timeout=timeout)
    return proc.returncode == 0 and os.path.exists(audio_output)


def _probe_video_duration(video_path: str) -> float:
    """探测视频时长（秒），失败返回 0。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(proc.stdout.strip())
    except Exception:
        return 0


def _download_youtube(url: str, output_dir: str, cache_key: str) -> dict:
    """
    使用 yt-dlp 下载 YouTube 视频。

    Args:
        url: YouTube 视频 URL
        output_dir: 下载目录
        cache_key: 缓存键（用于文件命名）

    Returns:
        dict: {"video_path": str, "title": str, "thumbnail": str, "duration": float}
              失败时返回 {"error": str}
    """
    os.makedirs(output_dir, exist_ok=True)

    # 检查是否已有缓存
    existing_files = [f for f in os.listdir(output_dir)
                      if f.startswith(cache_key) and f.endswith(('.mp4', '.mkv', '.webm'))]
    if existing_files:
        video_path = os.path.join(output_dir, existing_files[0])
        # 尝试从 json info 读取元数据
        info_path = video_path.rsplit('.', 1)[0] + '.info.json'
        title = existing_files[0]
        thumbnail = ""
        duration = 0.0
        if os.path.exists(info_path):
            try:
                import json
                with open(info_path, 'r', encoding='utf-8') as f:
                    info = json.load(f)
                title = info.get('title', title)
                thumbnail = info.get('thumbnail', '')
                duration = info.get('duration', 0)
            except Exception:
                pass
        duration = duration or _probe_video_duration(video_path)
        return {"video_path": video_path, "title": title,
                "thumbnail": thumbnail, "duration": duration}

    # 下载视频
    output_template = os.path.join(output_dir, f"{cache_key}_%(title)s.%(ext)s")
    info_output = os.path.join(output_dir, f"{cache_key}_info")

    # yt-dlp 下载最佳 mp4 格式
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
        "--write-info-json",
        "--print", "after_move:filepath",
        url,
    ]

    cwd = output_dir
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800, cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return {"error": "下载超时（>30分钟），请检查网络或视频长度"}

    if proc.returncode != 0:
        stderr = proc.stderr.strip()[-500:] if proc.stderr else "未知错误"
        return {"error": f"yt-dlp 下载失败: {stderr}"}

    # 查找下载的文件
    for f in sorted(os.listdir(output_dir), reverse=True):
        if f.startswith(cache_key) and f.endswith('.mp4') and not f.endswith('.info.json'):
            video_path = os.path.join(output_dir, f)
            # 查找对应的 info.json
            info_path = video_path.rsplit('.', 1)[0] + '.info.json'
            break
    else:
        return {"error": "下载完成但无法定位输出文件"}

    # 解析元数据
    title = os.path.splitext(os.path.basename(video_path))[0]
    thumbnail = ""
    duration = 0.0
    if os.path.exists(info_path):
        try:
            import json
            with open(info_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
            title = info.get('title', title)
            thumbnail = info.get('thumbnail', '')
            duration = info.get('duration', 0)
        except Exception:
            pass

    duration = duration or _probe_video_duration(video_path)
    return {"video_path": video_path, "title": title,
            "thumbnail": thumbnail, "duration": duration}


def prepare_video(
    source: str,
    cache_dir: str,
    title: Optional[str] = None,
) -> dict:
    """
    准备视频输入：下载 YouTube 视频或处理本地文件，提取音频。

    Args:
        source: YouTube URL 或本地视频文件路径
        cache_dir: 缓存目录（下载和提取的音频都放这里）
        title: 用户指定的标题（可选），用于文件命名

    Returns:
        dict: {
            "ok": bool,
            "video_path": str,     # 视频文件路径
            "audio_path": str,     # 提取的音频文件路径（16kHz mono wav）
            "title": str,          # 视频标题
            "duration": float,     # 视频时长（秒）
            "thumbnail": str,      # 缩略图 URL（YouTube 独有）
            "error": str,          # 失败时的错误信息
        }
    """
    os.makedirs(cache_dir, exist_ok=True)

    # ---- 判断来源类型 ----
    is_youtube = bool(re.match(
        r'https?://(www\.)?(youtube\.com|youtu\.be)/', source
    ))

    if is_youtube:
        cache_key = _get_video_cache_key(source)
        print(f"[Step0] YouTube URL 检测到: {source[:80]}...")

        # 1. 下载视频
        dl_result = _download_youtube(source, cache_dir, cache_key)
        if "error" in dl_result:
            return {"ok": False, "error": dl_result["error"],
                    "video_path": "", "audio_path": "", "title": "",
                    "duration": 0, "thumbnail": ""}

        video_path = dl_result["video_path"]
        video_title = title or dl_result["title"]
        duration = dl_result["duration"]
        thumbnail = dl_result["thumbnail"]

        print(f"[Step0] 下载完成: {os.path.basename(video_path)} "
              f"({duration:.0f}s)")

        # 2. 提取音频
        audio_cache_key = _get_video_cache_key(video_path)
        audio_path = os.path.join(cache_dir, f"audio_{audio_cache_key}.wav")
        if not _extract_audio(video_path, audio_path):
            return {"ok": False, "error": "音频提取失败",
                    "video_path": video_path, "audio_path": "",
                    "title": video_title, "duration": duration,
                    "thumbnail": thumbnail}

        print(f"[Step0] 音频提取完成: {os.path.basename(audio_path)}")
        return {"ok": True, "video_path": video_path, "audio_path": audio_path,
                "title": video_title, "duration": duration,
                "thumbnail": thumbnail}

    # ---- 本地文件 ----
    if not os.path.isfile(source):
        return {"ok": False, "error": f"文件不存在: {source}",
                "video_path": "", "audio_path": "", "title": "",
                "duration": 0, "thumbnail": ""}

    ext = os.path.splitext(source)[1].lower()
    if ext not in ('.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv'):
        return {"ok": False, "error": f"不支持的视频格式: {ext}",
                "video_path": "", "audio_path": "", "title": "",
                "duration": 0, "thumbnail": ""}

    video_path = source
    video_title = title or os.path.splitext(os.path.basename(source))[0]
    duration = _probe_video_duration(video_path)

    # 提取音频
    cache_key = _get_video_cache_key(video_path)
    audio_path = os.path.join(cache_dir, f"audio_{cache_key}.wav")
    if not _extract_audio(video_path, audio_path):
        return {"ok": False, "error": "音频提取失败",
                "video_path": video_path, "audio_path": "",
                "title": video_title, "duration": duration,
                "thumbnail": ""}

    print(f"[Step0] 本地视频 {os.path.basename(video_path)} → "
          f"音频 {os.path.basename(audio_path)} ({duration:.0f}s)")
    return {"ok": True, "video_path": video_path, "audio_path": audio_path,
            "title": video_title, "duration": duration, "thumbnail": ""}
