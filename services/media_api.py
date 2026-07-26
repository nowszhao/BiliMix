"""
Media Blueprint - static serving, upload, audio serve, config, API index.
"""
import json
import os
import sys
import time
import traceback
import urllib.parse

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Blueprint, jsonify, request, send_file, send_from_directory
from core import config
from core.config_manager import get_all_config, update_config
from core.task_manager import tasks
from core.database import (
    get_favorites, get_subscriptions, get_episodes, get_episode_stats,
    get_recent_podcasts, get_unread_counts_by_subscription,
)

# _web_dir is used by index() to serve index.html
_web_dir = os.path.join(config.BASE_DIR, "web")
media_bp = Blueprint('media_api', __name__)


# ── Main Page ───────────────────────────────────────────────

@media_bp.route("/")
def index():
    return send_from_directory(_web_dir, "index.html")


# ── File Upload ─────────────────────────────────────────────

@media_bp.route("/api/upload", methods=["POST"])
def upload_audio():
    if "file" not in request.files:
        return jsonify({"error": "请选择文件"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    allowed = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac",
               ".mp4", ".mkv", ".mov", ".avi", ".webm",
               ".ass", ".srt"}
    if ext not in allowed:
        return jsonify({"error": f"不支持的文件格式: {ext}，支持 {', '.join(sorted(allowed))}"}), 400

    safe_name = os.path.basename(f.filename)
    save_path = os.path.join(config.DOWNLOAD_DIR, safe_name)

    if os.path.exists(save_path):
        name, e = os.path.splitext(safe_name)
        save_path = os.path.join(config.DOWNLOAD_DIR, f"{name}_{int(time.time())}{e}")

    f.save(save_path)
    size_mb = os.path.getsize(save_path) / (1024 * 1024)
    print(f"[Upload] 已保存: {save_path} ({size_mb:.1f} MB)")

    return jsonify({
        "ok": True,
        "local_path": save_path,
        "filename": os.path.basename(save_path),
        "size_mb": round(size_mb, 1),
    })


# ── Media Serving ───────────────────────────────────────────

@media_bp.route("/api/audio/<path:filename>")
def serve_audio(filename):
    """Serve audio/video/srt files from results or downloads directories."""
    # Security: prevent path traversal
    safe = os.path.normpath(filename)
    if safe.startswith("..") or os.path.isabs(safe):
        return jsonify({"error": "Invalid path"}), 400

    # Try results dir first, then downloads dir
    result_path = os.path.join(config.RESULT_DIR, safe)
    if os.path.isfile(result_path):
        mime = _guess_mime(safe)
        return send_file(result_path, mimetype=mime)

    download_path = os.path.join(config.DOWNLOAD_DIR, safe)
    if os.path.isfile(download_path):
        mime = _guess_mime(safe)
        return send_file(download_path, mimetype=mime)

    return jsonify({"error": "File not found"}), 404


def _sanitize_filename(name: str) -> str:
    """Sanitize filename for cross-platform safety (Windows + macOS + Linux)."""
    import re
    # Remove or replace characters illegal on Windows: <>:"/\|?*
    # Also strip control characters and leading/trailing spaces/dots
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = re.sub(r'\.+$', '', name)
    name = name.strip()
    return name[:200] if len(name) > 200 else name


@media_bp.route("/api/download/<path:filename>")
def download_file(filename):
    """Download file as attachment with custom filename.

    Query params:
        name: desired download filename (will be sanitized)
    """
    safe = os.path.normpath(filename)
    if safe.startswith("..") or os.path.isabs(safe):
        return jsonify({"error": "Invalid path"}), 400

    result_path = os.path.join(config.RESULT_DIR, safe)
    if not os.path.isfile(result_path):
        download_path = os.path.join(config.DOWNLOAD_DIR, safe)
        if os.path.isfile(download_path):
            result_path = download_path
        else:
            return jsonify({"error": "File not found"}), 404

    download_name = request.args.get("name", "")
    if download_name:
        download_name = _sanitize_filename(download_name)
    if not download_name:
        download_name = os.path.basename(safe)

    mime = _guess_mime(safe)
    return send_file(result_path, mimetype=mime, as_attachment=True,
                     download_name=download_name)


def _guess_mime(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".ass": "text/plain",
        ".srt": "text/plain",
        ".vtt": "text/vtt",
    }.get(ext, "application/octet-stream")


# ── Config ──────────────────────────────────────────────────

@media_bp.route("/api/config")
def api_get_config():
    return jsonify(get_all_config())


@media_bp.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.get_json()
    if not data:
        return jsonify({"error": "请提供配置数据"}), 400
    try:
        update_config(data)
        return jsonify({"ok": True, "message": "配置已保存"})
    except Exception as e:
        return jsonify({"error": f"保存失败: {str(e)}"}), 500


# ── API Index ───────────────────────────────────────────────

@media_bp.route("/api")
def api_index():
    """Return full API index with all routes and descriptions."""
    endpoints = []

    # Task endpoints
    endpoints.append({
        "path": "/api/submit",
        "method": "POST",
        "description": "提交音���/视频处理任务",
        "params": {
            "url": "音频URL",
            "local_path": "upload 后的本地路径",
            "type": "audio 或 video",
            "title": "任务标题",
            "keep_bgm": "是否保留背景音乐",
            "skip_confirmation": "是否跳过确认",
            "subtitle_mode": "bilingual|chinese_only|none (video only)",
            "subtitle_font_size": "字幕字号 (video only)",
        },
    })
    endpoints.append({
        "path": "/api/tasks",
        "method": "GET",
        "description": "获取任务列表",
        "params": {"limit": "返回数量限制"},
    })
    endpoints.append({
        "path": "/api/task/<task_id>",
        "method": "GET",
        "description": "查询任务状态和进度",
    })
    endpoints.append({
        "path": "/api/task/<task_id>/result",
        "method": "GET",
        "description": "获取任务完整结果",
    })
    endpoints.append({
        "path": "/api/task/<task_id>",
        "method": "DELETE",
        "description": "删除任务及关联文件",
    })
    endpoints.append({
        "path": "/api/task/<task_id>/cancel",
        "method": "POST",
        "description": "终止正在运行的任务",
    })
    endpoints.append({
        "path": "/api/task/<task_id>/confirm_sentences",
        "method": "POST",
        "description": "确认句子翻译后继续处理",
    })
    endpoints.append({
        "path": "/api/task/<task_id>/redo",
        "method": "POST",
        "description": "完整重做任务（清空结果后从头执行）",
    })
    endpoints.append({
        "path": "/api/task/<task_id>/retry",
        "method": "POST",
        "description": "断点续传（从失败的步骤恢复执行）",
    })
    endpoints.append({
        "path": "/api/task/<task_id>/retry-synthesis",
        "method": "POST",
        "description": "仅重试 TTS 语音合成步骤",
    })

    # Podcast endpoints
    endpoints.append({
        "path": "/api/favorites",
        "methods": ["GET", "POST", "DELETE"],
        "description": "播客收藏管理",
    })
    endpoints.append({
        "path": "/api/favorites/check",
        "method": "GET",
        "description": "检查是否已收藏",
        "params": {"rss_url": "RSS地址"},
    })
    endpoints.append({
        "path": "/api/subscriptions",
        "methods": ["GET", "POST", "DELETE"],
        "description": "播客订阅管理",
    })
    endpoints.append({
        "path": "/api/subscriptions/refresh",
        "method": "POST",
        "description": "手动刷新所有订阅",
    })
    endpoints.append({
        "path": "/api/episodes",
        "method": "GET",
        "description": "获取单集列表",
        "params": {"status": "筛选状态", "time_range": "时间范围", "page": "页码", "page_size": "每页数量"},
    })
    endpoints.append({
        "path": "/api/episodes/stats",
        "method": "GET",
        "description": "单集统计",
    })
    endpoints.append({
        "path": "/api/episodes/<id>",
        "method": "PATCH",
        "description": "更新单集状态",
    })
    endpoints.append({
        "path": "/api/episodes/mark-all-read",
        "method": "POST",
        "description": "批量标记为已读",
    })
    endpoints.append({
        "path": "/api/episodes/refresh",
        "method": "POST",
        "description": "刷新所有订阅单集",
    })
    endpoints.append({
        "path": "/api/episodes/refresh/<rss_url>",
        "method": "POST",
        "description": "刷新指定订阅单集",
    })
    endpoints.append({
        "path": "/api/podcast/search",
        "method": "GET",
        "description": "搜索播客",
        "params": {"q": "搜索关键词"},
    })
    endpoints.append({
        "path": "/api/podcast/rss",
        "method": "GET",
        "description": "解析RSS feed",
        "params": {"url": "RSS地址"},
    })
    endpoints.append({
        "path": "/api/recent-podcasts",
        "method": "GET",
        "description": "最近访问的播客",
    })

    # Tools endpoints
    endpoints.append({
        "path": "/api/translate",
        "method": "POST",
        "description": "翻译单词",
        "params": {"english": "英文单词", "context": "上下文句子(可选)"},
    })
    endpoints.append({
        "path": "/api/word-levels",
        "method": "POST",
        "description": "查询单词频率等级",
        "params": {"words": "逗号分隔的单词列表"},
    })
    endpoints.append({
        "path": "/api/search-history/suggestions",
        "method": "GET",
        "description": "搜索建议",
        "params": {"q": "部分查询词"},
    })
    endpoints.append({
        "path": "/api/search-history",
        "methods": ["POST", "DELETE"],
        "description": "搜索历史管理",
    })

    # Other endpoints
    endpoints.append({
        "path": "/api/upload",
        "method": "POST",
        "description": "上传音频/视频文件",
        "params": {"file": "multipart文件上传"},
    })
    endpoints.append({
        "path": "/api/config",
        "methods": ["GET", "POST"],
        "description": "系统配置管理",
    })
    endpoints.append({
        "path": "/api/audio/<path:filename>",
        "method": "GET",
        "description": "下载/播放音频和视频文件",
    })
    endpoints.append({
        "path": "/api/auth/check",
        "method": "GET",
        "description": "检查登录状态",
    })
    endpoints.append({
        "path": "/api/login",
        "method": "POST",
        "description": "用户登录",
        "params": {"username": "用户名", "password": "密码"},
    })
    endpoints.append({
        "path": "/api/logout",
        "method": "POST",
        "description": "退出登录",
    })
    endpoints.append({
        "path": "/login",
        "method": "GET",
        "description": "登录页面",
    })
    endpoints.append({
        "path": "/",
        "method": "GET",
        "description": "主页面",
    })
    endpoints.append({
        "path": "/api",
        "method": "GET",
        "description": "API索引（本页面）",
    })

    # Enrich with stats
    try:
        task_count = len(tasks)
        fav_count = len(get_favorites())
        sub_count = len(get_subscriptions())
        ep_count = len(get_episodes().get("episodes", []))
        unread_stats = get_unread_counts_by_subscription()
        total_unread = sum(unread_stats.values()) if unread_stats else 0
    except Exception:
        task_count = 0
        fav_count = 0
        sub_count = 0
        ep_count = 0
        total_unread = 0

    return jsonify({
        "name": "BiliMix API",
        "version": "1.0.0",
        "description": "中英混合播客/视频学习工具",
        "total_endpoints": len(endpoints),
        "endpoints": endpoints,
        "stats": {
            "tasks": task_count,
            "favorites": fav_count,
            "subscriptions": sub_count,
            "episodes": ep_count,
            "unread_episodes": total_unread,
        },
    })
