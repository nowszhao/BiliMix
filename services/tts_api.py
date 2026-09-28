"""
TTS Blueprint - 独立语音合成（参考音频 + 文本 -> 音频）。

对外提供与翻译流水线解耦的 TTS 能力：
  POST   /api/tts/synthesize          提交合成任务（异步执行）
  GET    /api/tts/jobs                任务列表
  GET    /api/tts/<job_id>            任务状态
  GET    /api/tts/<job_id>/result     任务结果（含音频下载地址）
  POST   /api/tts/<job_id>/cancel     终止任务
  POST   /api/tts/<job_id>/retry      重新执行
  DELETE /api/tts/<job_id>            删除任务及产物

所有路由受 auth_bp 的全局登录校验保护（未登录返回 401）。
产物写入 data/results/tts/<job_id>/，因此可直接由 /api/audio/<路径> 下载。
"""
import os
import sys
import threading
import time
import traceback

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Blueprint, jsonify, request

from core import config
from core.tts_jobs import (
    TERMINAL_STATUSES, clear_cancel, create_job, delete_job, get_job,
    is_cancelled, list_jobs, request_cancel, update_job,
)
from pipeline.tts_direct import (
    DEFAULT_LANG, MAX_TEXTS, SUPPORTED_LANGS, build_job_id, get_job_dir,
    synthesize_texts, validate_lang, validate_texts,
)
from services.shared import _kill_task_subprocesses, _probe_audio_duration

tts_bp = Blueprint('tts', __name__, url_prefix='/api/tts')

# 全局串行锁：单个 TTS 任务已会按 CONFUCIUS4_TTS_NUM_WORKERS 并行拉起多个
# worker 子进程，多个任务同时跑会成倍占用 CPU 与内存（每个 worker 约 2-4GB），
# 因此这里串行化，等待期间任务保持 queued 状态。
_run_lock = threading.Lock()


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


def _run_job(job_id: str):
    """后台线程：排队 -> 合成 -> 写回终态"""
    job = get_job(job_id)
    if not job:
        return

    texts = job.get("texts") or []
    ref_audio = job.get("ref_audio", "")
    lang = job.get("lang", DEFAULT_LANG)
    total = len(texts)
    started = time.time()

    # 等待全局串行锁（可被取消中断）
    while not _run_lock.acquire(timeout=1.0):
        if is_cancelled(job_id):
            update_job(job_id, status="cancelled", message="已取消（排队中）",
                       elapsed=round(time.time() - started, 1))
            return

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
        )

        update_job(
            job_id,
            status="completed",
            progress=100,
            message=f"合成完成，共 {len(outputs)} 条",
            outputs=outputs,
            output_count=len(outputs),
            elapsed=round(time.time() - started, 1),
            error="",
        )
        print(f"[TTS] 任务完成 {job_id}: {len(outputs)} 条")

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
        _run_lock.release()


def _start_job(job_id: str):
    """启动后台合成线程（daemon，随服务退出）"""
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()


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
        if existing.get("status") == "processing":
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
    return jsonify({
        "job_id": job_id,
        "status": job.get("status", ""),
        "lang": job.get("lang", DEFAULT_LANG),
        "texts": job.get("texts") or [],
        "ref_audio": job.get("ref_audio", ""),
        "output_count": len(outputs),
        "outputs": outputs,
        # 便捷字段：第一条产物的下载地址
        "audio_url": outputs[0]["audio_url"] if outputs else "",
        "audio_path": outputs[0]["filename"] if outputs else "",
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
    # 杀掉合成中的 worker 子进程（worker 以 job_id 注册在 task_subprocesses）
    _kill_task_subprocesses(job_id)
    return jsonify({"message": "已请求终止", "job_id": job_id}), 200


@tts_bp.route("/<job_id>/retry", methods=["POST"])
def retry_job(job_id):
    """重新执行任务（复用已有产物缓存，只补齐缺失部分）"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    if job.get("status") == "processing":
        return jsonify({"error": "任务正在合成中，无需重试"}), 409

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

    if job.get("status") == "processing":
        _kill_task_subprocesses(job_id)

    cleaned = delete_job(job_id)
    return jsonify({"message": "任务已删除", "cleaned_files": cleaned})


@tts_bp.route("/languages")
def list_languages():
    """支持的合成语种"""
    return jsonify({"languages": list(SUPPORTED_LANGS),
                    "default": DEFAULT_LANG,
                    "max_texts": MAX_TEXTS})
