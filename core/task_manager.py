"""
任务管理模块
负责任务状态维护、持久化、磁盘恢复等功能。
从 web_app.py 中提取，减少主文件体积。

已从 JSON 文件迁移到 SQLite 数据库存储。
"""
import json
import os
import threading

from core import config
from core.database import (
    load_tasks_index as db_load_tasks_index,
    save_task_to_index,
    delete_task_from_index,
    get_task_from_index,
)

# ============================================================
# 内存中的任务状态
# ============================================================

tasks = {}                   # {task_id: {status, progress, message, ...}}
tasks_lock = threading.Lock()

cancel_flags = {}            # {task_id: threading.Event}  终止信号
task_subprocesses = {}       # {task_id: subprocess.Popen} 后台子进程引用


# ============================================================
# 持久化：通过 SQLite 存储任务索引
# ============================================================

def load_tasks_index() -> dict:
    """从 SQLite 加载历史任务索引"""
    try:
        return db_load_tasks_index()
    except Exception as e:
        import traceback
        print(f"[ERROR] 从 SQLite 加载任务索引失败: {e}")
        traceback.print_exc()
        return {}


def save_tasks_index(index: dict):
    """将任务索引写入 SQLite（兼容旧接口：全量写入）"""
    try:
        for task_id, summary in index.items():
            summary["task_id"] = task_id
            save_task_to_index(task_id, summary)
    except Exception as e:
        print(f"[WARN] 保存任务索引失败: {e}")


def save_task_result_to_disk(result_dir: str, data: dict):
    """将完整任务结果持久化到磁盘（供重启后恢复）"""
    try:
        # 自动注入 skip_confirmation（如果 data 中没有，从内存 tasks 中获取）
        if "skip_confirmation" not in data:
            task_id = data.get("task_id", "")
            if task_id and task_id in tasks:
                data["skip_confirmation"] = tasks[task_id].get(
                    "skip_confirmation",
                    getattr(config, "SKIP_CONFIRMATION", True))
            else:
                data["skip_confirmation"] = getattr(config, "SKIP_CONFIRMATION", True)
        # 确保 key 字段存在
        pass

        path = os.path.join(result_dir, "task_result.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[Task] 任务结果已持久化: {path}")
    except Exception as e:
        print(f"[WARN] 保存任务结果失败: {e}")


# ============================================================
# 任务状态操作
# ============================================================

def update_task(task_id: str, **kwargs):
    """线程安全地更新任务状态"""
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id].update(kwargs)
    # 持久化关键状态变更（processing 仅在轮询中频繁调用，不写盘）
    if "status" in kwargs and kwargs["status"] in (
        "completed", "error", "cancelled",
        "awaiting_confirmation", "awaiting_sentence_confirmation",
        "queued",
    ):
        _persist_task(task_id)
    # basename / audio_path / original_duration 首次设置时也更新 SQLite
    elif "_basename" in kwargs and kwargs.get("_basename"):
        _persist_task(task_id)
    elif "_audio_path" in kwargs and kwargs.get("_audio_path"):
        _persist_task(task_id)
    elif "original_duration" in kwargs and kwargs.get("original_duration"):
        _persist_task(task_id)


def _persist_task(task_id: str):
    """将任务摘要信息持久化到 SQLite"""
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return
        # 优先使用顶层 original_duration（下载完成后立即设置），否则尝试从 result 取
        orig_dur = task.get("original_duration", 0)
        if not orig_dur and task.get("result"):
            orig_dur = task.get("result", {}).get("original_duration", 0)
        summary = {
            "task_id": task.get("task_id"),
            "url": task.get("url", ""),
            "title": task.get("title", ""),
            "difficulty": task.get("difficulty", ""),
            "process_mode": task.get("process_mode", "sentence_translate"),
            "type": task.get("type", "audio"),
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
            "original_duration": orig_dur,
            "mixed_duration": (task.get("result", {}).get("mixed_duration", 0)
                               if task.get("result") else 0),
        }
    save_task_to_index(task_id, summary)


def get_task(task_id: str) -> dict:
    """线程安全地获取任务状态（返回副本）"""
    with tasks_lock:
        return dict(tasks.get(task_id, {}))


def is_cancelled(task_id: str) -> bool:
    """检查任务是否被请求终止"""
    event = cancel_flags.get(task_id)
    return event.is_set() if event else False


# ============================================================
# 任务恢复
# ============================================================

