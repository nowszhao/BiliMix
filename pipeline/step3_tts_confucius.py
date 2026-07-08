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


def _synthesize_parallel(
    pending_jobs: list,
    num_workers: int,
    config_path: str,
    python_bin: str,
    worker_script: str,
    env: dict,
    cache_dir: str,
    cancel_check,
    progress_cb,
    task_id: str,
    cached_results: dict,
) -> dict:
    """Split pending jobs into N chunks and run N workers in parallel."""
    import select as _select

    total = len(pending_jobs)
    chunk_size = total // num_workers
    chunks = []
    for i in range(num_workers):
        start = i * chunk_size
        if i == num_workers - 1:
            end = total
        else:
            end = start + chunk_size
        chunks.append(pending_jobs[start:end])

    print(f"[Step3-Confucius] 并行模式：{num_workers} 个 Worker，"
          f"分片 {[len(c) for c in chunks]}")

    # Write sub-task JSONs
    sub_task_files = []
    for i, chunk in enumerate(chunks):
        sub_task = {
            "config_path": config_path,
            "device": env.get("CONFUCIUS4_TTS_DEVICE", "cpu"),
            "jobs": [{"text": j["text"], "ref_audio": j["ref_audio"],
                      "output_path": j["output_path"]} for j in chunk],
            "temperature": getattr(config, "CONFUCIUS4_TTS_TEMPERATURE", 0.8),
            "top_p": getattr(config, "CONFUCIUS4_TTS_TOP_P", 0.8),
            "top_k": getattr(config, "CONFUCIUS4_TTS_TOP_K", 30),
            "num_beams": getattr(config, "CONFUCIUS4_TTS_NUM_BEAMS", 3),
            "repetition_penalty": getattr(config, "CONFUCIUS4_TTS_REPETITION_PENALTY", 10.0),
            "n_timesteps": getattr(config, "CONFUCIUS4_TTS_N_TIMESTEPS", 25),
            "inference_cfg_rate": getattr(config, "CONFUCIUS4_TTS_INFERENCE_CFG_RATE", 0.7),
            "verbose": False,
        }
        sub_file = os.path.join(cache_dir, f"confucius_tts_task_w{i}.json")
        with open(sub_file, "w", encoding="utf-8") as f:
            json.dump(sub_task, f, ensure_ascii=False, indent=2)
        sub_task_files.append(sub_file)

    # Set per-process thread limits
    env_parallel = env.copy()
    threads_per = max(1, os.cpu_count() // (num_workers * 2))
    env_parallel["OMP_NUM_THREADS"] = str(threads_per)
    env_parallel["MKL_NUM_THREADS"] = str(threads_per)

    # Launch all workers
    procs = []
    for i, stf in enumerate(sub_task_files):
        cmd = [python_bin, worker_script, stf]
        print(f"  [Worker-{i}] {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, env=env_parallel,
        )
        procs.append(proc)

    # Drain stdout threads
    all_stdout = [[] for _ in range(num_workers)]

    def _drain(idx):
        try:
            while True:
                chunk = procs[idx].stdout.read(4096)
                if not chunk:
                    break
                all_stdout[idx].append(chunk)
        except Exception:
            pass

    for i in range(num_workers):
        threading.Thread(target=_drain, args=(i,), daemon=True).start()

    # Register for cancellation
    if task_id:
        try:
            from core.task_manager import task_subprocesses
            task_subprocesses[task_id] = procs
        except Exception:
            pass

    start_time = time.time()
    stderr_bufs = [b"" for _ in range(num_workers)]
    all_fds = [p.stderr for p in procs]
    fd_to_idx = {p.stderr.fileno(): i for i, p in enumerate(procs)}
    _last_progress_cb = 0  # throttle

    def _count_disk_all():
        return sum(1 for j in pending_jobs if os.path.exists(j["output_path"]))

    try:
        while True:
            ready, _, _ = _select.select(all_fds, [], [], 5.0)
            for fd in ready:
                idx = fd_to_idx[fd.fileno()]
                try:
                    chunk = (procs[idx].stderr.read1(4096)
                             if hasattr(procs[idx].stderr, 'read1')
                             else procs[idx].stderr.read(4096))
                except Exception:
                    chunk = b""
                if not chunk:
                    continue
                stderr_bufs[idx] += chunk
                while b"\n" in stderr_bufs[idx]:
                    line_b, stderr_bufs[idx] = stderr_bufs[idx].split(b"\n", 1)
                    line = line_b.decode("utf-8", errors="replace").rstrip()
                    # stderr for logging only, no progress parsing

            # 进度上报：直接数磁盘文件
            disk_count = _count_disk_all()
            if progress_cb and disk_count != _last_progress_cb:
                _last_progress_cb = disk_count
                progress_cb(disk_count, total)

            if cancel_check and cancel_check():
                print("[Step3-Confucius] 用户取消，终止所有 worker")
                for p in procs:
                    if p.poll() is None:
                        p.kill(); p.wait()
                raise InterruptedError("任务已被用户终止")

            # Check if all done
            if all(p.poll() is not None for p in procs):
                break

    finally:
        if task_id:
            try:
                from core.task_manager import task_subprocesses
                task_subprocesses.pop(task_id, None)
            except Exception:
                pass

    # Cleanup temp files
    for stf in sub_task_files:
        try:
            os.remove(stf)
        except OSError:
            pass

    # Check exit codes and collect results
    tts_map = dict(cached_results)
    failed_workers = []
    for i, p in enumerate(procs):
        if p.returncode != 0:
            failed_workers.append(i)

    for job in pending_jobs:
        if job["segment_index"] not in tts_map and os.path.exists(job["output_path"]):
            tts_map[job["segment_index"]] = job["output_path"]

    if progress_cb:
        progress_cb(total, total)

    print(f"[Step3-Confucius] 并行合成完成: {len(tts_map) - len(cached_results)} 条新增")
    if failed_workers:
        print(f"  [警告] Worker {failed_workers} 异常退出，已从磁盘回收结果")

    return tts_map


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

    # 定位 worker 脚本和 Python 解释器
    confucius_root = _get_confucius_root()
    config_path = os.path.join(confucius_root, "config", "inference_config.yaml")
    worker_script = os.path.join(config.BASE_DIR, "workers", "confucius_tts_worker.py")
    python_bin = getattr(config, "CONFUCIUS4_TTS_PYTHON", "") or sys.executable
    env = os.environ.copy()
    env["CONFUCIUS4_TTS_ROOT"] = confucius_root

    num_workers = getattr(config, "CONFUCIUS4_TTS_NUM_WORKERS", 1)
    num_workers = max(1, min(num_workers, len(pending_jobs)))
    num_workers = int(num_workers)

    if num_workers > 1:
        tts_map_partial = _synthesize_parallel(
            pending_jobs, num_workers, config_path, python_bin, worker_script,
            env, cache_dir, cancel_check, progress_cb, task_id, cached_results,
        )
        return tts_map_partial

    # ---- 单 Worker 模式 ----
    worker_task = {
        "config_path": config_path,
        "device": getattr(config, "CONFUCIUS4_TTS_DEVICE", "cpu"),
        "temperature": getattr(config, "CONFUCIUS4_TTS_TEMPERATURE", 0.8),
        "top_p": getattr(config, "CONFUCIUS4_TTS_TOP_P", 0.8),
        "top_k": getattr(config, "CONFUCIUS4_TTS_TOP_K", 30),
        "num_beams": getattr(config, "CONFUCIUS4_TTS_NUM_BEAMS", 3),
        "repetition_penalty": getattr(config, "CONFUCIUS4_TTS_REPETITION_PENALTY", 10.0),
        "n_timesteps": getattr(config, "CONFUCIUS4_TTS_N_TIMESTEPS", 25),
        "inference_cfg_rate": getattr(config, "CONFUCIUS4_TTS_INFERENCE_CFG_RATE", 0.7),
        "verbose": False,
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
    _last_progress_cb = 0  # throttle progress callbacks

    def _count_disk():
        return sum(1 for j in pending_jobs if os.path.exists(j["output_path"]))

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

            # 进度上报：直接数磁盘上的 WAV 文件数
            disk_count = _count_disk()
            if progress_cb and disk_count != _last_progress_cb:
                _last_progress_cb = disk_count
                progress_cb(disk_count, len(pending_jobs))

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
                    f"已完成 {disk_count}/{len(pending_jobs)} 条")

            stall_elapsed = time.time() - last_output_time
            if stall_elapsed > stall_timeout:
                disk_done = _count_disk()
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
        if proc.returncode != 0:
            stderr_tail = "\n".join(stderr_lines[-10:]) if stderr_lines else "无日志"
            print(f"[Step3-Confucius] Worker 异常退出 (code={proc.returncode})")
            # 尝试从磁盘回收
            for job in pending_jobs:
                if os.path.exists(job["output_path"]):
                    tts_map[job["segment_index"]] = job["output_path"]
            if len(tts_map) == len(cached_results):
                # 磁盘也回收不到任何文件 → 真正失败
                raise RuntimeError(
                    f"Confucius4-TTS worker 异常退出 (code={proc.returncode}):\n{stderr_tail}")

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
