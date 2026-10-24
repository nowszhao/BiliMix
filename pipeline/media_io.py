"""Media file preparation and cleanup; no HTTP transport or scheduling."""

import os
import shutil
import subprocess
import urllib.request
from core import config


def _probe_audio_duration(path: str) -> float:
    """Probe audio duration with ffprobe."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            text=True,
            timeout=30,
        )
        return float(out.strip())
    except Exception:
        return 0.0


def download_audio(url: str, save_path: str, task_id: str) -> bool:
    """Download audio from URL with progress reporting."""
    from core.task_manager import update_task, is_cancelled

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BiliMix/1.0"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            last_pct = -1
            with open(save_path, "wb") as f:
                while True:
                    if is_cancelled(task_id):
                        return False
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(downloaded / total * 100)
                        if pct != last_pct:
                            update_task(
                                task_id,
                                progress=max(1, min(8, pct // 15)),
                                message=f"Downloading... {pct}%",
                            )
                            last_pct = pct
        return True
    except Exception as e:
        print(f"[Download] Failed: {e}")
        return False


def _cleanup_intermediate_files(result_dir: str):
    """Remove intermediate TTS cache files."""
    cache_dir = os.path.join(result_dir, "tts_confucius_cache")
    if os.path.isdir(cache_dir):
        try:
            shutil.rmtree(cache_dir)
        except Exception:
            pass
    gapfill_dir = os.path.join(result_dir, "gapfill")
    if os.path.isdir(gapfill_dir):
        try:
            shutil.rmtree(gapfill_dir)
        except Exception:
            pass


def _try_resolve_local_url(url: str) -> str:
    """Try to resolve a URL to local file path."""
    if not url:
        return ""
    if url.startswith("file://"):
        p = url[len("file://") :]
        if os.path.isfile(p):
            return p
        return ""
    if url.startswith("/") and os.path.isfile(url):
        return url
    download_dir = config.DOWNLOAD_DIR
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if name:
        candidate = os.path.join(download_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return ""
