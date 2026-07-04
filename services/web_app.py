"""
BiliMix Web App - Flask 后端
提供 REST API：提交音频URL、查询进度、获取结果、音频服务
支持：任务终止、历史任务列表、历史任务删除（含文件清理）

重构后的路由层：核心逻辑已分别提取到：
  - task_manager.py: 任务状态管理、持久化、恢复
  - podcast_service.py: 播客搜索、RSS 解析
  - config_manager.py: 配置读取/保存
  - word_frequency.py: BNC/COCA 词频数据
"""
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.request

# 确保项目根目录在 sys.path 中，支持 python services/web_app.py 直接启动
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import functools
from flask import Flask, jsonify, request, send_file, send_from_directory, session, redirect

from core import config
from core.task_manager import (
    tasks, tasks_lock, cancel_flags, task_subprocesses,
    load_tasks_index, save_tasks_index,
    save_task_result_to_disk, restore_task_from_disk,
    update_task, get_task, is_cancelled,
)
from core.database import (
    setup_database, delete_task_from_index, save_task_to_index,
    load_tasks_index, get_subscriptions, add_subscription, remove_subscription,
    add_search_keyword, get_search_suggestions, clear_search_history,
    get_recent_podcasts
)
from services.podcast_service import search_podcasts_itunes, parse_rss_feed

from core.config_manager import get_all_config, update_config

from pipeline.step1_transcribe import transcribe, extract_full_text, extract_word_timestamps
# step2_identify_difficult_words removed (word_replace mode deleted)
from pipeline.step3_tts_confucius import synthesize_sentences_with_confucius_tts
# step4_audio_editor removed (word_replace mode deleted)
from pipeline.step2b_translate_sentences import (
    select_sentences_to_translate,
    translate_sentences,
)
from pipeline.step4b_sentence_mixer import mix_sentence_audio

# Flask 静态文件目录使用绝对路径（web_app.py 已移至 services/ 子目录）
_web_dir = os.path.join(config.BASE_DIR, "web")
app = Flask(__name__, static_folder=_web_dir, static_url_path="")
app.secret_key = getattr(config, "SECRET_KEY", "bilimix-secret-key-change-me")

# 全局任务队列：保证同一时间只有一个任务在运行，后续任务按提交顺序排队（FIFO）
# 用 Condition + deque 实现公平排队，替代非公平的 threading.Lock 轮询
import collections as _collections
_queue_condition = threading.Condition()
_queue_waiters = _collections.deque()  # 按提交顺序存储等待中的 task_id


# ============================================================
# 登录认证
# ============================================================

