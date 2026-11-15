"""
TTS Blueprint - 独立语音合成（参考音频 + 文本 -> 音频）。

对外提供与翻译流水线解耦的 TTS 能力：
  POST   /api/tts/synthesize          提交合成任务（异步执行）
  POST   /api/tts/sync                一次请求内完成合成（可选逐词转录）
  GET    /api/tts/jobs                任务列表
  GET    /api/tts/<job_id>            任务状态
  GET    /api/tts/<job_id>/result     任务结果（含音频下载地址与逐词报告）
  POST   /api/tts/<job_id>/cancel     终止任务
  POST   /api/tts/<job_id>/retry      重新执行
  DELETE /api/tts/<job_id>            删除任务及产物

所有路由受 auth_bp 的全局登录校验保护（未登录返回 401）。
产物写入 data/results/tts/<job_id>/，因此可直接由 /api/audio/<路径> 下载。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
import wave

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Blueprint, jsonify, request

from core import config
from core.files import atomic_json
from core.task_queue import task_queue
from core.tts_jobs import (
    TERMINAL_STATUSES, clear_cancel, create_job, delete_job, get_job,
    is_cancelled, list_jobs, request_cancel, update_job,
)
from pipeline.tts_direct import (
    DEFAULT_LANG, MAX_SYNC_TEXT_CHARS, MAX_TEXTS, SUPPORTED_LANGS, build_job_id,
    get_job_dir, synthesize_texts, validate_lang, validate_texts,
)
from pipeline.dubbing_plan import file_sha256
from pipeline.media_io import _probe_audio_duration
from core.processes import _kill_task_subprocesses

tts_bp = Blueprint('tts', __name__, url_prefix='/api/tts')

# 同步接口：单次请求等待上限与参考音频限制
SYNC_TIMEOUT_DEFAULT_MS = 180_000
SYNC_TIMEOUT_MAX_MS = 600_000
SYNC_REF_MAX_BYTES = 20 * 1024 * 1024
# 参考片段过短克隆不稳，过长会拖慢推理；上游推荐 3–10 秒
SYNC_REF_MIN_SECONDS = 2.0
SYNC_REF_MAX_SECONDS = 15.0
SYNC_REF_EXTENSIONS = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac")
# 语气/节奏指令：只作为合成条件，不参与朗读与节奏度量
SYNC_MAX_STYLE_CHARS = 40
_STYLE_SEPARATORS = str.maketrans({"，": " ", ",": " ", "。": " ", "；": " ",
                                   ";": " ", "：": " ", ":": " ", "\n": " ",
                                   "\r": " "})

# ── 辅助 ────────────────────────────────────────────────────

def _is_allowed_ref_path(path: str) -> bool:
    """参考音频必须位于 data/ 目录内，避免通过接口探测/读取任意文件"""
    if not path:
        return False
    norm = os.path.normpath(os.path.abspath(path))
    data_root = os.path.normpath(config.DATA_DIR)
    return norm == data_root or norm.startswith(data_root + os.sep)


def _public_job(job: dict) -> dict:
    """裁剪为对外暴露的字段"""
    return {
        "job_id": job.get("job_id", ""),
        "status": job.get("status", ""),
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "title": job.get("title", ""),
        "lang": job.get("lang", DEFAULT_LANG),
        "text_count": len(job.get("texts") or []),
        "output_count": job.get("output_count", 0),
        "created_at": job.get("created_at", 0),
        "finished_at": job.get("finished_at", 0),
        "elapsed": job.get("elapsed", 0),
        "error": job.get("error", ""),
        "mode": job.get("mode", "async"),
        "include_words": bool(job.get("include_words")),
    }


def _tts_audio_url(path: str) -> str:
    """把产物绝对路径转成 /api/audio 下载地址。

    只有位于 RESULT_DIR 之内的文件才可被下载；越界（例如 TTS_JOB_DIR 被配置到
    别处）时返回空串，避免生成带 ../ 的无效地址。
    """
    if not path:
        return ""
    norm = os.path.normpath(os.path.abspath(path))
    root = os.path.normpath(config.RESULT_DIR)
    if norm != root and not norm.startswith(root + os.sep):
        return ""
    return "/api/audio/" + os.path.relpath(norm, root).replace(os.sep, "/")


def _output_items(job: dict) -> list:
    """把产物路径转换成带下载地址、时长、体积的列表"""
    items = []
    for out in job.get("outputs") or []:
        path = out.get("output_path", "")
        if not path or not os.path.isfile(path):
            continue
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        items.append({
            "index": out.get("index", 0),
            "text": out.get("text", ""),
            "audio_url": _tts_audio_url(path),
            "filename": os.path.basename(path),
            "duration": round(_probe_audio_duration(path), 2),
            "size_bytes": size_bytes,
        })
    return items


def _audio_descriptor(path: str) -> dict:
    """Size, hash, duration and sample rate of one committed artifact."""
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        size_bytes = 0
    sample_rate = 0
    try:
        with wave.open(path, "rb") as stream:
            sample_rate = stream.getframerate()
    except (OSError, EOFError, wave.Error):
        sample_rate = 0
    return {
        "path": path,
        "url": _tts_audio_url(path),
        "filename": os.path.basename(path),
        "duration": round(_probe_audio_duration(path), 3),
        "size_bytes": size_bytes,
        "sha256": file_sha256(path) if os.path.isfile(path) else "",
        "sample_rate": sample_rate,
    }


def _pin_sync_audio(job_id: str, outputs: list) -> list:
    """Give the synchronous result one stable filename.

    The synthesized cache file carries a content hash in its name; callers of
    the synchronous endpoint get `speech.wav` instead, so a saved response stays
    reusable without knowing the cache layout.
    """
    if len(outputs) != 1:
        return outputs
    source = outputs[0].get("output_path", "")
    if not source or not os.path.isfile(source):
        return outputs
    target = os.path.join(get_job_dir(job_id), "speech.wav")
    if os.path.abspath(source) != os.path.abspath(target):
        handle, temporary = tempfile.mkstemp(prefix=".speech-", suffix=".wav",
                                             dir=os.path.dirname(target))
        os.close(handle)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return [{**outputs[0], "output_path": target, "cache_path": source}]


def _analyze_output(job_id: str, output: dict, lang: str) -> dict:
    """Read one synthesized WAV back and align it to the text that produced it.

    Failure is reported per output; the audio stays published because the
    timings are an add-on, not part of the audio contract.
    """
    index = output.get("index", 0)
    text = output.get("text", "")
    report_path = os.path.join(get_job_dir(job_id), f"words_{index:04d}.json")
    try:
        from pipeline.tts_words import analyze_words

        language = lang if lang in ("zh", "en", "ja", "ko") else "zh"
        report = analyze_words(
            output["output_path"], text, language=language,
            work_dir=os.path.join(get_job_dir(job_id), "words", f"{index:04d}"),
        )
        atomic_json(report_path, report)
        return {
            "index": index,
            "text": text,
            "unit": report["unit"],
            "coverage": report["coverage"],
            "asr_text": report["asr_text"],
            "duration": report["duration"],
            "warnings": report["warnings"],
            "items": report["items"],
            "path": report_path,
            "url": _tts_audio_url(report_path),
            "error": "",
        }
    except InterruptedError:
        raise
    except Exception as exc:
        traceback.print_exc()
        return {"index": index, "text": text, "error": str(exc),
                "path": "", "url": "", "items": [], "warnings": []}


def _run_job(job_id: str):
    """后台线程：排队 -> 合成 -> 可选逐词回读 -> 写回终态"""
    job = get_job(job_id)
    if not job:
        return

    texts = job.get("texts") or []
    ref_audio = job.get("ref_audio", "")
    ref_text = job.get("ref_text", "")
    style = job.get("style", "")
    lang = job.get("lang", DEFAULT_LANG)
    include_words = bool(job.get("include_words"))
    total = len(texts)
    started = time.time()

    try:
        if is_cancelled(job_id):
            update_job(job_id, status="cancelled", message="已取消（排队中）",
                       elapsed=round(time.time() - started, 1))
            return

        update_job(job_id, status="processing", progress=1,
                   message=f"正在加载模型并合成 {total} 条文本...")

        def _progress(current, current_total):
            pct = int(min(current, current_total) / max(current_total, 1) * 99)
            update_job(job_id, progress=max(1, pct),
                       message=f"合成中 ({current}/{current_total})")

        outputs = synthesize_texts(
            texts, ref_audio, job_id, lang=lang,
            cancel_check=lambda: is_cancelled(job_id),
            progress_cb=_progress,
            ref_text=ref_text,
            style=style,
        )
        if job.get("mode") == "sync":
            outputs = _pin_sync_audio(job_id, outputs)

        words, words_error = [], ""
        if include_words:
            update_job(job_id, progress=99, message="合成完成，正在生成逐词转录...")
            for output in outputs:
                if is_cancelled(job_id):
                    raise InterruptedError("任务已被用户终止")
                words.append(_analyze_output(job_id, output, lang))
            failures = [item["error"] for item in words if item.get("error")]
            if failures:
                words_error = failures[0]

        completion = dict(
            status="completed",
            progress=100,
            message=f"合成完成，共 {len(outputs)} 条",
            outputs=outputs,
            output_count=len(outputs),
            elapsed=round(time.time() - started, 1),
            error="",
            words=words,
            words_error=words_error,
        )
        update_job(job_id, **completion)
        print(f"[TTS] 任务完成 {job_id}: {len(outputs)} 条"
              f"{'（含逐词转录）' if include_words else ''}")

    except InterruptedError:
        update_job(job_id, status="cancelled", message="任务已被终止",
                   elapsed=round(time.time() - started, 1))
        print(f"[TTS] 任务已终止 {job_id}")
    except Exception as e:
        traceback.print_exc()
        update_job(job_id, status="error", message=f"合成失败: {e}",
                   error=str(e), elapsed=round(time.time() - started, 1))
        print(f"[TTS] 任务失败 {job_id}: {e}")
    finally:
        clear_cancel(job_id)


def _start_job(job_id: str):
    """启动后台合成线程（daemon，随服务退出）"""
    task_queue.submit("tts:"+job_id, lambda: _run_job(job_id))


# ── 路由 ────────────────────────────────────────────────────

@tts_bp.route("/synthesize", methods=["POST"])
def synthesize():
    """提交 TTS 合成任务。

    请求体:
      ref_audio (必填): 服务端参考音频路径（先用 /api/upload 上传得到 local_path）
      text  或 texts (必填): 待合成文本，text 为单条，texts 为多条
      lang (可选): 合成语种，默认 zh
      title (可选): 任务标题
    """
    data = request.get_json(silent=True) or {}

    ref_audio = (data.get("ref_audio") or "").strip()
    if not ref_audio:
        return jsonify({"error": "缺少 ref_audio（参考音频路径）"}), 400
    if not _is_allowed_ref_path(ref_audio):
        return jsonify({"error": "参考音频必须位于服务端 data/ 目录内，"
                                 "请先通过 /api/upload 上传"}), 400
    if not os.path.isfile(ref_audio):
        return jsonify({"error": f"参考音频不存在: {ref_audio}"}), 400

    raw_texts = data.get("texts")
    if raw_texts is None:
        single = data.get("text")
        raw_texts = [single] if single is not None else []
    try:
        texts = validate_texts(raw_texts)
        lang = validate_lang(data.get("lang", DEFAULT_LANG))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    title = (data.get("title") or "").strip()
    job_id = build_job_id(ref_audio, texts, lang)

    existing = get_job(job_id)
    if existing:
        # 相同输入已成功合成且产物齐全 -> 直接复用（幂等）
        if (existing.get("status") == "completed"
                and all(os.path.isfile(o.get("output_path", ""))
                        for o in existing.get("outputs") or [])):
            return jsonify({"job_id": job_id, "status": "completed",
                            "reused": True,
                            "message": "相同输入的合成结果已存在"}), 200
        # 其他状态（error/cancelled/中断）-> 重置并重新执行
        if existing.get("status") in ("processing", "queued") or task_queue.position("tts:"+job_id) >= 0:
            return jsonify({"error": "该任务正在合成中，请稍后查询", 
                            "job_id": job_id}), 409

    job = create_job(job_id, texts=texts, ref_audio=ref_audio, lang=lang,
                     title=title, message="已排队")
    os.makedirs(get_job_dir(job_id), exist_ok=True)
    _start_job(job_id)

    print(f"[TTS] 任务已提交 {job_id}: {len(texts)} 条文本, lang={lang}, "
          f"ref={os.path.basename(ref_audio)}")
    return jsonify({"job_id": job_id, "status": job["status"],
                    "text_count": len(texts), "lang": lang}), 202


# ── 同步合成 ────────────────────────────────────────────────

class _SyncError(Exception):
    """同步请求的可公开错误：稳定 code 与 HTTP 状态。"""

    def __init__(self, message, code, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def _form_flag(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _store_ref_upload(stream) -> tuple:
    """把上传的参考音频按内容哈希落到 data/downloads/tts_refs/。

    内容寻址保证同一段参考只存一份，重复调用不会堆出无数临时文件。
    """
    extension = os.path.splitext(os.path.basename(stream.filename or ""))[1].lower()
    if extension not in SYNC_REF_EXTENSIONS:
        raise _SyncError(
            f"参考音频格式不支持: {extension or '(无扩展名)'}，"
            f"可选 {', '.join(SYNC_REF_EXTENSIONS)}", "unsupported_ref_format")
    directory = os.path.join(config.DOWNLOAD_DIR, "tts_refs")
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".ref-", suffix=extension, dir=directory)
    size = 0
    try:
        with os.fdopen(handle, "wb") as target:
            while True:
                chunk = stream.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > SYNC_REF_MAX_BYTES:
                    raise _SyncError(
                        f"参考音频超过 {SYNC_REF_MAX_BYTES // (1024 * 1024)}MB 上限",
                        "ref_too_large", 413)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if not size:
            raise _SyncError("参考音频为空", "ref_empty")
        digest = file_sha256(temporary)
        final = os.path.join(directory, digest[:24] + extension)
        os.replace(temporary, final)
        temporary = ""
        return final, digest
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _normalize_style(value) -> str:
    """风格指令只保留描述性文字。

    标点会被长句规划当作分句点，换行会破坏任务体，因此统一替换为空格；
    指令本身不参与朗读，也不参与语速/停顿度量。
    """
    style = str(value or "").strip().translate(_STYLE_SEPARATORS)
    style = " ".join(style.split())
    if len(style) > SYNC_MAX_STYLE_CHARS:
        raise _SyncError(f"style 最多 {SYNC_MAX_STYLE_CHARS} 字符", "invalid_style")
    return style


def _check_ref_duration(path: str) -> float:
    duration = _probe_audio_duration(path)
    if duration <= 0:
        raise _SyncError("无法读取参考音频时长（文件可能损坏）", "ref_unreadable")
    if duration < SYNC_REF_MIN_SECONDS:
        raise _SyncError(
            f"参考音频过短（{duration:.1f}s），至少 {SYNC_REF_MIN_SECONDS:.0f}s；推荐 3–10 秒",
            "ref_too_short")
    if duration > SYNC_REF_MAX_SECONDS:
        raise _SyncError(
            f"参考音频过长（{duration:.1f}s），最多 {SYNC_REF_MAX_SECONDS:.0f}s；推荐 3–10 秒",
            "ref_too_long")
    return duration


def _parse_sync_request() -> dict:
    """解析 multipart（内联文件）或 JSON（服务端已有路径）两种形态。"""
    uploaded = request.mimetype == "multipart/form-data"
    if uploaded:
        data = request.form
        raw_text = (data.get("text") or "").strip()
        raw_ref_text = (data.get("ref_text") or "").strip()
        include_words = _form_flag(data.get("include_words"))
        language = data.get("language") or data.get("lang")
        title = (data.get("title") or "").strip()
        style = _normalize_style(data.get("style"))
        timeout_raw = data.get("timeout_ms")
        stream = request.files.get("ref_audio")
        if stream is None:
            raise _SyncError("缺少 ref_audio 文件", "missing_ref_audio")
        ref_audio, ref_sha = _store_ref_upload(stream)
    else:
        data = request.get_json(silent=True) or {}
        raw_text = str(data.get("text") or "").strip()
        raw_ref_text = str(data.get("ref_text") or "").strip()
        include_words = bool(data.get("include_words"))
        language = data.get("language") or data.get("lang")
        title = str(data.get("title") or "").strip()
        style = _normalize_style(data.get("style"))
        timeout_raw = data.get("timeout_ms")
        ref_audio = str(data.get("ref_audio_path") or data.get("ref_audio") or "").strip()
        if not ref_audio:
            raise _SyncError("缺少 ref_audio_path（服务端参考音频路径）",
                             "missing_ref_audio")
        if not _is_allowed_ref_path(ref_audio) or not os.path.isfile(ref_audio):
            raise _SyncError("参考音频必须是服务端 data/ 目录内的现有文件",
                             "ref_path_forbidden")
        ref_sha = file_sha256(ref_audio)

    if not raw_text:
        raise _SyncError("缺少 text（待合成文本）", "invalid_text")
    if len(raw_text) > MAX_SYNC_TEXT_CHARS:
        raise _SyncError(f"text 最多 {MAX_SYNC_TEXT_CHARS} 字符，当前 {len(raw_text)}",
                         "invalid_text")
    if len(raw_ref_text) > MAX_SYNC_TEXT_CHARS:
        raise _SyncError(f"ref_text 最多 {MAX_SYNC_TEXT_CHARS} 字符", "invalid_text")

    try:
        lang = validate_lang(language or DEFAULT_LANG)
    except ValueError as error:
        raise _SyncError(str(error), "unsupported_language")

    engine = getattr(config, "TTS_ENGINE", "confucius-tts")
    if lang == "auto":
        raise _SyncError("同步接口不接受 lang=auto，请指定具体语种", "unsupported_language")
    if engine == "voxcpm" and lang != "zh":
        raise _SyncError("VoxCPM2 后端只支持 zh", "unsupported_language")
    if raw_ref_text and engine != "voxcpm":
        # 不静默忽略：参考转写只影响续写克隆，当前引擎没有该能力
        raise _SyncError("当前合成引擎不支持 ref_text 续写克隆，请去掉该字段",
                         "unsupported_ref_text")
    if style and engine != "voxcpm":
        raise _SyncError("当前合成引擎不支持 style 语气指令，请去掉该字段",
                         "unsupported_style")

    try:
        timeout_ms = int(timeout_raw) if timeout_raw not in (None, "") else SYNC_TIMEOUT_DEFAULT_MS
    except (TypeError, ValueError):
        raise _SyncError("timeout_ms 必须是整数毫秒", "invalid_timeout")
    timeout_ms = max(1000, min(timeout_ms, SYNC_TIMEOUT_MAX_MS))

    duration = _check_ref_duration(ref_audio)
    return {"text": raw_text, "ref_audio": ref_audio, "ref_sha256": ref_sha,
            "ref_duration": duration, "ref_text": raw_ref_text, "lang": lang,
            "include_words": include_words, "title": title, "style": style,
            "timeout_ms": timeout_ms}


def _sync_artifacts_ready(job: dict, include_words: bool) -> bool:
    outputs = job.get("outputs") or []
    if not outputs or not all(os.path.isfile(item.get("output_path", ""))
                              for item in outputs):
        return False
    if not include_words:
        return True
    words = job.get("words") or []
    if len(words) != len(outputs):
        return False
    for item in words:
        # 上一次逐词分析失败时允许显式重试，不把失败结果当成可复用缓存
        if item.get("error"):
            return False
        if not item.get("path") or not os.path.isfile(item["path"]):
            return False
    return True


def _sync_response(job: dict, parsed: dict, cache_hit: bool,
                   queue_wait_ms: int, started: float, request_id: str) -> dict:
    outputs = job.get("outputs") or []
    descriptor = _audio_descriptor(outputs[0]["output_path"]) if outputs else {}
    words = None
    if parsed["include_words"]:
        entries = job.get("words") or []
        if entries:
            entry = entries[0]
            words = {key: entry.get(key) for key in
                     ("index", "text", "unit", "coverage", "asr_text", "duration",
                      "warnings", "items", "path", "url", "error")}
    return {
        "request_id": request_id,
        "status": "completed",
        "job_id": job.get("job_id", ""),
        "mode": "sync",
        "text": parsed["text"],
        "language": parsed["lang"],
        "style": parsed["style"] or None,
        "audio": {
            "url": descriptor.get("url", ""),
            "filename": descriptor.get("filename", ""),
            "duration": descriptor.get("duration", 0),
            "size_bytes": descriptor.get("size_bytes", 0),
            "sha256": descriptor.get("sha256", ""),
            "sample_rate": descriptor.get("sample_rate", 0),
        },
        "words": words,
        "words_url": words.get("url", "") if words else None,
        "words_error": job.get("words_error") or None,
        "reference": {
            "sha256": parsed["ref_sha256"],
            "duration": round(parsed["ref_duration"], 3),
            "clone_mode": "prompt+reference" if parsed["ref_text"] else "reference",
        },
        "cache": {"hit": cache_hit},
        "queue_wait_ms": queue_wait_ms,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


@tts_bp.route("/sync", methods=["POST"])
def synthesize_sync():
    """同步合成：一次请求内返回音频，可选返回逐词转录。

    请求（multipart/form-data 或 application/json）:
      text (必填): 待合成文本，最多 500 字符
      ref_audio (multipart): 参考音频文件，3–10 秒最佳
      ref_audio_path (JSON): 服务端 data/ 内的参考音频路径
      ref_text (可选): 参考音频的逐字转写；提供时启用续写克隆
      include_words (可选): 是否返回逐词转录，默认 false
      language (可选): 合成语种，默认 zh；VoxCPM2 仅支持 zh
      timeout_ms (可选): 服务端等待上限，默认 180000，最大 600000

    等待超时返回 202 与 job_id，任务继续执行，可按 job_id 取回结果。
    """
    started = time.time()
    request_id = uuid.uuid4().hex[:12]
    try:
        parsed = _parse_sync_request()
    except _SyncError as error:
        print(f"[TTS][sync] 请求被拒绝 ({error.code}): {error}")
        return jsonify({"error": str(error), "code": error.code,
                        "request_id": request_id}), error.status

    job_id = build_job_id(parsed["ref_audio"], [parsed["text"]], parsed["lang"],
                          ref_text=parsed["ref_text"],
                          include_words=parsed["include_words"],
                          style=parsed["style"])
    queue_started = time.time()
    cache_hit = False
    job = get_job(job_id)

    if job and job.get("status") == "completed" and _sync_artifacts_ready(
            job, parsed["include_words"]):
        cache_hit = True
        print(f"[TTS][sync] 命中已有结果 {job_id}")
    else:
        running = bool(job) and (
            job.get("status") in ("processing", "queued")
            or task_queue.position("tts:" + job_id) >= 0)
        if not running:
            job = create_job(job_id, texts=[parsed["text"]],
                             ref_audio=parsed["ref_audio"], lang=parsed["lang"],
                             title=parsed["title"], message="已排队", mode="sync",
                             ref_text=parsed["ref_text"],
                             include_words=parsed["include_words"],
                             style=parsed["style"])
            os.makedirs(get_job_dir(job_id), exist_ok=True)
            _start_job(job_id)
            print(f"[TTS][sync] 已提交 {job_id}: ref={os.path.basename(parsed['ref_audio'])}, "
                  f"words={parsed['include_words']}, style={parsed['style'] or '-'}")

    deadline = time.time() + parsed["timeout_ms"] / 1000
    queue_wait_ms = 0
    while time.time() < deadline:
        current = get_job(job_id)
        if not current:
            return jsonify({"error": "任务状态丢失", "code": "job_missing",
                            "request_id": request_id}), 500
        status = current.get("status", "")
        if queue_wait_ms == 0 and status != "queued":
            queue_wait_ms = int((time.time() - queue_started) * 1000)
        if status in TERMINAL_STATUSES:
            break
        time.sleep(0.05)

    job = get_job(job_id) or {}
    status = job.get("status", "")
    if status not in TERMINAL_STATUSES:
        return jsonify({
            "request_id": request_id, "status": status or "queued",
            "job_id": job_id, "mode": "sync",
            "poll_url": f"/api/tts/{job_id}",
            "result_url": f"/api/tts/{job_id}/result",
            "message": "服务端等待超时，任务继续执行，请按 job_id 轮询",
        }), 202
    if status == "cancelled":
        return jsonify({"error": "任务已被终止", "code": "cancelled",
                        "job_id": job_id, "request_id": request_id}), 409
    if status != "completed":
        return jsonify({"error": job.get("error") or "合成失败",
                        "code": "synthesis_failed", "job_id": job_id,
                        "request_id": request_id}), 500

    return jsonify(_sync_response(job, parsed, cache_hit, queue_wait_ms,
                                  started, request_id)), 200


@tts_bp.route("/jobs")
def get_jobs():
    """任务列表（支持 ?limit=N 与 ?status=xxx 过滤）"""
    limit = request.args.get("limit", type=int, default=0)
    status = (request.args.get("status") or "").strip()
    jobs = [_public_job(j) for j in list_jobs(limit=0)]
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    if limit and limit > 0:
        jobs = jobs[:limit]
    return jsonify({"jobs": jobs})


@tts_bp.route("/<job_id>")
def get_job_status(job_id):
    """任务状态"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(_public_job(job))


