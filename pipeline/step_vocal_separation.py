"""
人声/背景音分离模块（基于 Demucs）

用于"保留背景音乐"功能：将原始音频分离为人声轨和伴奏轨（背景音乐/环境音），
分离出的伴奏轨可在最终混音时叠加到中文 TTS 底层，让配音视频保留原有的
背景音乐/音效，提升沉浸感（否则中文 TTS 只能铺在纯静音底轨上）。

使用 htdemucs 双源模型（--two-stems=vocals），只分离人声/非人声两路，
比四源分离（vocals/drums/bass/other）更快，满足本场景需求。
"""
import os
import subprocess
import tempfile
import shutil
import sys


def separate_vocals(audio_path: str, cache_dir: str, timeout: int = None) -> dict:
    """
    对音频做人声/背景音分离。

    Args:
        audio_path: 原始音频文件路径（wav/mp3等）
        cache_dir: 分离结果缓存目录
        timeout: demucs 进程超时时间（秒）。None 则读取 config.DEMUCS_TIMEOUT（默认 1800）

    Returns:
        dict: {"ok": bool, "vocals_path": str, "no_vocals_path": str, "error": str}
    """
    if timeout is None:
        from core import config as _cfg
        timeout = getattr(_cfg, "DEMUCS_TIMEOUT", 1800)
    os.makedirs(cache_dir, exist_ok=True)
    basename = os.path.splitext(os.path.basename(audio_path))[0]

    # demucs 输出目录结构固定为 {out_dir}/{model_name}/{track_basename}/
    model_name = "htdemucs"
    vocals_path = os.path.join(cache_dir, model_name, basename, "vocals.wav")
    no_vocals_path = os.path.join(cache_dir, model_name, basename, "no_vocals.wav")

    # 已有缓存结果直接复用
    if os.path.exists(vocals_path) and os.path.exists(no_vocals_path):
        print(f"[VocalSep] 发现已有分离缓存: {cache_dir}")
        return {"ok": True, "vocals_path": vocals_path,
                "no_vocals_path": no_vocals_path, "error": ""}

    print(f"[VocalSep] 开始人声分离: {audio_path}")
    # 用当前进程的 Python 解释器（sys.executable）绝对路径调用 demucs，
    # 避免依赖系统 PATH 解析 "python"（可能解析到未安装 demucs 的其他环境）
    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",
        "-n", model_name,
        "-d", "cpu",
        "-o", cache_dir,
        audio_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "vocals_path": "", "no_vocals_path": "",
                "error": f"人声分离超时（>{timeout}s）"}

    if r.returncode != 0:
        err = r.stderr.strip()[-1000:] if r.stderr else "无错误输出"
        # 也检查 stdout，demucs 有时把错误打到 stdout
        if not err or err == "无错误输出":
            err = r.stdout.strip()[-1000:] if r.stdout else "无错误输出"
        print(f"[VocalSep] 分离失败 (code={r.returncode}): {err}")
        return {"ok": False, "vocals_path": "", "no_vocals_path": "", "error": err}

    if not os.path.exists(no_vocals_path):
        return {"ok": False, "vocals_path": "", "no_vocals_path": "",
                "error": "分离完成但未找到输出文件"}

    print(f"[VocalSep] 分离完成: vocals={vocals_path}, no_vocals={no_vocals_path}")
    return {"ok": True, "vocals_path": vocals_path,
            "no_vocals_path": no_vocals_path, "error": ""}