def login_required(f):
    """装饰器：检查用户是否已登录"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not getattr(config, "AUTH_ENABLED", True):
            return f(*args, **kwargs)
        if not session.get("logged_in"):
            # API 请求返回 401 JSON，页面请求重定向
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录", "code": "AUTH_REQUIRED"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


@app.before_request
def check_auth():
    """全局请求拦截：未登录时只允许访问登录相关路由和静态资源"""
    if not getattr(config, "AUTH_ENABLED", True):
        return None

    # 允许访问的路径（不需要登录）
    allowed_paths = ["/login", "/api/login", "/api/auth/check"]
    if request.path in allowed_paths:
        return None

    # 允许登录页面的静态资源
    if request.path == "/login.html":
        return None

    # 已登录则放行
    if session.get("logged_in"):
        return None

    # 未登录：API 返回 401，页面请求重定向到登录页
    if request.path.startswith("/api/"):
        return jsonify({"error": "未登录", "code": "AUTH_REQUIRED"}), 401

    # 允许加载 CSS/JS/图标等静态资源（登录页面需要用）
    static_exts = (".css", ".js", ".svg", ".png", ".ico", ".woff", ".woff2", ".ttf")
    if any(request.path.endswith(ext) for ext in static_exts):
        return None

    return redirect("/login")


def _run_with_retry(fn, *args, name="", max_retries=None, **kwargs):
    """通用自动重试：调用 fn(*args, **kwargs)，失败自动重试"""
    import time as _time
    if max_retries is None:
        max_retries = getattr(config, "AUTO_RETRY_MAX", 2)
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except InterruptedError:
            raise
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"[Retry] {name} 失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}，"
                      f"{wait}s 后重试...")
                _time.sleep(wait)
            else:
                print(f"[Retry] {name} 重试耗尽 ({max_retries + 1} 次失败): {e}")
    raise last_err


@app.route("/login")
def login_page():
    """登录页面"""
    if session.get("logged_in") and getattr(config, "AUTH_ENABLED", True):
        return redirect("/")
    return send_from_directory(_web_dir, "login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    """登录接口"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请提供登录信息"}), 400

    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    expected_user = getattr(config, "AUTH_USERNAME", "admin")
    expected_pass = getattr(config, "AUTH_PASSWORD", "bilimix2024")

    if username == expected_user and password == expected_pass:
        session["logged_in"] = True
        session["username"] = username
        return jsonify({"ok": True, "message": "登录成功"})
    else:
        return jsonify({"error": "用户名或密码错误"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """退出登录"""
    session.clear()
    return jsonify({"ok": True, "message": "已退出登录"})


@app.route("/api/auth/check")
def api_auth_check():
    """检查登录状态"""
    auth_enabled = getattr(config, "AUTH_ENABLED", True)
    if not auth_enabled:
        return jsonify({"authenticated": True, "auth_enabled": False})
    return jsonify({
        "authenticated": bool(session.get("logged_in")),
        "auth_enabled": True,
        "username": session.get("username", ""),
    })


# ============================================================
# 核心处理流程（在后台线程中执行）
# ============================================================

def download_audio(url: str, save_path: str, task_id: str) -> bool:
    """下载音频文件（支持终止）"""
    try:
        update_task(task_id, message="正在下载音频文件...")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else None
            downloaded = 0
            chunk_size = 1024 * 64

            with open(save_path, "wb") as f:
                while True:
                    if is_cancelled(task_id):
                        f.close()
                        if os.path.exists(save_path):
                            os.remove(save_path)
                        raise InterruptedError("任务已被用户终止")

                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = min(int(downloaded / total * 100), 100)
                        update_task(task_id, message=f"正在下载音频... {pct}%")

        size_mb = os.path.getsize(save_path) / (1024 * 1024)
        update_task(task_id, message=f"音频下载完成 ({size_mb:.1f} MB)")
        return True
    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已终止")
        return False
    except Exception as e:
        update_task(task_id, status="error", message=f"下载失败: {str(e)}")
        return False


def generate_task_id(url: str) -> str:
    """根据URL生成唯一任务ID"""
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _cleanup_intermediate_files(result_dir: str):
    """删除任务完成后不再需要的中间文件（参考音频、TTS 缓存）"""
    cleanup_dirs = [
        "tts_fish_cache",
        "tts_sent_cache",
        "tts_cache",
        "ref_audio",
    ]
    for subdir in cleanup_dirs:
        path = os.path.join(result_dir, subdir)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            print(f"[Cleanup] 已删除: {subdir}/")


def _try_resolve_local_url(url: str) -> str:
    """
    检测是否为本地服务器自己的音频 URL，如果是则解析到本地文件路径。

    支持两种模式：
    1. /api/audio/<basename>/<filename>  → data/results/<basename>/<filename>
    2. /api/audio/<basename>.<ext>       → 依次查 results/、downloads/、项目根目录

    这样可以避免后台下载线程对本地 URL 发起 HTTP 请求导致的 401 认证问题。

    Returns:
        str: 本地文件路径，如果不是本地 URL 或无对应文件则返回空字符串
    """
    # 匹配 /api/audio/<path> 模式
    m = re.search(r'/api/audio/(.+)', url)
    if not m:
        return ""

    # 检查 host 是否指向本地服务器
    from urllib.parse import urlparse
    parsed = urlparse(url)
    local_hosts = {"127.0.0.1", "localhost", "0.0.0.0"}
    hostname = parsed.hostname or ""
    port = parsed.port or 5000

    # 仅当 host 匹配本地或同机器时才解析
    # （外网 URL 不应该被误解析为本地文件）
    if hostname not in local_hosts and not hostname.startswith(("192.168.", "10.", "172.")):
        # 可能是外网 URL，不做本地解析
        return ""

    filepath = m.group(1)

    # 按 serve_audio 的查找顺序查找文件
    search_paths = [
        os.path.join(config.RESULT_DIR, filepath),
        os.path.join(config.DOWNLOAD_DIR, filepath),
        os.path.join(config.BASE_DIR, filepath),
    ]
    for p in search_paths:
        if os.path.isfile(p):
            print(f"[本地解析] {url} → {p}")
            return p

    return ""


# ============================================================
# 处理模式：word_replace（生词替换）
# ============================================================

def process_audio_sentence_mode(task_id: str, audio_path: str):
    """句子翻译模式前半段：转录 → 翻译 → 暂停等待确认"""
    try:
        basename = os.path.splitext(os.path.basename(audio_path))[0]
        result_dir = os.path.join(config.RESULT_DIR, basename)
        os.makedirs(result_dir, exist_ok=True)
        update_task(task_id, _basename=basename, _audio_path=audio_path)

        # ---- Step 1: 转录 ----
        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")
        update_task(task_id, status="processing", step="transcribe",
                    progress=5, message="Step 1/4: 正在转录音频...")

        transcription = transcribe(audio_path)
        full_text = extract_full_text(transcription)
        segments = transcription.get("segments", [])

        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        serialized_segments = [{"text": s.get("text", "").strip(),
                                "start": s.get("start", 0),
                                "end": s.get("end", 0),
                                "speaker": s.get("speaker", "")} for s in segments]
        update_task(task_id, progress=20,
                    message=f"转录完成: {len(segments)} 个句子",
                    transcription_text=full_text, segments=serialized_segments)

        # ---- Step 2: 选择并翻译句子 ----
        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")
        update_task(task_id, step="translate", progress=25,
                    message="Step 2/4: 正在翻译句子...")

        ratio = getattr(config, "SENTENCE_CN_RATIO", 1.0)
        translated_indices = select_sentences_to_translate(serialized_segments, ratio)

        if not translated_indices:
            update_task(task_id, status="completed", progress=100,
                        step="done", message="没有需要翻译的句子。")
            return

        update_task(task_id, progress=30,
                    message=f"将翻译 {len(translated_indices)}/{len(segments)} 个句子 "
                            f"(比例: {ratio*100:.0f}%)")

        def _translate_progress(batch_idx, total_batches):
            pct = 30 + int((batch_idx / max(total_batches, 1)) * 25)
            update_task(task_id, progress=pct,
                        message=f"Step 2/4: 翻译批次 ({batch_idx+1}/{total_batches})")

        def _cancel_check():
            return is_cancelled(task_id)

        def _translate_checkpoint(batch_idx, trans):
            """每批翻译完成后更新内存断点 + 落盘到 task_result.json"""
            update_task(task_id, _checkpoint_translate_batch=batch_idx,
                        _checkpoint_translations=trans)
            save_task_result_to_disk(result_dir, {
                "task_id": task_id, "status": "processing",
                "process_mode": "sentence_translate",
                "_checkpoint_translate_batch": batch_idx,
                "_checkpoint_translations": trans,
            })

        _task = get_task(task_id)
        resume_tl_batch = int(_task.get("_checkpoint_translate_batch", 0))
        resume_tl_trans = _task.get("_checkpoint_translations", None)
        if resume_tl_trans:
            resume_tl_trans = {int(k): v for k, v in resume_tl_trans.items()}

        translations = _run_with_retry(
            translate_sentences,
            serialized_segments, translated_indices,
            cancel_check=_cancel_check, progress_cb=_translate_progress,
            resume_batch=resume_tl_batch,
            existing_translations=resume_tl_trans,
            checkpoint_cb=_translate_checkpoint,
            name="句子翻译")

        update_task(task_id, _checkpoint_translate_batch=0, _checkpoint_translations=None)

        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")
        update_task(task_id, progress=55,
                    message=f"翻译完成: {len(translations)}/{len(translated_indices)} 个句子")

        # 翻译结果立刻落盘，kill 后断点续传能跳过翻译步骤
        save_task_result_to_disk(result_dir, {
            "task_id": task_id, "status": "processing",
            "process_mode": "sentence_translate",
            "transcription_text": full_text,
            "segments": serialized_segments,
            "translations": translations,
            "translated_indices": translated_indices,
        })

        # ---- 确认翻译环节 ----
        task = get_task(task_id)
        skip_confirm = task.get("skip_confirmation",
                                getattr(config, "SKIP_CONFIRMATION", True))

        if skip_confirm:
            print(f"[Sentence] 任务 {task_id[:8]}... 跳过确认，自动继续处理")
            update_task(task_id, translations=translations,
                        translated_indices=translated_indices,
                        _raw_segments=segments,
                        status="processing", step="synthesize", progress=58,
                        message=f"自动确认 {len(translations)} 个翻译，继续处理...")
            continue_after_sentence_confirmation(task_id)
        else:
            update_task(task_id, status="awaiting_sentence_confirmation",
                        step="confirm_sentence", progress=55,
                        message=f"已翻译 {len(translations)} 个句子，请确认后继续",
                        translations=translations,
                        translated_indices=translated_indices,
                        _raw_segments=segments)
            save_task_result_to_disk(result_dir, {
                "task_id": task_id,
                "status": "awaiting_sentence_confirmation",
                "process_mode": "sentence_translate",
                "transcription_text": full_text,
                "segments": serialized_segments,
                "translations": translations,
                "translated_indices": translated_indices,
            })
            print(f"[Sentence] 任务 {task_id[:8]}... 暂停等待用户确认翻译")

    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已被终止")
    except Exception as e:
        traceback.print_exc()
        task = get_task(task_id) or {}
        update_task(task_id, status="error", message=f"处理出错: {str(e)}",
                    _failed_step=task.get("step", "translate"))
    finally:
        task_subprocesses.pop(task_id, None)


# ============================================================
# 句子翻译/智能翻译的共享后半段
# ============================================================

def continue_after_sentence_confirmation(task_id: str):
    """句子翻译模式后半段：TTS 合成 → 音频组装 → 完成"""
    try:
        task = get_task(task_id)
        if not task:
            return

        audio_path = task.get("_audio_path", "")
        basename = task.get("_basename", "")
        translations = task.get("translations", {})
        translated_indices = task.get("translated_indices", [])
        # 优先使用完整 _raw_segments（含 speaker 等字段，声音克隆需 speaker 做说话人匹配）；
        # _raw_segments 缺失时（如断点续传从磁盘恢复）回退到精简 segments
        segments = task.get("_raw_segments") or task.get("segments", [])

        full_text = task.get("transcription_text", "")
        result_dir = os.path.join(config.RESULT_DIR, basename)
        os.makedirs(result_dir, exist_ok=True)

        translations = {int(k): v for k, v in translations.items()}
        translated_indices = sorted([idx for idx in translated_indices
                                     if idx in translations])

        # 翻译已就绪，落盘以便 kill 后断点续传跳过翻译步骤
        mode = "sentence_translate"
        save_task_result_to_disk(result_dir, {
            "task_id": task_id, "status": "processing",
            "process_mode": "sentence_translate",
            "transcription_text": full_text,
            "segments": segments,
            "difficult_words": task.get("difficult_words", []),
            "translations": translations,
            "translated_indices": translated_indices,
        })

        if not translations:
            update_task(task_id, status="completed", progress=100,
                        step="done", message="没有翻译内容，已完成。")
            return

        if task_id not in cancel_flags:
            cancel_flags[task_id] = threading.Event()

        # ---- Step 3: TTS 合成 ----
        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        voice_clone = getattr(config, "SENTENCE_TTS_VOICE_CLONE", True)

        update_task(task_id, status="processing", step="synthesize", progress=60,
                    message="Step 3/4: 合成中文语音...")

        tts_audio_map = {}

        # Confucius4-TTS-CPU: 零样本多语言声音克隆
        confucius_cache_dir = os.path.join(result_dir, "tts_confucius_cache")
        confucius_ref_dir = os.path.join(confucius_cache_dir, "ref_audio")
        os.makedirs(confucius_ref_dir, exist_ok=True)

        from pipeline.ref_audio_utils import (
            extract_ref_audio_for_segments, extract_ref_audio_speaker_local)
        pseudo_replacements = [
            {"segment_index": idx}
            for idx in translated_indices if idx < len(segments)
        ]
        confucius_ref_map = {}
        if voice_clone and pseudo_replacements:
            ref_mode = getattr(config, "REF_SELECT_MODE", "speaker_local")
            if ref_mode == "speaker_local":
                (confucius_ref_map, confucius_ref_source_map,
                 _confucius_ref_text_map) = extract_ref_audio_speaker_local(
                    audio_path, segments, pseudo_replacements,
                    confucius_ref_dir, engine="confucius")
            else:
                confucius_ref_map, confucius_ref_source_map = extract_ref_audio_for_segments(
                    audio_path, segments, pseudo_replacements, confucius_ref_dir)
            print(f"[Confucius] 提取了 {len(confucius_ref_map)} 个参考音频 (mode={ref_mode})")

        def _confucius_progress(current, total):
            pct = 60 + int((current / max(total, 1)) * 20)
            update_task(task_id, progress=pct,
                        message=f"Step 3/4: Confucius4-TTS 句子合成 ({current}/{total})",
                        _tts_completed_count=current)

        def _confucius_cancel():
            return is_cancelled(task_id)

        tts_audio_map = synthesize_sentences_with_confucius_tts(
            segments, translated_indices, translations,
            audio_path, confucius_cache_dir,
            ref_audio_map=confucius_ref_map if voice_clone else {},
            cancel_check=_confucius_cancel, progress_cb=_confucius_progress,
            task_id=task_id)

        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        if len(translated_indices) > 0 and not tts_audio_map:
            raise RuntimeError(
                f"TTS 合成完全失败：{len(translated_indices)} 个句子未生成任何音频，"
                f"请检查 TTS 引擎配置和环境依赖")

        update_task(task_id, progress=80,
                    message=f"语音合成完成: {len(tts_audio_map)} 条中文语音")

        # ---- Step 4: 中英交替音频组装 ----
        update_task(task_id, step="merge", progress=82,
                    message="Step 4/4: 组装中英交替音频...")

        output_audio_path = os.path.join(
            result_dir, f"{basename}_sentence.{config.OUTPUT_FORMAT}")

        mix_result = mix_sentence_audio(
            audio_path=audio_path, segments=segments,
            translated_indices=translated_indices,
            translations=translations,
            tts_audio_map=tts_audio_map,
            output_path=output_audio_path)

        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        # ---- 完成 ----
        result_data = {
            "basename": basename,
            "original_audio": audio_path,
            "mixed_audio": output_audio_path,
            "original_duration": mix_result["original_duration"],
            "mixed_duration": mix_result["mixed_duration"],
            "total_segments": mix_result["total_segments"],
            "translated_segments": mix_result["translated_segments"],
            "process_mode": task.get("process_mode", "sentence_translate"),
        }

        sentence_pairs = []
        for seg_idx in translated_indices:
            if seg_idx in translations and seg_idx < len(segments):
                sentence_pairs.append({
                    "index": seg_idx,
                    "english": segments[seg_idx].get("text", "").strip(),
                    "chinese": translations[seg_idx],
                    "start": segments[seg_idx].get("start", 0),
                    "end": segments[seg_idx].get("end", 0),
                })

        mode = "sentence_translate"

        update_task(task_id, status="completed", step="done", progress=100,
                    message="全部完成！", result=result_data,
                    sentence_pairs=sentence_pairs,
                    time_mapping=mix_result["time_mapping"])

        save_task_result_to_disk(result_dir, {
            "task_id": task_id, "status": "completed",
            "process_mode": "sentence_translate",
            "transcription_text": full_text,
            "segments": segments,
            "difficult_words": task.get("difficult_words", []),
            "translations": translations,
            "translated_indices": translated_indices,
            "sentence_pairs": sentence_pairs,
            "result": result_data,
            "time_mapping": mix_result["time_mapping"],
        })

        _cleanup_intermediate_files(result_dir)

        try:
            dw = task.get("difficult_words", [])
            if dw:
                task_title = task.get("title", "") or basename
                print(f"[Vocab] 已保存 {len(dw)} 个生词到全局生词库 (任务 {task_id[:8]}...)")
        except Exception as ve:
            print(f"[Vocab] 保存生词库失败: {ve}")

    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已被终止")
    except Exception as e:
        traceback.print_exc()
        task = get_task(task_id) or {}
        update_task(task_id, status="error", message=f"处理出错: {str(e)}",
                    _failed_step=task.get("step", "synthesize"))
    finally:
        cancel_flags.pop(task_id, None)
        task_subprocesses.pop(task_id, None)








# ============================================================
# API 路由 — 播客收藏
# ============================================================







def api_check_favorite():
    """检查是否已收藏"""
    rss_url = request.args.get("rss_url", "").strip()
    if not rss_url:
        return jsonify({"error": "请提供 rss_url"}), 400


# ============================================================
# API 路由 — RSS 订阅管理
# ============================================================

@app.route("/api/subscriptions")
def api_get_subscriptions():
    """获取全部 RSS 订阅"""
    return jsonify({"subscriptions": get_subscriptions()})


@app.route("/api/subscriptions", methods=["POST"])
def api_add_subscription():
    """添加 RSS 订阅"""
    data = request.get_json()
    if not data or "rss_url" not in data:
        return jsonify({"error": "请提供 rss_url"}), 400
    sub = add_subscription(
        title=data.get("title", ""),
        author=data.get("author", ""),
        image=data.get("image", ""),
        rss_url=data["rss_url"],
    )
    return jsonify({"ok": True, "subscription": sub})


@app.route("/api/subscriptions", methods=["DELETE"])
def api_remove_subscription():
    """移除 RSS 订阅"""
    data = request.get_json()
    if not data or "rss_url" not in data:
        return jsonify({"error": "请提供 rss_url"}), 400
    remove_subscription(data["rss_url"])
    return jsonify({"ok": True})


# ============================================================
# API 路由 — 搜索历史
# ============================================================

@app.route("/api/search-history/suggestions")
def api_search_suggestions():
    """获取搜索建议"""
    prefix = request.args.get("q", "").strip()
    suggestions = get_search_suggestions(prefix)
    return jsonify({"suggestions": suggestions})


@app.route("/api/search-history", methods=["POST"])
def api_add_search_history():
    """记录搜索关键词"""
    data = request.get_json()
    if data and "keyword" in data:
        add_search_keyword(data["keyword"])
    return jsonify({"ok": True})


@app.route("/api/search-history", methods=["DELETE"])
def api_clear_search_history():
    """清空搜索历史"""
    clear_search_history()
    return jsonify({"ok": True})


# ============================================================
# API 路由 — 最近使用的播客
# ============================================================

@app.route("/api/recent-podcasts")
def api_recent_podcasts():
    """获取最近使用的播客源"""
    podcasts = get_recent_podcasts()
    return jsonify({"podcasts": podcasts})





# ============================================================
# API 路由 — 播客搜索
# ============================================================

@app.route("/api/podcast/search")
def podcast_search():
    """搜索播客"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "请提供搜索关键词 q"}), 400
    # 记录搜索历史
    add_search_keyword(q)
    result = search_podcasts_itunes(q)
    if "error" in result:
        return jsonify({"error": result["error"]}), 500
    return jsonify(result)


@app.route("/api/podcast/rss")
def podcast_rss():
    """通过 RSS Feed URL 解析播客单集列表"""
    feed_url = request.args.get("url", "").strip()
    if not feed_url:
        return jsonify({"error": "请提供 RSS Feed URL"}), 400
    if not feed_url.startswith(("http://", "https://")):
        return jsonify({"error": "请提供有效的 HTTP/HTTPS URL"}), 400
    result = parse_rss_feed(feed_url)
    if "error" in result:
        return jsonify({"error": result["error"]}), 500
    return jsonify(result)


# ============================================================
# API 路由 — 任务操作
# ============================================================

@app.route("/")
def index():
    """主页"""
    return send_from_directory(_web_dir, "index.html")


@app.route("/api/upload", methods=["POST"])
def upload_audio():
    """上传本地音频文件"""
    if "file" not in request.files:
        return jsonify({"error": "请选择文件"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400

    # 校验文件扩展名
    ext = os.path.splitext(f.filename)[1].lower()
    allowed = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
    if ext not in allowed:
        return jsonify({"error": f"不支持的文件格式: {ext}，支持 {', '.join(sorted(allowed))}"}), 400

    # 生成安全文件名（保留原始文件名但防止路径遍历）
    safe_name = os.path.basename(f.filename)
    save_path = os.path.join(config.DOWNLOAD_DIR, safe_name)

    # 如果已存在同名文件，加时间戳
    if os.path.exists(save_path):
        name, e = os.path.splitext(safe_name)
        save_path = os.path.join(config.DOWNLOAD_DIR,
                                 f"{name}_{int(time.time())}{e}")

    f.save(save_path)
    size_mb = os.path.getsize(save_path) / (1024 * 1024)

    print(f"[Upload] 已保存: {save_path} ({size_mb:.1f} MB)")

    return jsonify({
        "ok": True,
        "local_path": save_path,
        "filename": os.path.basename(save_path),
        "size_mb": round(size_mb, 1),
    })


@app.route("/api/submit", methods=["POST"])
def submit_task():
    """提交音频处理任务"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请提供数据"}), 400

    local_path = data.get("local_path", "").strip()
    audio_url = data.get("url", "").strip()

    if not local_path and not audio_url:
        return jsonify({"error": "请提供音频URL或上传本地文件"}), 400

    # 优先使用本地文件路径
    if local_path:
        if not os.path.isfile(local_path):
            return jsonify({"error": f"文件不存在: {local_path}"}), 400
        # 本地文件：用文件路径构造虚拟 URL 作为任务标识
        audio_url = f"file://{local_path}"
    elif not audio_url.startswith(("http://", "https://")):
        return jsonify({"error": "请提供有效的HTTP/HTTPS URL"}), 400

    # Always use sentence_translate mode with 100% translation
    process_mode = "sentence_translate"
    skip_confirmation = data.get("skip_confirmation",
                                 getattr(config, "SKIP_CONFIRMATION", True))
    title = data.get("title", "").strip()

    task_id = generate_task_id(audio_url + str(time.time()))

    cancel_flags[task_id] = threading.Event()

    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    with tasks_lock:
        tasks[task_id] = {
            "task_id": task_id,
            "url": audio_url,
            "title": title,
            "process_mode": process_mode,
            "skip_confirmation": skip_confirmation,
            "status": "downloading",
            "step": "download",
            "progress": 0,
            "message": "任务已创建，准备下载...",
            "created_at": created_at,
            "transcription_text": "",
            "segments": [],
            "difficult_words": [],
            "replacements": [],
            "translations": {},
            "translated_indices": [],
            "result": None,
            "_basename": "",
            "_audio_path": "",
        }

    # 任务创建后立即持久化到 SQLite，避免进程中途退出后历史记录丢失
    try:
        save_task_to_index(task_id, {
            "task_id": task_id,
            "url": audio_url,
            "title": title,
            "process_mode": process_mode,
            "status": "downloading",
            "progress": 0,
            "message": "任务已创建",
            "created_at": created_at,
            "basename": "",
            "total_words": 0,
            "total_replacements": 0,
            "original_duration": 0,
            "mixed_duration": 0,
        })
    except Exception as e:
        print(f"[WARN] 任务创建时持久化失败: {e}")

    def worker():
        # 排队等待：同一时间只允许一个任务运行，按提交顺序 FIFO
        update_task(task_id, status="queued",
                    message="排队中，请稍候…")
        print(f"[Queue] 任务 {task_id[:8]}... 排队等待")

        with _queue_condition:
            _queue_waiters.append(task_id)

            # 等待直到成为队首（FIFO 公平排队）
            while _queue_waiters[0] != task_id:
                if is_cancelled(task_id) or not get_task(task_id):
                    print(f"[Queue] 任务 {task_id[:8]}... 排队中被取消，退出")
                    _queue_waiters.remove(task_id)
                    _queue_condition.notify_all()
                    return
                _queue_condition.wait(timeout=1.0)

        try:
            # 成为队首后再次确认任务未被取消/删除
            if is_cancelled(task_id) or not get_task(task_id):
                print(f"[Queue] 任务 {task_id[:8]}... 获取执行权后检测到已取消，退出")
                return
            update_task(task_id, status="processing",
                        message="开始处理…")
            _run_worker(task_id, audio_url)
        finally:
            # 执行完成（或异常退出），从队列移除并通知下一个等待者
            with _queue_condition:
                if task_id in _queue_waiters:
                    _queue_waiters.remove(task_id)
                _queue_condition.notify_all()

    def _run_worker(task_id, audio_url):
        # 处理 file:// 协议（用户上传的本地文件）
        is_file_url = audio_url.startswith("file://")
        if is_file_url:
            local_path = audio_url[len("file://"):]
            if os.path.isfile(local_path):
                local_file = local_path
            else:
                update_task(task_id, status="error",
                            message=f"文件不存在: {local_path}")
                return
        else:
            # 尝试解析本地 HTTP URL 到本地文件路径（避免 401 认证）
            local_file = _try_resolve_local_url(audio_url)

        if local_file:
            # 本地文件，跳过下载
            basename = os.path.splitext(os.path.basename(local_file))[0]
            audio_path = local_file
            update_task(task_id, _basename=basename, _audio_path=audio_path)
            size_mb = os.path.getsize(local_file) / (1024 * 1024)
            update_task(task_id, progress=5,
                        message=f"使用本地音频文件 ({size_mb:.1f} MB)")
        else:
            basename = hashlib.md5(audio_url.encode()).hexdigest()
            ext = os.path.splitext(audio_url.split("?")[0])[-1] or ".mp3"
            if ext not in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
                ext = ".mp3"
            audio_path = os.path.join(config.DOWNLOAD_DIR, f"{basename}{ext}")
            update_task(task_id, _basename=basename, _audio_path=audio_path)

            if os.path.exists(audio_path):
                update_task(task_id, progress=5, message="音频文件已存在，跳过下载")
            else:
                if not download_audio(audio_url, audio_path, task_id):
                    return

        task = get_task(task_id)
        mode = "sentence_translate"
        process_audio_sentence_mode(task_id, audio_path)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    return jsonify({"task_id": task_id, "message": "任务已提交"})


