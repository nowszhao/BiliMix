"""Shared media cache paths for video analysis and dubbing tasks.

The source video is uploaded once by the Skill.  Both the analysis task and
the dubbing task need the same extracted audio and separated vocal stems, so
these intermediate files are content-addressed and shared across tasks.
"""

import os

from core import config


MEDIA_CACHE_SCHEMA = "v1"


def video_cache_root() -> str:
    return os.path.join(config.DOWNLOAD_DIR, "video_cache")


def shared_media_cache_dir() -> str:
    return os.path.join(video_cache_root(), "_shared")


def extracted_audio_dir() -> str:
    return os.path.join(shared_media_cache_dir(), f"audio_{MEDIA_CACHE_SCHEMA}")


def vocal_separation_dir() -> str:
    return os.path.join(
        shared_media_cache_dir(), f"vocal_separation_{MEDIA_CACHE_SCHEMA}"
    )


def vocal_cache_for_task(task: dict | None, fallback: str) -> str:
    """Use the shared cache for video/analysis tasks; keep audio tasks isolated."""
    if task and task.get("type") in ("video", "analysis"):
        return vocal_separation_dir()
    return fallback


def is_shared_media_path(path: str) -> bool:
    if not path:
        return False
    root = os.path.realpath(shared_media_cache_dir())
    value = os.path.realpath(path)
    return value == root or value.startswith(root + os.sep)
