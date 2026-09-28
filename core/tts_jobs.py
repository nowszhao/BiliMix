"""
独立 TTS 合成任务（bmx tts）的状态管理。

与翻译流水线的 core.task_manager.tasks 完全隔离：TTS 任务只做
「参考音频 + 文本 -> 音频」，没有下载/转录/翻译/混音等步骤，因此不共用
任务表，避免污染任务列表和断点恢复逻辑。

持久化策略（沿用项目既有风格）：
  - 易变状态（progress / message）只放内存；
  - 每次状态变更写入 data/results/tts/<job_id>/job.json，供重启后列表与查询；
  - 产物 WAV 与 job.json 同目录，可直接经 /api/audio/<相对路径> 下载。
"""
import json
import os
import shutil
import threading
import time

from core import config

# 内存中的任务状态 {job_id: {...}}
tts_jobs = {}
tts_jobs_lock = threading.Lock()

# 终止信号 {job_id: threading.Event}
tts_cancel_flags = {}

# 终态：达到后不再变更
TERMINAL_STATUSES = ("completed", "error", "cancelled")

# 需要落盘的字段（回调、线程对象等不入盘）
_PERSIST_FIELDS = (
    "job_id", "status", "progress", "message", "created_at", "updated_at",
    "finished_at", "title", "lang", "texts", "ref_audio", "outputs",
    "output_count", "error", "elapsed",
)


def get_job_dir(job_id: str) -> str:
    """返回 job 的产物目录（位于 RESULT_DIR 之下）"""
    return os.path.join(config.TTS_JOB_DIR, job_id)


def _job_file(job_id: str) -> str:
    return os.path.join(get_job_dir(job_id), "job.json")


def _write_job_to_disk(job: dict):
    """将任务摘要写入 job.json（失败不阻塞主流程）"""
    job_id = job.get("job_id", "")
    if not job_id:
        return
    try:
        job_dir = get_job_dir(job_id)
        os.makedirs(job_dir, exist_ok=True)
        data = {k: job.get(k) for k in _PERSIST_FIELDS if k in job}
        with open(_job_file(job_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[TTS] 写入 job.json 失败 ({job_id}): {e}")


def create_job(job_id: str, **fields) -> dict:
    """创建（或重置）一个 TTS 任务"""
    now = round(time.time(), 1)
    job = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "message": "已排队",
        "created_at": now,
        "updated_at": now,
        "finished_at": 0,
        "title": "",
        "lang": "zh",
        "texts": [],
        "ref_audio": "",
        "outputs": [],
        "output_count": 0,
        "error": "",
        "elapsed": 0,
    }
    job.update({k: v for k, v in fields.items() if k in _PERSIST_FIELDS})
    with tts_jobs_lock:
        tts_jobs[job_id] = job
    tts_cancel_flags.pop(job_id, None)
    _write_job_to_disk(job)
    return dict(job)


def update_job(job_id: str, **kwargs) -> dict:
    """线程安全地更新任务状态；终态自动补 finished_at 并落盘"""
    with tts_jobs_lock:
        job = tts_jobs.get(job_id)
        if not job:
            return {}
        job.update(kwargs)
        job["updated_at"] = round(time.time(), 1)
        if kwargs.get("status") in TERMINAL_STATUSES:
            job["finished_at"] = job["updated_at"]
        snapshot = dict(job)

    # 仅在状态变化时落盘；progress 高频刷新只留内存，避免频繁 IO
    status = kwargs.get("status")
    if status in TERMINAL_STATUSES or status in ("processing", "queued"):
        _write_job_to_disk(snapshot)
    return snapshot


def get_job(job_id: str) -> dict:
    """读取任务状态：内存优先，其次磁盘（含重启中断修正）"""
    with tts_jobs_lock:
        job = tts_jobs.get(job_id)
        if job:
            return dict(job)

    path = _job_file(job_id)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except Exception as e:
        print(f"[TTS] 读取 job.json 失败 ({job_id}): {e}")
        return {}

    # 重启前处于 queued/processing 的任务其子进程已不存在，标记为中断
    if saved.get("status") in ("queued", "processing"):
        saved["status"] = "error"
        saved["message"] = "任务因服务重启而中断，请重新提交"
        saved["error"] = "服务重启"
        _write_job_to_disk(saved)

    with tts_jobs_lock:
        tts_jobs[job_id] = saved
    return dict(saved)


def list_jobs(limit: int = 0) -> list:
    """列出全部任务（内存 + 磁盘），按创建时间倒序"""
    merged = {}
    if os.path.isdir(config.TTS_JOB_DIR):
        for name in os.listdir(config.TTS_JOB_DIR):
            if not os.path.isfile(os.path.join(config.TTS_JOB_DIR, name, "job.json")):
                continue
            job = get_job(name)
            if job:
                merged[name] = job

    with tts_jobs_lock:
        for job_id, job in tts_jobs.items():
            merged[job_id] = dict(job)

    jobs = sorted(merged.values(),
                  key=lambda j: j.get("created_at", 0), reverse=True)
    if limit and limit > 0:
        jobs = jobs[:limit]
    return jobs


def delete_job(job_id: str) -> list:
    """删除任务及其产物文件，返回已清理的相对路径列表"""
    event = tts_cancel_flags.get(job_id)
    if event:
        event.set()

    cleaned = []
    job_dir = get_job_dir(job_id)
    if os.path.isdir(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)
        if not os.path.isdir(job_dir):
            cleaned.append(f"data/results/tts/{job_id}/")

    with tts_jobs_lock:
        tts_jobs.pop(job_id, None)
    tts_cancel_flags.pop(job_id, None)
    return cleaned


def is_cancelled(job_id: str) -> bool:
    """检查任务是否被请求终止"""
    event = tts_cancel_flags.get(job_id)
    return event.is_set() if event else False


def clear_cancel(job_id: str):
    """清除终止标记（重新提交/重试前调用）"""
    tts_cancel_flags.pop(job_id, None)


def request_cancel(job_id: str) -> bool:
    """请求终止任务，返回任务是否存在"""
    job = get_job(job_id)
    if not job:
        return False
    if job.get("status") in TERMINAL_STATUSES:
        return True
    event = tts_cancel_flags.get(job_id)
    if event is None:
        event = threading.Event()
        tts_cancel_flags[job_id] = event
    event.set()
    return True