@app.route("/api/task/<task_id>/cancel", methods=["POST"])
def cancel_task(task_id):
    """终止正在运行的任务"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    status = task.get("status", "")
    if status in ("completed", "error", "cancelled"):
        return jsonify({"error": f"任务已是 {status} 状态，无需终止"}), 400

    event = cancel_flags.get(task_id)
    if event:
        event.set()

    proc = task_subprocesses.get(task_id)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    update_task(task_id, status="cancelled", message="任务已被终止")
    return jsonify({"message": "任务终止请求已发送"})




@app.route("/api/task/<task_id>/confirm_sentences", methods=["POST"])
def confirm_sentences(task_id):
    """用户确认翻译后，继续执行句子翻译模式的后续流程"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task.get("status") != "awaiting_sentence_confirmation":
        return jsonify({"error": f"任务状态为 {task.get('status')}，不在等待句子确认状态"}), 400

    data = request.get_json()
    if not data or "translations" not in data:
        return jsonify({"error": "请提供 translations"}), 400

    confirmed_translations = {int(k): v for k, v in data["translations"].items()}
    confirmed_indices = data.get("translated_indices", [])
    if confirmed_indices:
        confirmed_indices = [int(i) for i in confirmed_indices]
    else:
        confirmed_indices = sorted(confirmed_translations.keys())

    update_task(task_id, translations=confirmed_translations,
                translated_indices=confirmed_indices,
                status="processing", step="synthesize", progress=58,
                message=f"用户确认 {len(confirmed_translations)} 个翻译，继续处理...")

    thread = threading.Thread(target=continue_after_sentence_confirmation,
                              args=(task_id,), daemon=True)
    thread.start()
    return jsonify({"message": f"已确认 {len(confirmed_translations)} 个翻译，继续处理"})


