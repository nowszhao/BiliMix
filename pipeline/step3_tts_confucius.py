"""
Step 3 (Confucius4-TTS-CPU): 声音克隆语音合成模块
通过 subprocess 调用 Confucius4-TTS-CPU worker 进行零样本跨语言声音克隆。

架构:
  BiliMix ──subprocess──→ confucius_tts_worker.py
                          ├── 加载 ConfuciusTTS 模型一次
                          ├── 逐句合成中文 TTS
                          │   ├── text: 待合成中文文本
                          │   ├── prompt_wav: 参考音频（零样本音色克隆）
                          │   └── lang: zh
                          └── 输出 WAV 文件

前置条件:
  1. 已克隆 Confucius4-TTS-CPU:
     git clone https://github.com/nowszhao/Confucius4-TTS-CPU.git
  2. 已安装 Python 依赖（torch, transformers, safetensors 等）
  3. 首次运行会自动从 HuggingFace 下载模型权重 (netease-youdao/Confucius4-TTS)

特点:
  - 零样本声音克隆，不需要微调
  - 多语言支持（zh, en, ja, ko 等）
  - 纯 CPU 推理（约 10x 实时速度）
  - 音色克隆质量优秀
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

from core import config


def _get_confucius_root() -> str:
    """获取 Confucius4-TTS-CPU 项目根目录"""
    path = getattr(config, "CONFUCIUS4_TTS_ROOT", "")
    if path and os.path.isdir(path):
        return os.path.abspath(path)
    # 默认路径：与 BiliMix 项目同级
    default = os.path.join(config.BASE_DIR, "..", "Confucius4-TTS-CPU")
    return os.path.abspath(default)


def synthesize_sentences_with_confucius_tts(
    segments: list,
    translated_indices: list,
    translations: dict,
    audio_path: str,
    cache_dir: str,
    ref_audio_map: dict = None,
    cancel_check=None,
    progress_cb=None,
    task_id: str = None,
) -> dict:
    """
    使用 Confucius4-TTS-CPU 为句子翻译模式合成中文 TTS 音频。

    每个被翻译的句子独立调用 ConfuciusTTS 合成，使用对应英文句子的原声
    作为零样本声音克隆的参考音频。

    Args:
        segments: WhisperX segments 列表
        translated_indices: 需要翻译的 segment 索引列表
        translations: {segment_index: chinese_text} 翻译结果
        audio_path: 原始音频文件路径
        cache_dir: TTS 缓存目录
        ref_audio_map: {segment_index: ref_audio_path} 说话人参考音频映射
        cancel_check: 终止检查回调
        progress_cb: 进度回调 (current, total)
        task_id: 任务 ID（用于取消注册）

    Returns:
        dict: {segment_index: tts_wav_path} 映射
    """
    os.makedirs(cache_dir, exist_ok=True)

    if not translated_indices or not translations:
        print("[Step3-Confucius] 没有需要合成的句子")
        return {}

    total = len(translated_indices)
    print(f"[Step3-Confucius] 准备为 {total} 个句子合成中文 TTS")

    # 构建合成任务
    jobs = []
    for seg_idx in translated_indices:
        chinese_text = translations.get(seg_idx, "")
        if not chinese_text:
            print(f"  [跳过] seg[{seg_idx}] 翻译文本为空，跳过 TTS 合成")
            continue

        ref_audio = ""
        if ref_audio_map:
            ref_audio = ref_audio_map.get(seg_idx, "")

        if not ref_audio or not os.path.isfile(ref_audio):
            print(f"  [跳过] seg[{seg_idx}] 缺少参考音频 ({ref_audio or '(空)'})")
            continue

        # 缓存 key
        cache_key = hashlib.md5(
            f"conf_{chinese_text}|{ref_audio}".encode()
        ).hexdigest()[:12]
        output_path = os.path.join(cache_dir, f"confucius_tts_{cache_key}.wav")

        jobs.append({
            "text": chinese_text,
            "ref_audio": ref_audio,
            "output_path": output_path,
            "segment_index": seg_idx,
        })

    if not jobs:
        print("[Step3-Confucius] 没有有效的合成任务")
        return {}

    # 检查缓存
    pending_jobs = []
    cached_results = {}
    for job in jobs:
        if os.path.exists(job["output_path"]):
            cached_results[job["segment_index"]] = job["output_path"]
            print(f"  [缓存] seg[{job['segment_index']}] {job['text'][:20]}...")
        else:
            pending_jobs.append(job)

    if not pending_jobs:
        print(f"[Step3-Confucius] 全部命中缓存 ({len(cached_results)} 条)")
        return cached_results

    print(f"[Step3-Confucius] 需合成 {len(pending_jobs)} 条"
          f"（缓存 {len(cached_results)} 条）")

    if cancel_check and cancel_check():
        raise InterruptedError("任务已被用户终止")

    if progress_cb:
        progress_cb(0, len(pending_jobs))

    # 构建 worker 任务 JSON
    confucius_root = _get_confucius_root()
    config_path = os.path.join(confucius_root, "config", "inference_config.yaml")

    worker_task = {
        "config_path": config_path,
        "device": getattr(config, "CONFUCIUS4_TTS_DEVICE", "cpu"),
        "jobs": [
            {
                "text": j["text"],
                "ref_audio": j["ref_audio"],
                "output_path": j["output_path"],
            }
            for j in pending_jobs
        ],
    }

    task_file = os.path.join(cache_dir, "confucius_tts_task.json")
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(worker_task, f, ensure_ascii=False, indent=2)

    # 定位 worker 脚本和 Python 解释器
    worker_script = os.path.join(config.BASE_DIR, "workers", "confucius_tts_worker.py")
    python_bin = getattr(config, "CONFUCIUS4_TTS_PYTHON", "") or sys.executable

    # 设置环境变量 CONFUCIUS4_TTS_ROOT
    env = os.environ.copy()
    env["CONFUCIUS4_TTS_ROOT"] = confucius_root

    cmd = [python_bin, worker_script, task_file]
    print(f"[Step3-Confucius] 启动 worker: {' '.join(cmd)}")

    # 超时配置：CPU 推理较慢，每句约 30-90s
    per_job_timeout = getattr(config, "CONFUCIUS4_TTS_PER_JOB_TIMEOUT", 180)
    total_timeout = max(900, len(pending_jobs) * per_job_timeout + 600)
    stall_timeout = max(per_job_timeout * 2, 600)

    print(f"[Step3-Confucius] 超时设置: 总计{total_timeout}s, "
          f"单条卡住{stall_timeout}s ({len(pending_jobs)} 任务 × {per_job_timeout}s/任务 + 600s 模型加载)")

    import select as _select
    start_time = time.time()
    last_output_time = start_time
    done_count = 0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        env=env,
    )

    # 后台线程持续读取 stdout，防止管道缓冲区填满导致 worker 死锁
    _stdout_chunks = []
    def _drain_stdout():
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                _stdout_chunks.append(chunk)
        except Exception:
            pass
    _stdout_reader = threading.Thread(target=_drain_stdout, daemon=True)
    _stdout_reader.start()

    # 注册 worker 进程到 task_subprocesses 以便用户终止
    if task_id:
        try:
            from core.task_manager import task_subprocesses
            task_subprocesses[task_id] = proc
        except Exception:
            pass

    stderr_lines = []
    stderr_buffer = b""
    worker_stall_but_done = False
    try:
        while True:
            ready, _, _ = _select.select([proc.stderr], [], [], 5.0)

            if ready:
                chunk = (proc.stderr.read1(4096) if hasattr(proc.stderr, 'read1')
                         else proc.stderr.read(4096))
                if not chunk:
                    break
                last_output_time = time.time()
                stderr_buffer += chunk
                while b"\n" in stderr_buffer:
                    line_bytes, stderr_buffer = stderr_buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                    stderr_lines.append(line)
                    print(f"  {line}")

                    # 解析进度: "[ConfuciusWorker] [3/10] ..."
                    if "[ConfuciusWorker] [" in line and "/" in line:
                        try:
                            part = line.split("[ConfuciusWorker] [")[1].split("]")[0]
                            current_str, total_str = part.split("/")
                            done_count = int(current_str)
                            if progress_cb:
                                progress_cb(done_count, len(pending_jobs))
                        except (ValueError, IndexError):
                            pass

            if cancel_check and cancel_check():
                print("[Step3-Confucius] 用户取消，终止 worker 进程")
                proc.kill()
                proc.wait()
                raise InterruptedError("任务已被用户终止")

            elapsed = time.time() - start_time
            if elapsed > total_timeout:
                print(f"[Step3-Confucius] 总超时 ({elapsed:.0f}s > {total_timeout}s)，终止 worker")
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"Confucius4-TTS worker 总超时 ({elapsed:.0f}s)，"
                    f"已完成 {done_count}/{len(pending_jobs)} 条")

            stall_elapsed = time.time() - last_output_time
            if stall_elapsed > stall_timeout:
                disk_done = sum(1 for j in pending_jobs if os.path.exists(j["output_path"]))
                if disk_done >= len(pending_jobs):
                    print(f"[Step3-Confucius] Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                          f"但磁盘上已有 {disk_done}/{len(pending_jobs)} 个文件，强制终止并继续")
                    proc.kill()
                    proc.wait()
                    worker_stall_but_done = True
                    break
                else:
                    print(f"[Step3-Confucius] Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                          f"磁盘 {disk_done}/{len(pending_jobs)}，终止 worker")
                    proc.kill()
                    proc.wait()
                    raise RuntimeError(
                        f"Confucius4-TTS worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                        f"已完成 {done_count}/{len(pending_jobs)} 条")

            if proc.poll() is not None and not ready:
                break

        if stderr_buffer:
            line = stderr_buffer.decode("utf-8", errors="replace").rstrip()
            if line:
                stderr_lines.append(line)
                print(f"  {line}")

        proc.stderr.close()
        _stdout_reader.join(timeout=10)
        if not worker_stall_but_done:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stdout_data = b"".join(_stdout_chunks).decode("utf-8", errors="replace") if _stdout_chunks else ""
        else:
            stdout_data = ""

    except InterruptedError:
        raise
    except Exception as e:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        if task_id:
            try:
                from core.task_manager import task_subprocesses
                task_subprocesses.pop(task_id, None)
            except Exception:
                pass

    # 解析 worker 输出
    tts_map = dict(cached_results)

    if not worker_stall_but_done:
        if proc.returncode != 0 and stdout_data.strip():
            stderr_tail = "\n".join(stderr_lines[-10:]) if stderr_lines else "无日志"
            print(f"[Step3-Confucius] Worker 失败 (code={proc.returncode})")
            # 尝试从磁盘回收
            disk_recovered = 0
            for job in pending_jobs:
                if os.path.exists(job["output_path"]):
                    tts_map[job["segment_index"]] = job["output_path"]
                    disk_recovered += 1
            if disk_recovered == 0:
                raise RuntimeError(f"Confucius4-TTS worker 失败 (code={proc.returncode}):\n{stderr_tail}")
            print(f"[Step3-Confucius] 从磁盘回收 {disk_recovered} 个文件")

    # 从磁盘回收生成的音频
    for job in pending_jobs:
        if job["segment_index"] not in tts_map and os.path.exists(job["output_path"]):
            tts_map[job["segment_index"]] = job["output_path"]

    # 尝试从 stdout 解析 JSON 结果
    if not worker_stall_but_done and stdout_data.strip():
        try:
            stdout_lines = stdout_data.strip().split("\n")
            result_json = json.loads(stdout_lines[-1])
            if result_json.get("results"):
                for wr, job in zip(result_json["results"], pending_jobs):
                    if wr.get("output_path") and os.path.exists(wr["output_path"]):
                        tts_map[job["segment_index"]] = wr["output_path"]
        except (json.JSONDecodeError, IndexError):
            pass

    if progress_cb:
        progress_cb(len(pending_jobs), len(pending_jobs))

    completed = len(tts_map) - len(cached_results)
    print(f"[Step3-Confucius] 合成完成: {len(tts_map)}/{total} 条 (新合成 {completed} 条)")
    return tts_map