@tts_bp.route("/<job_id>/result")
def get_job_result(job_id):
    """任务结果：含每条文本的音频下载地址"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404

    outputs = _output_items(job)
    words = job.get("words") or []
    by_index = {item.get("index"): item for item in words}
    for item in outputs:
        item["words"] = by_index.get(item["index"])
    return jsonify({
        "job_id": job_id,
        "status": job.get("status", ""),
        "mode": job.get("mode", "async"),
        "lang": job.get("lang", DEFAULT_LANG),
        "texts": job.get("texts") or [],
        "ref_audio": job.get("ref_audio", ""),
        "ref_text": job.get("ref_text", ""),
        "include_words": bool(job.get("include_words")),
        "output_count": len(outputs),
        "outputs": outputs,
        # 便捷字段：第一条产物的下载地址
        "audio_url": outputs[0]["audio_url"] if outputs else "",
        "audio_path": outputs[0]["filename"] if outputs else "",
        "words": words,
        "words_error": job.get("words_error", ""),
        "error": job.get("error", ""),
    })


@tts_bp.route("/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    """终止任务（排队中可直接取消，合成中会杀掉 worker 子进程）"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    if job.get("status") in TERMINAL_STATUSES:
        return jsonify({"message": f"任务已处于 {job['status']} 状态",
                        "status": job["status"]}), 200

    request_cancel(job_id)
    if task_queue.discard("tts:"+job_id):
        update_job(job_id, status="cancelled", message="已取消（排队中）")
        clear_cancel(job_id)
    # 杀掉合成中的 worker 子进程（worker 以 job_id 注册在 task_subprocesses）
    _kill_task_subprocesses(job_id)
    return jsonify({"message": "已请求终止", "job_id": job_id}), 200