@app.route("/api/task/<task_id>/retry", methods=["POST"])
def retry_task(task_id):
    """通用断点续传：检查已有数据，跳过已完成的步骤"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    if task.get("status") != "error":
        return jsonify({"error": f"任务状态为 {task.get('status')}，仅 error 状态可重试"}), 400

    mode = "sentence_translate"
    audio_path = task.get("_audio_path", "")

    if not audio_path:
        return jsonify({"error": "任务缺少音频路径，无法重试"}), 400

    # 尝试从 task_result.json 加载断点数据（内存恢复可能缺失）
    basename = task.get("_basename", "")
    if basename:
        result_dir = os.path.join(config.RESULT_DIR, basename)
        checkpoint_path = os.path.join(result_dir, "task_result.json")
        if os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # 恢复关键数据到内存 task
                for key in ("segments", "translations", "translated_indices",
                            "sentence_pairs", "tts_audio_map",
                            "_checkpoint_translate_batch",
                            "_checkpoint_translations",
                            "_checkpoint_tts_idx"):
                    if key in saved and not task.get(key):
                        task[key] = saved[key]
                if saved.get("_checkpoint_translations"):
                    task["_checkpoint_translations"] = {
                        int(k): v for k, v
                        in saved["_checkpoint_translations"].items()
                    }
                update_task(task_id, **{k: task[k] for k in task
                                        if k.startswith("_checkpoint_")})
            except Exception as e:
                print(f"[Retry] 加载断点数据失败: {e}")

    # 判断已有数据，确定从哪步恢复
    segments = task.get("segments", [])
    translations = task.get("translations", {})
    translated_indices = task.get("translated_indices", [])
    difficult_words = task.get("difficult_words", [])

    if True:  # always sentence_translate

        if translations and translated_indices and segments:
            # 翻译已完成，直接从 TTS 合成恢复（只补缺失的 TTS 文件）
            msg = f"断点续传 — 跳过转录+翻译，从 TTS 恢复 ({len(translated_indices)} 句)"
            print(f"[Retry] {msg}")
            update_task(task_id, status="processing", step="synthesize",
                        _failed_step="", progress=58,
                        message=msg)
            thread = threading.Thread(target=_synthesis_resume,
                                      args=(task_id,), daemon=True)
        elif segments:
            # 转录已完成，从翻译步骤恢复
            msg = f"断点续传 — 跳过转录，从翻译恢复 ({len(segments)} 句)"
            print(f"[Retry] {msg}")
            update_task(task_id, status="processing", step="translate",
                        _failed_step="", progress=20,
                        message=msg)
            thread = threading.Thread(target=process_audio_sentence_mode,
                                      args=(task_id, audio_path), daemon=True)
        else:
            # 从头开始
            update_task(task_id, status="processing", step="transcribe",
                        _failed_step="", progress=0,
                        message="断点续传 — 从头开始…")
            thread = threading.Thread(target=process_audio_sentence_mode,
                                      args=(task_id, audio_path), daemon=True)
    else:
        update_task(task_id, status="processing", step="transcribe",
                    _failed_step="", progress=0,
                    message="断点续传 — 从头开始…")
        thread = threading.Thread(target=process_audio_sentence_mode,
                                  args=(task_id, audio_path), daemon=True)

    thread.start()
    return jsonify({"message": "已开始断点续传"})


def _synthesis_resume(task_id):
    """断点续传：跳过转录+翻译，只补充合成缺失的 TTS 文件并重新混合"""
    task = get_task(task_id)
    if not task: return
    segments = task.get("segments", [])
    translated_indices = task.get("translated_indices", [])
    translations = task.get("translations", {})
    translations = {int(k): v for k, v in translations.items()}
    audio_path = task.get("_audio_path", "")
    basename = task.get("_basename", "")
    result_dir = os.path.join(config.RESULT_DIR, basename)

    existing_tts = task.get("tts_audio_map", {}) or {}
    missing_indices = [idx for idx in translated_indices if idx not in existing_tts]

    if not missing_indices:
        tts_audio_map = existing_tts
        print(f"[SynthesisResume] 全部 {len(tts_audio_map)} 条 TTS 已就绪，直接混合")
    else:
        missing_translations = {k: translations[k] for k in missing_indices if k in translations}
        confucius_cache_dir = os.path.join(result_dir, "tts_confucius_cache")
        os.makedirs(confucius_cache_dir, exist_ok=True)

        def _progress(current, total):
            update_task(task_id, progress=60 + int((current / max(total, 1)) * 20),
                        message=f"补充合成 TTS ({current}/{total})")

        def _cancel(): return is_cancelled(task_id)

        update_task(task_id, status="processing", step="synthesize",
                    progress=60, message=f"补充合成 {len(missing_indices)} 条 TTS...")
        try:
            new_tts = synthesize_sentences_with_confucius_tts(
                segments, missing_indices, missing_translations,
                audio_path, confucius_cache_dir,
                ref_audio_map={},
                cancel_check=_cancel, progress_cb=_progress, task_id=task_id)
        except InterruptedError:
            update_task(task_id, status="cancelled", message="重试已被取消")
            return
        tts_audio_map = dict(existing_tts)
        tts_audio_map.update(new_tts)
        update_task(task_id, tts_audio_map=tts_audio_map)

    update_task(task_id, status="processing", step="merge", progress=82,
                message="重新组装音频...")
    output_path = os.path.join(result_dir, f"{basename}_sentence.{config.OUTPUT_FORMAT}")
    mix_result = mix_sentence_audio(audio_path, segments,
                                    translated_indices, translations,
                                    tts_audio_map, output_path)
    
    # 完成
    result_data = {
        "basename": basename,
        "original_audio": audio_path,
        "mixed_audio": output_path,
        "original_duration": mix_result["original_duration"],
        "mixed_duration": mix_result["mixed_duration"],
        "total_segments": len(segments),
        "translated_segments": len(translated_indices),
    }
    update_task(task_id, status="completed", progress=100,
                step="done", message="断点续传完成!",
                result=result_data, sentence_pairs=task.get("sentence_pairs", []))
    save_task_result_to_disk(result_dir, {**result_data, "task_id": task_id,
                              "status": "completed", "segments": segments,
                              "translations": translations,
                              "translated_indices": translated_indices,
                              "process_mode": "sentence_translate"})
    save_task_to_index(task_id, {"status": "completed", "progress": 100,
                       "step": "done", "message": "断点续传完成!"})


@app.route("/api/task/<task_id>/retry-synthesis", methods=["POST"])
def retry_sentence_synthesis(task_id):
    """TTS 合成失败后手动重试"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    status = task.get("status", "")
    if status not in ("error",):
        return jsonify({"error": f"任务状态为 {status}，仅 error 状态可重试合成"}), 400

    segments = task.get("segments", [])
    translated_indices = task.get("translated_indices", [])
    translations = task.get("translations", {})
    translations = {int(k): v for k, v in translations.items()}
    audio_path = task.get("_audio_path", "")
    basename = task.get("_basename", "")

    if not segments or not translated_indices or not translations:
        return jsonify({"error": "任务数据不完整，无法重试"}), 400

    # 找出已有 TTS 和缺失 TTS 的 segment
    existing_tts = task.get("tts_audio_map", {}) or {}
    missing_indices = [idx for idx in translated_indices
                       if idx not in existing_tts]

    if not missing_indices:
        # 全部已有 TTS，可直接混合
        tts_audio_map = existing_tts
    else:
        # 只合成缺失的部分
        result_dir = os.path.join(config.RESULT_DIR, basename)
        confucius_cache_dir = os.path.join(result_dir, "tts_confucius_cache")
        os.makedirs(confucius_cache_dir, exist_ok=True)

        missing_translations = {k: translations[k] for k in missing_indices
                                if k in translations}

        update_task(task_id, status="processing", step="retry_synthesis",
                    progress=60, message=f"重新合成 {len(missing_indices)} 条 TTS...")

        def _retry_progress(current, total):
            pct = 60 + int((current / max(total, 1)) * 20)
            update_task(task_id, progress=pct,
                        message=f"TTS 重试合成 ({current}/{total})")

        def _retry_cancel():
            return is_cancelled(task_id)

        try:
            new_tts = synthesize_sentences_with_confucius_tts(
                segments, missing_indices, missing_translations,
                audio_path, confucius_cache_dir,
                ref_audio_map={},
                cancel_check=_retry_cancel, progress_cb=_retry_progress,
                task_id=task_id)
        except InterruptedError:
            update_task(task_id, status="cancelled", message="重试已被取消")
            return jsonify({"message": "重试已取消"})

        # 合并新旧 TTS
        tts_audio_map = dict(existing_tts)
        tts_audio_map.update(new_tts)
        update_task(task_id, tts_audio_map=tts_audio_map)

    # 执行音频混合
    update_task(task_id, step="retry_merge", progress=82,
                message="重新混合音频...")

    output_audio_path = os.path.join(
        config.RESULT_DIR, basename,
        f"{basename}_sentence.{config.OUTPUT_FORMAT}")

    mix_result = mix_sentence_audio(
        audio_path=audio_path, segments=segments,
        translated_indices=translated_indices,
        translations=translations,
        tts_audio_map=tts_audio_map,
        output_path=output_audio_path)

    # 构建完整结果
    full_text = task.get("transcription_text", "")
    result_data = {
        "basename": basename,
        "original_audio": audio_path,
        "mixed_audio": output_audio_path,
        "original_duration": mix_result["original_duration"],
        "mixed_duration": mix_result["mixed_duration"],
        "total_segments": mix_result["total_segments"],
        "translated_segments": mix_result["translated_segments"],
        "process_mode": task.get("process_mode", "sentence_translate"),
    }

    sentence_pairs = []
    for seg_idx in translated_indices:
        if seg_idx in translations and seg_idx < len(segments):
            sentence_pairs.append({
                "index": seg_idx,
                "english": segments[seg_idx].get("text", "").strip(),
                "chinese": translations[seg_idx],
                "start": segments[seg_idx].get("start", 0),
                "end": segments[seg_idx].get("end", 0),
            })

    update_task(task_id, status="completed", step="done", progress=100,
                message="全部完成！", result=result_data,
                sentence_pairs=sentence_pairs,
                time_mapping=mix_result["time_mapping"],
                tts_audio_map=tts_audio_map)

    save_task_result_to_disk(result_dir, {
        "task_id": task_id, "status": "completed",
        "process_mode": task.get("process_mode", "sentence_translate"),
        "transcription_text": full_text,
        "segments": segments,
        "translations": translations,
        "translated_indices": translated_indices,
        "sentence_pairs": sentence_pairs,
        "result": result_data,
        "time_mapping": mix_result["time_mapping"],
    })

    _cleanup_intermediate_files(result_dir)

    return jsonify({"message": "重试完成", "result": result_data})