def restore_task_from_disk(task_id: str) -> dict:
    """从磁盘恢复完整任务数据到内存。

    查找流程：
    1. 从 SQLite 任务索引获取 basename
    2. 从 data/results/<basename>/task_result.json 读取完整数据
    3. 如果 task_result.json 不存在，尝试从散落的文件拼装
    4. 恢复后注入内存 tasks 字典
    """
    summary = get_task_from_index(task_id)
    if not summary:
        return {}

    basename = summary.get("basename", "")
    if not basename:
        # 任务可能在中途被中断（如转录中 kill 进程），没有 basename
        # 统一标记为 error 状态，SQLite 里的旧 status 不可信
        task = {
            "task_id": task_id,
            "url": summary.get("url", ""),
            "title": summary.get("title", ""),
                        "skip_confirmation": summary.get("skip_confirmation",
                               getattr(config, "SKIP_CONFIRMATION", True)),
            "status": "error",
            "step": "download",
            "progress": 0,
            "message": "任务因服务重启而中断，请重新提交",
            "created_at": summary.get("created_at", ""),
            "transcription_text": "",
            "segments": [],
            "difficult_words": [],
            "replacements": [],
            "translations": {},
            "translated_indices": [],
            "result": None,
            "time_mapping": [],
            "_basename": "",
            "_audio_path": "",
        }
        with tasks_lock:
            tasks[task_id] = task
        print(f"[恢复] 任务 {task_id[:8]}... 无 basename，恢复为已中断状态")
        return task

    result_dir = os.path.join(config.RESULT_DIR, basename)

    # 尝试从 task_result.json 恢复（优先）
    task_result_path = os.path.join(result_dir, "task_result.json")
    if os.path.exists(task_result_path):
        try:
            with open(task_result_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            saved_status = saved.get("status", summary.get("status", "completed"))
            is_confirm = saved_status == "awaiting_confirmation"
            is_sentence_confirm = saved_status == "awaiting_sentence_confirmation"
            task = {
                "task_id": task_id,
                "url": summary.get("url", ""),
                "title": summary.get("title", ""),
                "difficulty": saved.get("difficulty", summary.get("difficulty", "")),
                "process_mode": saved.get("process_mode", summary.get("process_mode", "sentence_translate")),
                "type": saved.get("type", summary.get("type", "audio")),
                "skip_confirmation": saved.get("skip_confirmation",
                                   summary.get("skip_confirmation",
                                   getattr(config, "SKIP_CONFIRMATION", True))),
                "status": saved_status,
                "step": ("confirm_sentence" if is_sentence_confirm
                         else ("confirm" if is_confirm else "done")),
                "progress": (55 if is_sentence_confirm
                             else (60 if is_confirm else 100)),
                "message": summary.get("message", "全部完成！"),
                "created_at": summary.get("created_at", ""),
                "transcription_text": saved.get("transcription_text", ""),
                "segments": saved.get("segments", []),
                "difficult_words": saved.get("difficult_words", []),
                "replacements": saved.get("replacements", []),
                "translations": saved.get("translations", {}),
                "translated_indices": saved.get("translated_indices", []),
                "sentence_pairs": saved.get("sentence_pairs", []),
                "result": saved.get("result"),
                "time_mapping": saved.get("time_mapping", []),
                "video_result": saved.get("video_result"),
                "_basename": basename,
                "_video_path": saved.get("_video_path", ""),
                "_audio_path": (saved.get("result", {}).get("original_audio", "")
                                if saved.get("result") else ""),
            }
            # 恢复翻译/识词批次的断点数据
            if saved.get("_checkpoint_translate_batch"):
                task["_checkpoint_translate_batch"] = saved["_checkpoint_translate_batch"]
            if saved.get("_checkpoint_translations"):
                task["_checkpoint_translations"] = {
                    int(k): v for k, v in saved["_checkpoint_translations"].items()
                }
            if saved.get("_checkpoint_tts_idx"):
                task["_checkpoint_tts_idx"] = saved["_checkpoint_tts_idx"]

            # 对于 awaiting 状态，需要查找音频路径
            if (is_confirm or is_sentence_confirm) and not task["_audio_path"]:
                for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
                    p = os.path.join(config.DOWNLOAD_DIR, f"{basename}{ext}")
                    if os.path.exists(p):
                        task["_audio_path"] = p
                        break
            with tasks_lock:
                tasks[task_id] = task
            print(f"[恢复] 从 task_result.json 恢复任务 {task_id[:8]}... "
                  f"(basename={basename}, status={saved_status})")
            return task
        except Exception as e:
            print(f"[WARN] 读取 task_result.json 失败: {e}")

    # 回退方案：从散落文件拼装（适配旧数据）
    # 保留 completed / cancelled / error 三种终态，其余标记为中断
    sqlite_status = summary.get("status", "completed")
    is_terminal = sqlite_status in ("completed", "cancelled", "error")
    task = {
        "task_id": task_id,
        "url": summary.get("url", ""),
        "title": summary.get("title", ""),
                "skip_confirmation": summary.get("skip_confirmation",
                           getattr(config, "SKIP_CONFIRMATION", True)),
        "status": sqlite_status if is_terminal else "error",
        "step": "done" if sqlite_status == "completed" else "download",
        "progress": summary.get("progress", 100) if sqlite_status == "completed" else summary.get("progress", 0),
        "message": summary.get("message", "全部完成！") if is_terminal
                   else "任务因服务重启而中断，请重新提交",
        "created_at": summary.get("created_at", ""),
        "transcription_text": "",
        "segments": [],
        "difficult_words": [],
        "replacements": [],
        "translations": {},
        "translated_indices": [],
        "sentence_pairs": [],
        "result": None,
        "time_mapping": [],
        "_basename": basename,
        "_audio_path": "",
    }

    # 尝试恢复转录文本和 segments
    transcription_cache = os.path.join(config.OUTPUT_DIR, f"{basename}.json")
    if os.path.exists(transcription_cache):
        try:
            with open(transcription_cache, "r", encoding="utf-8") as f:
                transcription = json.load(f)
            raw_segments = transcription.get("segments", [])
            task["segments"] = [{"text": s.get("text", "").strip(),
                                 "start": s.get("start", 0),
                                 "end": s.get("end", 0)} for s in raw_segments]
            task["transcription_text"] = " ".join(
                s.get("text", "").strip() for s in raw_segments)
        except Exception as e:
            print(f"[WARN] 恢复转录数据失败: {e}")

    # 尝试恢复 difficult_words
    dw_path = os.path.join(result_dir, "difficult_words.json")
    if os.path.exists(dw_path):
        try:
            with open(dw_path, "r", encoding="utf-8") as f:
                task["difficult_words"] = json.load(f)
        except Exception as e:
            print(f"[WARN] 恢复生词数据失败: {e}")

    # 尝试从 vocabulary_book.json 恢复 replacements
    vocab_path = os.path.join(result_dir, "vocabulary_book.json")
    if os.path.exists(vocab_path):
        try:
            with open(vocab_path, "r", encoding="utf-8") as f:
                vocab_data = json.load(f)
            vocab_list = vocab_data.get("vocabulary", [])
            restored_replacements = []
            for item in vocab_list:
                if item.get("replaced_in_audio") and item.get("audio_timestamp"):
                    ts = item["audio_timestamp"]
                    restored_replacements.append({
                        "english": item.get("english", ""),
                        "chinese": item.get("chinese", ""),
                        "type": item.get("type", "word"),
                        "start": round(ts.get("start", 0), 2),
                        "end": round(ts.get("end", 0), 2),
                    })
            task["replacements"] = restored_replacements
        except Exception as e:
            print(f"[WARN] 从生词本恢复替换数据失败: {e}")

    # 查找音频文件路径
    audio_path = ""
    for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
        p = os.path.join(config.DOWNLOAD_DIR, f"{basename}{ext}")
        if os.path.exists(p):
            audio_path = p
            break
    task["_audio_path"] = audio_path

    # 构建 result 对象
    mixed_audio_path = os.path.join(
        result_dir, f"{basename}_mixed.{config.OUTPUT_FORMAT}")
    if os.path.exists(mixed_audio_path):
        task["result"] = {
            "basename": basename,
            "original_audio": audio_path,
            "mixed_audio": mixed_audio_path,
            "original_duration": summary.get("original_duration", 0),
            "mixed_duration": summary.get("mixed_duration", 0),
            "total_words": summary.get("total_words", 0),
            "total_replacements": summary.get("total_replacements", 0),
                    }

    with tasks_lock:
        tasks[task_id] = task
    print(f"[恢复] 从散落文件拼装恢复任务 {task_id[:8]}... (basename={basename})")
    return task