@tts_bp.route("/<job_id>/retry", methods=["POST"])
def retry_job(job_id):
    """重新执行任务（复用已有产物缓存，只补齐缺失部分）"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    if job.get("status") in ("processing", "queued") or task_queue.position("tts:"+job_id) >= 0:
        return jsonify({"error": "任务已在执行队列中，无需重试"}), 409

    clear_cancel(job_id)
    update_job(job_id, status="queued", progress=0, error="",
               message="已重新排队", outputs=[], output_count=0)
    _start_job(job_id)
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@tts_bp.route("/<job_id>", methods=["DELETE"])
def remove_job(job_id):
    """删除任务及其产物"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404

    request_cancel(job_id)
    task_queue.discard("tts:"+job_id)
    _kill_task_subprocesses(job_id)

    if not task_queue.wait("tts:"+job_id, timeout=10):
        return jsonify({"error": "任务仍在停止中，请稍后重试删除"}), 409
    cleaned = delete_job(job_id)
    return jsonify({"message": "任务已删除", "cleaned_files": cleaned})


@tts_bp.route("/languages")
def list_languages():
    """支持的合成语种"""
    return jsonify({"languages": list(SUPPORTED_LANGS),
                    "default": DEFAULT_LANG,
                    "max_texts": MAX_TEXTS})