@app.route("/api/config")
def api_get_config():
    """返回前端需要的全部配置"""
    return jsonify(get_all_config())


@app.route("/api/config", methods=["POST"])
def api_save_config():
    """保存配置（更新内存 + 写回文件）"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无效的配置数据"}), 400

    updated, error = update_config(data)
    if error:
        return jsonify({"error": error, "updated": updated}), 207
    return jsonify({"ok": True, "updated": updated})


# ============================================================
# API 路由 — 任务查询与管理
# ============================================================

@app.route("/api/tasks")
def list_tasks():
    """获取所有历史任务列表"""
    index = load_tasks_index()
    result = {}
    for tid, summary in index.items():
        result[tid] = summary

    with tasks_lock:
        for tid, task in tasks.items():
            result[tid] = {
                "task_id": task.get("task_id"),
                "url": task.get("url", ""),
                "title": task.get("title", ""),
                "status": task.get("status"),
                "progress": task.get("progress", 0),
                "message": task.get("message", ""),
                "created_at": task.get("created_at", ""),
                "basename": (task.get("result", {}).get("basename", "")
                             if task.get("result") else task.get("_basename", "")),
                "total_words": (task.get("result", {}).get("total_words", 0)
                                if task.get("result") else 0),
                "total_replacements": (task.get("result", {}).get("total_replacements", 0)
                                       if task.get("result") else 0),
                "original_duration": (task.get("result", {}).get("original_duration", 0)
                                      if task.get("result") else 0),
                "mixed_duration": (task.get("result", {}).get("mixed_duration", 0)
                                   if task.get("result") else 0),
            }

    sorted_tasks = sorted(result.values(),
                          key=lambda t: t.get("created_at", ""), reverse=True)
    return jsonify({"tasks": sorted_tasks})


@app.route("/api/task/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    """删除历史任务及其关联文件"""
    task = get_task(task_id)
    index = load_tasks_index()

    if not task and task_id not in index:
        return jsonify({"error": "任务不存在"}), 404

    # 如果任务还在运行，先终止
    if task and task.get("status") in ("downloading", "processing", "queued"):
        event = cancel_flags.get(task_id)
        if event:
            event.set()
        proc = task_subprocesses.get(task_id)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    basename = ""
    audio_path = ""
    if task:
        basename = task.get("_basename", "")
        audio_path = task.get("_audio_path", "")
        if not basename and task.get("result"):
            basename = task["result"].get("basename", "")
    if not basename and task_id in index:
        basename = index[task_id].get("basename", "")

    cleaned = []
    if basename:
        result_dir = os.path.join(config.RESULT_DIR, basename)
        if os.path.isdir(result_dir):
            shutil.rmtree(result_dir, ignore_errors=True)
            cleaned.append(f"data/results/{basename}/")

        output_json = os.path.join(config.OUTPUT_DIR, f"{basename}.json")
        if os.path.exists(output_json):
            os.remove(output_json)
            cleaned.append(f"data/transcripts/{basename}.json")

        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
            cleaned.append(os.path.basename(audio_path))
        else:
            for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
                p = os.path.join(config.DOWNLOAD_DIR, f"{basename}{ext}")
                if os.path.exists(p):
                    os.remove(p)
                    cleaned.append(f"data/downloads/{basename}{ext}")

    with tasks_lock:
        tasks.pop(task_id, None)
    cancel_flags.pop(task_id, None)
    task_subprocesses.pop(task_id, None)

    if task_id in index:
        delete_task_from_index(task_id)

    return jsonify({"message": "任务已删除", "cleaned_files": cleaned})


@app.route("/api/task/<task_id>")
def get_task_status(task_id):
    """查询任务状态"""
    task = get_task(task_id)
    if not task:
        task = restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    return jsonify({
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "step": task.get("step"),
        "progress": task.get("progress", 0),
        "message": task.get("message", ""),
        "process_mode": task.get("process_mode", "word_replace"),
    })


@app.route("/api/task/<task_id>/result")
def get_task_result(task_id):
    """获取完整任务结果"""
    task = get_task(task_id)
    if not task:
        task = restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    return jsonify({
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "progress": task.get("progress", 0),
        "message": task.get("message", ""),
        "process_mode": task.get("process_mode", "word_replace"),
        "title": task.get("title", ""),
        "transcription_text": task.get("transcription_text", ""),
        "segments": task.get("segments", []),
        "difficult_words": task.get("difficult_words", []),
        "replacements": task.get("replacements", []),
        "translations": task.get("translations", {}),
        "translated_indices": task.get("translated_indices", []),
        "sentence_pairs": task.get("sentence_pairs", []),
        "result": task.get("result"),
        "time_mapping": task.get("time_mapping", []),
    })


@app.route("/api/audio/<path:filename>")
def serve_audio(filename):
    """服务音频文件"""
    mime_map = {
        ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".m4a": "audio/mp4", ".ogg": "audio/ogg",
        ".flac": "audio/flac", ".aac": "audio/aac",
    }
    ext = os.path.splitext(filename)[1].lower()
    mime = mime_map.get(ext, "audio/mpeg")

    result_path = os.path.join(config.RESULT_DIR, filename)
    if os.path.exists(result_path):
        return send_file(result_path, mimetype=mime)

    download_path = os.path.join(config.DOWNLOAD_DIR, filename)
    if os.path.exists(download_path):
        return send_file(download_path, mimetype=mime)

    root_path = os.path.join(config.BASE_DIR, filename)
    if os.path.exists(root_path):
        return send_file(root_path, mimetype=mime)

    return jsonify({"error": "文件不存在"}), 404


# ============================================================
# API 索引 — 暴露全部接口供 Agent 调用
# ============================================================

@app.route("/api")
def api_index():
    """返回所有可用 API 端点的元信息，方便 Agent 发现和调用"""
    endpoints = [
        {
            "path": "/api/submit",
            "method": "POST",
            "summary": "提交音频处理任务",
            "description": "下载远程音频并进行转录、生词识别、TTS合成、混合音频等处理",
            "request_body": {
                "content_type": "application/json",
                "fields": {
                    "url": {"type": "string", "required": True, "description": "音频文件的 HTTP/HTTPS URL"},
                    "difficulty": {"type": "string", "required": False, "description": "难度等级，如 CET-4, CET-6, IELTS, GRE 等", "default": "CET-4"},
                    "process_mode": {"type": "string", "required": False, "description": "处理模式（固定为 sentence_translate 全文翻译）", "default": "sentence_translate"},
                    "skip_confirmation": {"type": "boolean", "required": False, "description": "是否跳过人工确认步骤", "default": True},
                },
            },
            "response": {"task_id": "string", "message": "string"},
        },
        {
            "path": "/api/tasks",
            "method": "GET",
            "summary": "获取所有历史任务列表",
            "description": "返回所有任务（含内存中运行的和磁盘上已完成的），按创建时间倒序排列",
            "response": {
                "tasks": [{"task_id": "string", "url": "string", "status": "string",
                           "difficulty": "string", "progress": "number",
                           "message": "string", "created_at": "string",
                           "basename": "string", "total_words": "number",
                           "total_replacements": "number",
                           "original_duration": "number", "mixed_duration": "number"}]
            },
        },
        {
            "path": "/api/task/<task_id>",
            "method": "GET",
            "summary": "查询任务状态",
            "description": "返回指定任务的当前状态、进度、步骤等信息",
            "params": {"task_id": {"in": "path", "type": "string", "required": True, "description": "任务ID"}},
            "response": {"task_id": "string", "status": "string", "step": "string",
                         "progress": "number", "message": "string", "process_mode": "string"},
        },
        {
            "path": "/api/task/<task_id>/result",
            "method": "GET",
            "summary": "获取完整任务结果",
            "description": "返回任务的转录文本、分段、生词、替换、翻译、混合音频路径等完整结果",
            "params": {"task_id": {"in": "path", "type": "string", "required": True, "description": "任务ID"}},
            "response": {"task_id": "string", "status": "string", "process_mode": "string",
                         "transcription_text": "string", "segments": "array",
                         "difficult_words": "array", "replacements": "array",
                         "translations": "object", "translated_indices": "array",
                         "sentence_pairs": "array", "result": "object", "time_mapping": "array"},
        },
        {
            "path": "/api/task/<task_id>/cancel",
            "method": "POST",
            "summary": "终止正在运行的任务",
            "description": "向指定任务发送终止信号，任务状态变为 cancelled",
            "params": {"task_id": {"in": "path", "type": "string", "required": True, "description": "任务ID"}},
            "response": {"message": "string"},
        },
        {
            "path": "/api/task/<task_id>/confirm",
            "method": "POST",
            "summary": "确认生词列表并继续处理",
            "description": "在 word_replace 模式下，用户确认/编辑生词后提交，任务继续执行 TTS 合成和混合",
            "params": {"task_id": {"in": "path", "type": "string", "required": True, "description": "任务ID"}},
            "request_body": {
                "content_type": "application/json",
                "fields": {
                    "difficult_words": {"type": "array", "required": True,
                                        "description": "确认后的生词列表，每项包含 word, translation, start, end 等字段"},
                },
            },
            "response": {"message": "string"},
        },
        {
            "path": "/api/task/<task_id>/confirm_sentences",
            "method": "POST",
            "summary": "确认句子翻译并继续处理",
            "description": "在句子翻译模式下，用户确认/编辑翻译后提交",
            "params": {"task_id": {"in": "path", "type": "string", "required": True, "description": "任务ID"}},
            "request_body": {
                "content_type": "application/json",
                "fields": {
                    "translations": {"type": "object", "required": True,
                                     "description": "翻译映射 {句子索引: 翻译文本}"},
                    "translated_indices": {"type": "array", "required": False,
                                           "description": "需要翻译的句子索引列表"},
                },
            },
            "response": {"message": "string"},
        },
        {
            "path": "/api/task/<task_id>",
            "method": "DELETE",
            "summary": "删除历史任务及其关联文件",
            "description": "删除指定任务的所有数据，包括下载文件、结果目录、索引记录等",
            "params": {"task_id": {"in": "path", "type": "string", "required": True, "description": "任务ID"}},
            "response": {"message": "string", "cleaned_files": "array"},
        },
        {
            "path": "/api/translate",
            "method": "POST",
            "summary": "翻译单个英文词/短语为中文",
            "description": "调用 LLM 将英文单词或短语翻译为中文，可提供上下文句子以提升翻译准确度",
            "request_body": {
                "content_type": "application/json",
                "fields": {
                    "english": {"type": "string", "required": True, "description": "待翻译的英文单词或短语"},
                    "context_sentence": {"type": "string", "required": False, "description": "单词所在的上下文句子"},
                },
            },
            "response": {"english": "string", "chinese": "string"},
        },
        {
            "path": "/api/word-levels",
            "method": "POST",
            "summary": "查询单词的 BNC/COCA 词频等级",
            "description": "批量查询单词在 BNC/COCA 词频表中的等级（如 CET-4, CET-6, GRE 等）",
            "request_body": {
                "content_type": "application/json",
                "fields": {
                    "words": {"type": "array", "required": True, "description": "待查询的单词列表"},
                },
            },
            "response": {"levels": "object", "level_nums": "object"},
        },
        {
            "path": "/api/config",
            "method": "GET",
            "summary": "获取当前配置",
            "description": "返回前端需要的全部配置项（难度、TTS引擎、API key 等）",
            "response": "object (所有配置键值对)",
        },
        {
            "path": "/api/config",
            "method": "POST",
            "summary": "保存/更新配置",
            "description": "更新配置项，同时写入内存和配置文件",
            "request_body": {
                "content_type": "application/json",
                "fields": "任意配置键值对，如 {\"DIFFICULTY_LEVEL\": \"CET-6\", \"TTS_ENGINE\": \"edge\"}",
            },
            "response": {"ok": True, "updated": "array"},
        },
        {
            "path": "/api/audio/<filename>",
            "method": "GET",
            "summary": "获取音频文件",
            "description": "按文件名提供音频文件流，依次在 results/、downloads/、项目根目录查找",
            "params": {"filename": {"in": "path", "type": "string", "required": True,
                                    "description": "音频文件名，如 {basename}.mp3 或 {basename}/{basename}_mixed.mp3"}},
            "response": "audio binary stream",
        },
        {
            "path": "/api/podcast/search",
            "method": "GET",
            "summary": "搜索播客",
            "description": "通过关键词搜索播客节目",
            "params": {"q": {"in": "query", "type": "string", "required": True, "description": "搜索关键词"}},
            "response": "object (搜索结果)",
        },
        {
            "path": "/api/podcast/rss",
            "method": "GET",
            "summary": "解析播客 RSS Feed",
            "description": "通过 RSS Feed URL 获取播客单集列表",
            "params": {"url": {"in": "query", "type": "string", "required": True, "description": "RSS Feed 的 HTTP/HTTPS URL"}},
            "response": "object (单集列表)",
        },
        {
            "path": "/api/favorites",
            "method": "GET",
            "summary": "获取全部播客收藏",
            "response": {"favorites": "array"},
        },
        {
            "path": "/api/favorites",
            "method": "POST",
            "summary": "添加播客收藏",
            "request_body": {"fields": {"title": "string", "author": "string", "image": "string", "rss_url": "string (required)"}},
            "response": {"ok": True, "favorite": "object"},
        },
        {
            "path": "/api/favorites",
            "method": "DELETE",
            "summary": "移除播客收藏",
            "request_body": {"fields": {"rss_url": "string (required)"}},
            "response": {"ok": True},
        },
        {
            "path": "/api/favorites/check",
            "method": "GET",
            "summary": "检查是否已收藏",
            "params": {"rss_url": {"in": "query", "type": "string", "required": True}},
        },
        {
            "path": "/api/subscriptions",
            "method": "GET",
            "summary": "获取全部 RSS 订阅",
            "response": {"subscriptions": "array"},
        },
        {
            "path": "/api/subscriptions",
            "method": "POST",
            "summary": "添加 RSS 订阅",
            "request_body": {"fields": {"title": "string", "author": "string", "image": "string", "rss_url": "string (required)"}},
            "response": {"ok": True, "subscription": "object"},
        },
        {
            "path": "/api/subscriptions",
            "method": "DELETE",
            "summary": "移除 RSS 订阅",
            "request_body": {"fields": {"rss_url": "string (required)"}},
            "response": {"ok": True},
        },
        {
            "path": "/api/search-history/suggestions",
            "method": "GET",
            "summary": "获取搜索建议",
            "params": {"q": {"in": "query", "type": "string", "description": "搜索前缀"}},
            "response": {"suggestions": "array"},
        },
        {
            "path": "/api/search-history",
            "method": "DELETE",
            "summary": "清空搜索历史",
            "response": {"ok": True},
        },
        {
            "path": "/api/recent-podcasts",
            "method": "GET",
            "summary": "获取最近使用的播客源",
            "response": {"podcasts": "array"},
        },
    ]

    return jsonify({
        "name": "BiliMix Audio Processing API",
        "description": "BiliMix — 双语混合音频智能处理服务：支持转录、生词识别、TTS合成、中文翻译混合等功能",
        "version": "1.0",
        "base_url": request.host_url.rstrip("/"),
        "total_endpoints": len(endpoints),
        "endpoints": endpoints,
    })


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    os.makedirs(_web_dir, exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    # 抑制 Flask 轮询类 API 的请求日志（如 /api/task/<id> 每 1.5s 轮询一次）
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    # 初始化 SQLite 数据库（建表 + 迁移旧 JSON 数据）
    setup_database()
    _index = load_tasks_index()
    # 清理服务重启导致的孤儿任务（queued/processing/downloading状态在进程重启后无效）
    orphaned = 0
    for _tid, _t in _index.items():
        if _t.get("status") in ("queued", "processing", "downloading"):
            delete_task_from_index(_tid)
            orphaned += 1
    if orphaned:
        print(f"🧹 已清理 {orphaned} 个孤儿任务（服务重启导致）")
    print(f"📋 已加载 {len(_index)} 条历史任务记录")
    print("=" * 50)
    print("🎧 BiliMix Web App")
    print(f"📡 http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
