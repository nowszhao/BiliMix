"""
Step 3 (Qwen3-TTS): 声音克隆语音合成模块
使用 Qwen3-TTS 从原音频中克隆说话人声音，合成生词语音。

核心策略:
- Segment 级别参考音频：每个生词使用其所在句子的原声作为克隆参考
  * 多说话人场景下，每个角色的声音自动匹配
  * 参考音频的语气、语速与目标句子一致，合成更自然
  * 极短 segment 回退到全篇最长纯净 segment 作为 fallback
- 紧挨着的相邻替换合并为一句话一起合成，避免割裂感
- 支持中英混合合成格式（如 "villain，反派"），用英文参考音频驱动，
  自然产生"外国人说中文"的语调效果，与原音频音色和谐一致
- 通过 subprocess 调用 qwen3-tts conda 环境中的 worker 脚本
"""
import hashlib
import json
import os
import subprocess
import tempfile

from pydub import AudioSegment

from core import config


def _build_tts_text(group_indices: list, replacements: list) -> str:
    """
    根据配置构建 TTS 合成文本。

    当 config.TTS_TEXT_FORMAT == "mixed" 时，生成中英混合格式（如 "villain，反派"），
    利用英文参考音频的声音克隆，让模型自然从英文过渡到中文，产生"外国人说中文"的效果。

    当 config.TTS_TEXT_FORMAT == "chinese_only" 时，生成纯中文格式（旧模式）。

    Args:
        group_indices: 组内 replacement 索引列表
        replacements: 完整的替换列表

    Returns:
        str: 拼接好的合成文本
    """
    text_format = getattr(config, "TTS_TEXT_FORMAT", "mixed")

    if text_format == "mixed":
        # 中英混合格式：每个词生成 "english，chinese"，多个词之间用逗号连接
        parts = []
        for idx in group_indices:
            r = replacements[idx]
            english = r["english"]
            chinese = r["chinese"]
            parts.append(f"{english}，{chinese}")
        return "，".join(parts)
    else:
        # 纯中文格式（旧模式）
        return "".join(replacements[idx]["chinese"] for idx in group_indices)


def extract_ref_audio_for_segments(audio_path: str, segments: list,
                                   replacements: list, output_dir: str,
                                   ref_duration: float = None) -> dict:
    """
    提取参考音频片段用于声音克隆。

    策略（segment 级别参考）：
    - 每个需要 TTS 的 segment 单独提取参考音频，实现"谁说的话就用谁的声音"
    - 参考音频就是该 segment 在原始音频中的完整原声
    - 对于太短的 segment（< MIN_REF_DURATION），向前后 segment 扩展以达到最小时长
    - 对于极短 segment（< 1 秒），回退到全篇最长纯净 segment 作为参考

    这样做的好处：
    1. 多说话人场景下，每个角色的生词自动用该角色的声音克隆
    2. 参考音频的语气、语速与目标句子一致，合成更自然

    Args:
        audio_path: 原始音频文件路径
        segments: WhisperX 的 segments 列表
        replacements: 替换列表，每项含 segment_index
        output_dir: 参考音频输出目录
        ref_duration: 参考音频最大时长上限（秒），默认用 config 配置

    Returns:
        dict: {segment_index: ref_audio_path} 映射
              每个 segment_index 指向各自的参考音频文件
    """
    if ref_duration is None:
        ref_duration = getattr(config, "QWEN3_TTS_REF_DURATION", 8)

    # segment 级别参考的最小时长（秒）
    min_ref_duration = getattr(config, "SEGMENT_REF_MIN_DURATION", 3.0)

    os.makedirs(output_dir, exist_ok=True)

    # 找出所有涉及的 segment_index（需要 TTS 的 segment）
    seg_indices = sorted(set(r["segment_index"] for r in replacements))

    print(f"[Step3-Qwen] 涉及 {len(seg_indices)} 个 segment 的替换，"
          f"采用 segment 级别参考模式：每句独立参考音频")

    audio = AudioSegment.from_file(audio_path)
    audio_duration_ms = len(audio)
    ref_duration_ms = int(ref_duration * 1000)
    min_ref_duration_ms = int(min_ref_duration * 1000)

    # ---- 准备 fallback 参考音频 ----
    # 对于极短 segment，需要一个全篇级的 fallback
    replacement_seg_set = set(r["segment_index"] for r in replacements)
    seg_durations = []
    for i, seg in enumerate(segments):
        start_s = seg.get("start", 0)
        end_s = seg.get("end", 0)
        dur = end_s - start_s
        is_clean = i not in replacement_seg_set
        seg_durations.append((i, start_s, end_s, dur, is_clean))

    # fallback: 优先选最长纯净 segment，全翻译时选最长 segment
    clean_segs = [s for s in seg_durations if s[4] and s[3] > 1.0]
    if not clean_segs:
        # 100% 翻译模式：所有 segment 都在替换集中，用最长 segment 做 fallback
        clean_segs = [s for s in seg_durations if s[3] > 1.0]
    clean_segs.sort(key=lambda x: x[3], reverse=True)

    fallback_path = None
    if clean_segs:
        fb_idx, fb_start, fb_end, fb_dur, _ = clean_segs[0]
        fb_start_ms = int(fb_start * 1000)
        fb_end_ms = int(fb_end * 1000)
        fb_mid_ms = (fb_start_ms + fb_end_ms) // 2
        half = ref_duration_ms // 2
        fb_clip_start = max(0, fb_mid_ms - half)
        fb_clip_end = min(audio_duration_ms, fb_clip_start + ref_duration_ms)
        if fb_clip_end - fb_clip_start < ref_duration_ms:
            fb_clip_start = max(0, fb_clip_end - ref_duration_ms)
        fb_clip = audio[fb_clip_start:fb_clip_end]
        fallback_path = os.path.join(output_dir, "ref_fallback.wav")
        fb_clip.export(fallback_path, format="wav")
        print(f"  [fallback] 全篇参考: segment[{fb_idx}] "
              f"({fb_start:.1f}s-{fb_end:.1f}s) -> {fallback_path}")

    # ---- 为每个 segment 提取独立参考音频 ----
    ref_map = {}
    for seg_idx in seg_indices:
        seg = segments[seg_idx]
        seg_start = seg.get("start", 0)
        seg_end = seg.get("end", 0)
        seg_dur = seg_end - seg_start

        seg_start_ms = int(seg_start * 1000)
        seg_end_ms = int(seg_end * 1000)
        seg_dur_ms = seg_end_ms - seg_start_ms

        # 极短 segment（< 1 秒）：使用 fallback
        if seg_dur < 1.0:
            if fallback_path:
                ref_map[seg_idx] = fallback_path
                print(f"  seg[{seg_idx}] ({seg_dur:.1f}s) 极短 -> 使用 fallback")
            else:
                print(f"  seg[{seg_idx}] ({seg_dur:.1f}s) 极短且无 fallback，跳过")
            continue

        # segment 足够长（>= min_ref_duration）：直接使用
        if seg_dur_ms >= min_ref_duration_ms:
            clip_start = seg_start_ms
            clip_end = seg_end_ms
        else:
            # segment 太短（< min_ref_duration 但 >= 1s）：向前后扩展
            # 以 segment 中心为基准，向两侧对称扩展到 min_ref_duration
            seg_mid_ms = (seg_start_ms + seg_end_ms) // 2
            half_needed = min_ref_duration_ms // 2
            clip_start = max(0, seg_mid_ms - half_needed)
            clip_end = min(audio_duration_ms, seg_mid_ms + half_needed)
            # 如果一侧不够，另一侧多取
            if clip_end - clip_start < min_ref_duration_ms:
                if clip_start == 0:
                    clip_end = min(audio_duration_ms, clip_start + min_ref_duration_ms)
                else:
                    clip_start = max(0, clip_end - min_ref_duration_ms)

        # 限制最大时长
        if clip_end - clip_start > ref_duration_ms:
            # 以 segment 中心为基准截取
            seg_mid_ms = (seg_start_ms + seg_end_ms) // 2
            clip_start = max(0, seg_mid_ms - ref_duration_ms // 2)
            clip_end = min(audio_duration_ms, clip_start + ref_duration_ms)

        ref_clip = audio[clip_start:clip_end]
        ref_filename = f"ref_seg_{seg_idx}.wav"
        ref_path = os.path.join(output_dir, ref_filename)
        ref_clip.export(ref_path, format="wav")

        ref_map[seg_idx] = ref_path
        print(f"  seg[{seg_idx}] ({seg_start:.1f}s-{seg_end:.1f}s, "
              f"{seg_dur:.1f}s) -> {ref_filename} "
              f"(clip: {clip_start/1000:.1f}s-{clip_end/1000:.1f}s, "
              f"{len(ref_clip)/1000:.1f}s)")

    print(f"[Step3-Qwen] 共提取 {len(set(ref_map.values()))} 个独立参考音频 "
          f"(覆盖 {len(ref_map)} 个 segment)")

    return ref_map


def group_adjacent_replacements(replacements: list) -> list:
    """
    检测并分组紧挨着的相邻替换点。

    如果两个替换点在时间上紧紧相邻（间隔小于 ADJACENT_MERGE_GAP），
    则合并为一个组，TTS 合成时将它们的中文翻译拼在一起作为一句话合成，
    避免独立合成导致的语调割裂。

    Args:
        replacements: 已按时间排序的替换列表
            [{start, end, chinese, english, type, segment_index}, ...]

    Returns:
        list[list[int]]: 分组结果，每组是 replacement 索引列表。
            例如 [[0], [1, 2], [3], [4, 5, 6]] 表示：
            - 索引 0 单独一组
            - 索引 1 和 2 是相邻词，合并为一组
            - 索引 3 单独一组
            - 索引 4, 5, 6 三个连续紧挨着，合并为一组
    """
    if not replacements:
        return []

    gap_threshold = getattr(config, "ADJACENT_MERGE_GAP", 0.12)

    groups = []
    current_group = [0]

    for i in range(1, len(replacements)):
        prev = replacements[i - 1]
        curr = replacements[i]

        # 判断是否紧挨着：
        # 1. 同一个 segment 内
        # 2. 前一个词的 end 到后一个词的 start 间隔很小
        gap = curr["start"] - prev["end"]

        if prev["segment_index"] == curr["segment_index"] and gap <= gap_threshold:
            current_group.append(i)
        else:
            groups.append(current_group)
            current_group = [i]

    groups.append(current_group)

    # 打印分组信息
    merged_groups = [g for g in groups if len(g) > 1]
    if merged_groups:
        print(f"[Step3-Qwen] 检测到 {len(merged_groups)} 组相邻词需要合并合成:")
        for g in merged_groups:
            words = " + ".join(
                f"{replacements[idx]['english']}({replacements[idx]['chinese']})"
                for idx in g
            )
            print(f"  合并: {words}")
    else:
        print(f"[Step3-Qwen] 没有检测到相邻词，全部独立合成")

    return groups


def synthesize_with_qwen_tts(replacements: list, ref_audio_map: dict,
                              segments: list, cache_dir: str,
                              adjacent_groups: list = None,
                              cancel_check=None, progress_cb=None,
                              task_id: str = None) -> dict:
    """
    使用 Qwen3-TTS 声音克隆合成中文语音。
    支持相邻词合并合成：紧挨着的生词拼成一句话一起合成，语调更自然。

    Args:
        replacements: 替换列表 [{english, chinese, type, start, end, segment_index}, ...]
        ref_audio_map: {segment_index: ref_audio_path} 说话人参考音频映射
        segments: WhisperX segments 列表
        cache_dir: TTS 缓存目录
        adjacent_groups: 相邻词分组 [[idx, ...], ...]，None 则每个词独立
        cancel_check: 终止检查回调
        progress_cb: 进度回调 (current, total)

    Returns:
        dict: {group_key: tts_audio_path} 映射
            - 单词组: group_key = "single_{replacement_index}"
            - 合并组: group_key = "merged_{first_index}_{last_index}"
    """
    os.makedirs(cache_dir, exist_ok=True)

    # 如果没有传入分组，则默认每个词独立一组
    if adjacent_groups is None:
        adjacent_groups = [[i] for i in range(len(replacements))]

    # 构建合成任务：每个组一个 job
    jobs = []
    for group in adjacent_groups:
        # 构建合成文本（支持中英混合格式或纯中文格式，由 config.TTS_TEXT_FORMAT 控制）
        merged_text = _build_tts_text(group, replacements)

        # 用组内第一个替换点的 segment 来确定参考音频
        first_r = replacements[group[0]]
        seg_idx = first_r["segment_index"]
        ref_audio = ref_audio_map.get(seg_idx, "")
        if not ref_audio:
            print(f"  [警告] seg[{seg_idx}] 无参考音频，跳过 '{merged_text}'")
            continue

        # 参考文本（英文原文，仅用于日志/调试，worker 中不再用于 ICL 模式）
        # 注意：由于参考音频是英文，如果用 ICL 模式会导致 TTS 输出英文而非中文，
        # 因此 worker 改用 x_vector_only_mode=True，只提取音色不使用参考文本。
        ref_text = segments[seg_idx].get("text", "").strip() if seg_idx < len(segments) else ""

        # 构建 group_key
        if len(group) == 1:
            group_key = f"single_{group[0]}"
        else:
            group_key = f"merged_{group[0]}_{group[-1]}"

        # 缓存 key: 合成文本 + 参考音频 + 合成模式标识 + 文本格式标识
        # 包含 "xvec" 和 text_format 标识，确保切换模式后不会命中旧缓存
        text_format = getattr(config, "TTS_TEXT_FORMAT", "mixed")
        cache_key = hashlib.md5(
            f"{merged_text}|{ref_audio}|xvec|{text_format}".encode()
        ).hexdigest()[:12]
        output_path = os.path.join(cache_dir, f"qwen_tts_{cache_key}.wav")

        jobs.append({
            "text": merged_text,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "output_path": output_path,
            "segment_index": seg_idx,
            "_group_key": group_key,
            "_group": group,
        })

    if not jobs:
        print("[Step3-Qwen] 没有需要合成的任务")
        return {}

    # 过滤掉已有缓存的 job
    pending_jobs = []
    cached_results = {}
    for job in jobs:
        if os.path.exists(job["output_path"]):
            cached_results[job["_group_key"]] = job["output_path"]
            print(f"  [缓存] {job['text']} -> {os.path.basename(job['output_path'])}")
        else:
            pending_jobs.append(job)

    if not pending_jobs:
        print(f"[Step3-Qwen] 全部命中缓存 ({len(cached_results)} 条)")
        return cached_results

    # 去重：相同 output_path（即相同文本+相同参考音频）只合成一次
    # 但需要记住所有被去重 job 的 group_key -> output_path 映射
    seen_outputs = set()
    unique_pending = []
    dup_count = 0
    output_to_group_keys = {}  # output_path -> [group_key, ...]
    for job in pending_jobs:
        out = job["output_path"]
        gk = job["_group_key"]
        if out not in output_to_group_keys:
            output_to_group_keys[out] = []
        output_to_group_keys[out].append(gk)

        if out not in seen_outputs:
            seen_outputs.add(out)
            unique_pending.append(job)
        else:
            dup_count += 1

    if dup_count > 0:
        print(f"[Step3-Qwen] 去重: {len(pending_jobs)} -> {len(unique_pending)} "
              f"（{dup_count} 条重复文本跳过）")
    pending_jobs = unique_pending

    print(f"[Step3-Qwen] 需要合成 {len(pending_jobs)} 条"
          f"（缓存命中 {len(cached_results)} 条）")

    if cancel_check and cancel_check():
        raise InterruptedError("任务已被用户终止")

    # 构建 worker 任务 JSON
    worker_task = {
        "model_path": config.QWEN3_TTS_MODEL_PATH,
        "device": getattr(config, "QWEN3_TTS_DEVICE", "cpu"),
        "language": getattr(config, "QWEN3_TTS_LANGUAGE", "Chinese"),
        "jobs": [
            {
                "text": j["text"],
                "ref_audio": j["ref_audio"],
                "ref_text": j["ref_text"],
                "output_path": j["output_path"],
            }
            for j in unique_pending
        ],
    }

    # 写入临时文件
    task_file = os.path.join(cache_dir, "qwen_tts_task.json")
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(worker_task, f, ensure_ascii=False, indent=2)

    # 调用 worker 脚本
    worker_script = os.path.join(config.BASE_DIR, "workers", "qwen_tts_worker.py")
    python_bin = getattr(config, "QWEN3_TTS_PYTHON",
                         "/root/miniconda3/envs/qwen3-tts/bin/python")

    cmd = [python_bin, worker_script, task_file]
    print(f"[Step3-Qwen] 启动 worker: {' '.join(cmd)}")

    if progress_cb:
        progress_cb(0, len(pending_jobs))

    # 使用 Popen + 实时读取 stderr 以跟踪进度
    # 超时按每个任务最多 120 秒计算（CPU 模式下单词合成可能较慢）
    per_job_timeout = getattr(config, "QWEN3_TTS_PER_JOB_TIMEOUT", 120)
    total_timeout = max(600, len(pending_jobs) * per_job_timeout + 300)  # 至少 600s
    # 单条任务无输出的最大等待时间（防止 worker 卡住导致 stderr 阻塞）
    stall_timeout = max(per_job_timeout * 2, 300)
    print(f"[Step3-Qwen] 超时设置: 总计{total_timeout}s, 单条卡住{stall_timeout}s "
          f"({len(pending_jobs)} 任务 × {per_job_timeout}s/任务 + 300s 模型加载)")

    import select as _select
    import time as _time
    start_time = _time.time()
    last_output_time = start_time  # 上次收到 stderr 输出的时间
    done_count = 0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,  # 使用 bytes 模式以支持非阻塞读取
    )

    # 注册 worker 进程到 task_subprocesses 以便用户终止时能杀掉
    if task_id:
        try:
            from core.task_manager import task_subprocesses
            task_subprocesses[task_id] = proc
        except Exception:
            pass

    # 实时读取 stderr 以监控进度和检测取消
    # 使用 select 实现带超时的读取，避免 for line in proc.stderr 的永久阻塞
    stderr_lines = []
    stderr_buffer = b""
    worker_stall_but_done = False  # 标记：worker 卡住但所有文件已生成
    try:
        while True:
            # 使用 select 等待 stderr 有数据可读，最多等待 5 秒
            ready, _, _ = _select.select([proc.stderr], [], [], 5.0)

            if ready:
                chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, 'read1') else proc.stderr.read(4096)
                if not chunk:
                    # stderr 关闭（进程即将结束）
                    break
                last_output_time = _time.time()
                stderr_buffer += chunk
                # 按行处理
                while b"\n" in stderr_buffer:
                    line_bytes, stderr_buffer = stderr_buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                    stderr_lines.append(line)
                    print(f"  {line}")

                    # 从 stderr 解析进度: "[QwenTTS] [3/79] 合成: ..."
                    if "[QwenTTS] [" in line and "/" in line:
                        try:
                            part = line.split("[QwenTTS] [")[1].split("]")[0]
                            current_str, total_str = part.split("/")
                            done_count = int(current_str)
                            if progress_cb:
                                progress_cb(done_count, len(pending_jobs))
                        except (ValueError, IndexError):
                            pass

            # 检查是否被用户取消
            if cancel_check and cancel_check():
                print("[Step3-Qwen] 用户取消，终止 worker 进程")
                proc.kill()
                proc.wait()
                raise InterruptedError("任务已被用户终止")

            # 检查总超时
            elapsed = _time.time() - start_time
            if elapsed > total_timeout:
                print(f"[Step3-Qwen] 总超时 ({elapsed:.0f}s > {total_timeout}s)，终止 worker")
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"Qwen3-TTS worker 总超时 ({elapsed:.0f}s)，"
                    f"已完成 {done_count}/{len(pending_jobs)} 条")

            # 检查单条卡住超时（长时间无任何输出）
            stall_elapsed = _time.time() - last_output_time
            if stall_elapsed > stall_timeout:
                # 检查是否所有任务已在磁盘上完成（worker 只是退出时卡住）
                disk_done = sum(1 for j in unique_pending if os.path.exists(j["output_path"]))
                if disk_done >= len(unique_pending):
                    print(f"[Step3-Qwen] Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                          f"但磁盘上已有 {disk_done}/{len(unique_pending)} 个文件，"
                          f"强制终止 worker 并继续")
                    proc.kill()
                    proc.wait()
                    worker_stall_but_done = True
                    break
                else:
                    print(f"[Step3-Qwen] Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                          f"已完成 {done_count}/{len(pending_jobs)} 条"
                          f"（磁盘 {disk_done}/{len(unique_pending)}），终止 worker")
                    proc.kill()
                    proc.wait()
                    raise RuntimeError(
                        f"Qwen3-TTS worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                        f"已完成 {done_count}/{len(pending_jobs)} 条")

            # 检查进程是否已退出（但 stderr 可能还有缓冲数据）
            if proc.poll() is not None and not ready:
                break

        # 处理 stderr_buffer 中剩余的不完整行
        if stderr_buffer:
            line = stderr_buffer.decode("utf-8", errors="replace").rstrip()
            if line:
                stderr_lines.append(line)
                print(f"  {line}")

        # 等待进程结束并获取 stdout（如果 worker 还在运行）
        if not worker_stall_but_done:
            stdout_data, _ = proc.communicate(timeout=60)
            stdout_data = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
        else:
            stdout_data = ""

    except InterruptedError:
        raise
    except Exception as e:
        # 确保进程被清理
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        # 清理 task_subprocesses 注册
        if task_id:
            try:
                from core.task_manager import task_subprocesses
                task_subprocesses.pop(task_id, None)
            except Exception:
                pass

    # 尝试从 stdout JSON 解析结果
    result_json = None
    if not worker_stall_but_done:
        if proc.returncode != 0:
            print(f"[Step3-Qwen] Worker 失败 (code={proc.returncode})")
            stderr_tail = "\n".join(stderr_lines[-10:]) if stderr_lines else "无日志"
            raise RuntimeError(f"Qwen3-TTS worker 失败 (code={proc.returncode}):\n{stderr_tail}")

        try:
            stdout_lines = stdout_data.strip().split("\n") if stdout_data else []
            result_json = json.loads(stdout_lines[-1])
        except (json.JSONDecodeError, IndexError) as e:
            print(f"[Step3-Qwen] 解析 worker 输出失败: {e}")
            print(f"  stdout: {(stdout_data or '')[-300:]}")
            # 不再直接抛异常，尝试从磁盘回收结果
            result_json = None

    # 构建最终结果映射
    tts_map = dict(cached_results)  # 先放入缓存结果

    if result_json:
        success_count = result_json.get("success", 0)
        failed_count = result_json.get("failed", 0)
        print(f"[Step3-Qwen] Worker 完成: {success_count} 成功, {failed_count} 失败")

        worker_results = result_json.get("results", [])
        for wr, job in zip(worker_results, unique_pending):
            out_path = wr.get("output_path", "")
            if out_path and os.path.exists(out_path):
                for gk in output_to_group_keys.get(job["output_path"], [job["_group_key"]]):
                    tts_map[gk] = out_path
            elif wr.get("error"):
                print(f"  [失败] {job['text']}: {wr['error']}")
    else:
        # 从磁盘回收已生成的文件（worker 卡住或输出解析失败时的降级方案）
        print("[Step3-Qwen] 从磁盘回收已生成的合成文件...")
        disk_ok = 0
        disk_miss = 0
        for job in unique_pending:
            if os.path.exists(job["output_path"]):
                disk_ok += 1
                for gk in output_to_group_keys.get(job["output_path"], [job["_group_key"]]):
                    tts_map[gk] = job["output_path"]
            else:
                disk_miss += 1
                print(f"  [缺失] {job['text'][:30]}...")
        print(f"[Step3-Qwen] 磁盘回收: {disk_ok} 成功, {disk_miss} 缺失")
        if disk_miss > 0:
            print(f"[Step3-Qwen] 警告: {disk_miss} 条合成文件缺失，对应句段将无 TTS 音频")

    if progress_cb:
        progress_cb(len(pending_jobs), len(pending_jobs))

    return tts_map


def synthesize_sentences_with_qwen_tts(
    segments: list,
    translated_indices: list,
    translations: dict,
    audio_path: str,
    cache_dir: str,
    voice_clone: bool = True,
    cancel_check=None,
    progress_cb=None,
    task_id: str = None,
) -> dict:
    """
    为句子翻译模式合成中文 TTS 音频。
    每个被翻译的句子独立合成，使用对应英文句子的原声作为声音克隆参考。
    多说话人场景下，每个说话人的声音自动匹配。

    Args:
        segments: WhisperX segments 列表
        translated_indices: 需要翻译的 segment 索引列表
        translations: {segment_index: chinese_text} 翻译结果
        audio_path: 原始音频文件路径
        cache_dir: TTS 缓存目录
        voice_clone: 是否克隆原声音色（否则无参考音频合成）
        cancel_check: 终止检查回调
        progress_cb: 进度回调 (current, total)

    Returns:
        dict: {segment_index: tts_wav_path} 映射
    """
    os.makedirs(cache_dir, exist_ok=True)

    if not translated_indices or not translations:
        print("[Step3-Qwen-Sentence] 没有需要合成的句子")
        return {}

    print(f"[Step3-Qwen-Sentence] 准备为 {len(translated_indices)} 个句子合成中文 TTS")

    # 提取参考音频（每个句子用自己的原声，区分多说话人）
    ref_audio_map = {}
    if voice_clone:
        ref_dir = os.path.join(cache_dir, "ref_audio")
        pseudo_replacements = [
            {"segment_index": idx}
            for idx in translated_indices
            if idx < len(segments)
        ]
        if pseudo_replacements:
            ref_audio_map = extract_ref_audio_for_segments(
                audio_path, segments, pseudo_replacements, ref_dir)
            print(f"[Step3-Qwen-Sentence] 提取了 {len(ref_audio_map)} 个句子参考音频")

    # 构建 TTS 合成任务（每个句子用自己的参考音频）
    jobs = []
    for seg_idx in translated_indices:
        chinese_text = translations.get(seg_idx, "")
        if not chinese_text:
            print(f"  [跳过] seg[{seg_idx}] 翻译文本为空，跳过 TTS 合成")
            continue

        ref_audio = ref_audio_map.get(seg_idx, "") if voice_clone else ""
        if voice_clone and not ref_audio:
            print(f"  [跳过] seg[{seg_idx}] 无参考音频，跳过 TTS 合成")
            continue
        ref_text = segments[seg_idx].get("text", "").strip() if seg_idx < len(segments) else ""

        cache_key = hashlib.md5(
            f"sent_{chinese_text}|{ref_audio}|xvec".encode()
        ).hexdigest()[:12]
        output_path = os.path.join(cache_dir, f"qwen_sent_{cache_key}.wav")

        jobs.append({
            "text": chinese_text,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "output_path": output_path,
            "segment_index": seg_idx,
        })

    if not jobs:
        print("[Step3-Qwen-Sentence] 没有有效的合成任务")
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
        print(f"[Step3-Qwen-Sentence] 全部命中缓存 ({len(cached_results)} 条)")
        return cached_results

    print(f"[Step3-Qwen-Sentence] 需合成 {len(pending_jobs)} 条"
          f"（缓存 {len(cached_results)} 条）")

    if cancel_check and cancel_check():
        raise InterruptedError("任务已被用户终止")

    # 调用 worker
    worker_task = {
        "model_path": config.QWEN3_TTS_MODEL_PATH,
        "device": getattr(config, "QWEN3_TTS_DEVICE", "cpu"),
        "language": getattr(config, "QWEN3_TTS_LANGUAGE", "Chinese"),
        "jobs": [
            {
                "text": j["text"],
                "ref_audio": j["ref_audio"],
                "ref_text": j["ref_text"],
                "output_path": j["output_path"],
            }
            for j in pending_jobs
        ],
    }

    task_file = os.path.join(cache_dir, "qwen_sent_task.json")
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(worker_task, f, ensure_ascii=False, indent=2)

    worker_script = os.path.join(config.BASE_DIR, "workers", "qwen_tts_worker.py")
    python_bin = getattr(config, "QWEN3_TTS_PYTHON",
                         "/root/miniconda3/envs/qwen3-tts/bin/python")

    cmd = [python_bin, worker_script, task_file]
    print(f"[Step3-Qwen-Sentence] 启动 worker: {' '.join(cmd)}")

    if progress_cb:
        progress_cb(0, len(pending_jobs))

    import select as _select
    import time as _time
    per_job_timeout = getattr(config, "QWEN3_TTS_PER_JOB_TIMEOUT", 120)
    total_timeout = max(600, len(pending_jobs) * per_job_timeout + 300)
    stall_timeout = max(per_job_timeout * 2, 300)
    print(f"[Step3-Qwen-Sentence] 超时设置: 总计{total_timeout}s, 单条卡住{stall_timeout}s")
    start_time = _time.time()
    last_output_time = start_time
    done_count = 0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,  # 使用 bytes 模式以支持非阻塞读取
    )

    # 注册 worker 进程到 task_subprocesses 以便用户终止时能杀掉
    if task_id:
        try:
            from core.task_manager import task_subprocesses
            task_subprocesses[task_id] = proc
        except Exception:
            pass

    stderr_lines = []
    stderr_buffer = b""
    worker_stall_but_done = False  # 标记：worker 卡住但所有文件已生成
    try:
        while True:
            # 使用 select 等待 stderr 有数据可读，最多等待 5 秒
            ready, _, _ = _select.select([proc.stderr], [], [], 5.0)

            if ready:
                chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, 'read1') else proc.stderr.read(4096)
                if not chunk:
                    break
                last_output_time = _time.time()
                stderr_buffer += chunk
                while b"\n" in stderr_buffer:
                    line_bytes, stderr_buffer = stderr_buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                    stderr_lines.append(line)
                    print(f"  {line}")

                    if "[QwenTTS] [" in line and "/" in line:
                        try:
                            part = line.split("[QwenTTS] [")[1].split("]")[0]
                            current_str, total_str = part.split("/")
                            done_count = int(current_str)
                            if progress_cb:
                                progress_cb(done_count, len(pending_jobs))
                        except (ValueError, IndexError):
                            pass

            if cancel_check and cancel_check():
                print("[Step3-Qwen-Sentence] 用户取消，终止 worker")
                proc.kill()
                proc.wait()
                raise InterruptedError("任务已被用户终止")

            elapsed = _time.time() - start_time
            if elapsed > total_timeout:
                print(f"[Step3-Qwen-Sentence] 总超时 ({elapsed:.0f}s)")
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"Qwen3-TTS 句子合成总超时 ({elapsed:.0f}s)，"
                    f"已完成 {done_count}/{len(pending_jobs)} 条")

            stall_elapsed = _time.time() - last_output_time
            if stall_elapsed > stall_timeout:
                # 检查是否所有任务已在磁盘上完成（worker 只是退出时卡住）
                disk_done = sum(1 for j in pending_jobs if os.path.exists(j["output_path"]))
                if disk_done >= len(pending_jobs):
                    print(f"[Step3-Qwen-Sentence] Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                          f"但磁盘上已有 {disk_done}/{len(pending_jobs)} 个文件，"
                          f"强制终止 worker 并继续")
                    proc.kill()
                    proc.wait()
                    worker_stall_but_done = True
                    break
                else:
                    print(f"[Step3-Qwen-Sentence] Worker 卡住 ({stall_elapsed:.0f}s 无输出)，"
                          f"已完成 {done_count}/{len(pending_jobs)} 条"
                          f"（磁盘 {disk_done}/{len(pending_jobs)}），终止 worker")
                    proc.kill()
                    proc.wait()
                    raise RuntimeError(
                        f"Qwen3-TTS 句子合成卡住 ({stall_elapsed:.0f}s 无输出)，"
                        f"已完成 {done_count}/{len(pending_jobs)} 条")

            if proc.poll() is not None and not ready:
                break

        # 处理 stderr_buffer 中剩余的不完整行
        if stderr_buffer:
            line = stderr_buffer.decode("utf-8", errors="replace").rstrip()
            if line:
                stderr_lines.append(line)
                print(f"  {line}")

        # 等待进程结束并获取 stdout（如果 worker 还在运行）
        if not worker_stall_but_done:
            stdout_data, _ = proc.communicate(timeout=60)
            stdout_data = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
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

    # 尝试从 stdout JSON 解析结果
    result_json = None
    if not worker_stall_but_done:
        if proc.returncode != 0:
            stderr_tail = "\n".join(stderr_lines[-10:]) if stderr_lines else "无日志"
            raise RuntimeError(f"Qwen3-TTS 句子合成失败 (code={proc.returncode}):\n{stderr_tail}")

        try:
            stdout_lines = stdout_data.strip().split("\n") if stdout_data else []
            result_json = json.loads(stdout_lines[-1])
        except (json.JSONDecodeError, IndexError) as e:
            print(f"[Step3-Qwen-Sentence] 解析 worker 输出失败: {e}")
            result_json = None

    # 构建结果映射
    tts_map = dict(cached_results)

    if result_json:
        success_count = result_json.get("success", 0)
        failed_count = result_json.get("failed", 0)
        print(f"[Step3-Qwen-Sentence] Worker 完成: {success_count} 成功, {failed_count} 失败")

        worker_results = result_json.get("results", [])
        for wr, job in zip(worker_results, pending_jobs):
            out_path = wr.get("output_path", "")
            if out_path and os.path.exists(out_path):
                tts_map[job["segment_index"]] = out_path
            elif wr.get("error"):
                print(f"  [失败] seg[{job['segment_index']}] {job['text'][:20]}: {wr['error']}")
    else:
        # 从磁盘回收已生成的文件（worker 卡住或输出解析失败时的降级方案）
        print("[Step3-Qwen-Sentence] 从磁盘回收已生成的合成文件...")
        disk_ok = 0
        disk_miss = 0
        for job in pending_jobs:
            if os.path.exists(job["output_path"]):
                disk_ok += 1
                tts_map[job["segment_index"]] = job["output_path"]
            else:
                disk_miss += 1
                print(f"  [缺失] seg[{job['segment_index']}] {job['text'][:30]}...")
        print(f"[Step3-Qwen-Sentence] 磁盘回收: {disk_ok} 成功, {disk_miss} 缺失")
        if disk_miss > 0:
            print(f"[Step3-Qwen-Sentence] 警告: {disk_miss} 条合成文件缺失，对应句段将无 TTS 音频")

    if progress_cb:
        progress_cb(len(pending_jobs), len(pending_jobs))

    return tts_map


def build_tts_audio_map_for_replacements(replacements: list,
                                          tts_map: dict,
                                          adjacent_groups: list = None) -> dict:
    """
    将 group_key 映射转换为 step4 需要的替换索引映射。

    支持两种模式：
    - 有 adjacent_groups: 合并组用一个 TTS 音频替换整段
    - 无 adjacent_groups: 每个替换独立映射（兼容旧逻辑）

    Args:
        replacements: 替换列表
        tts_map: {group_key: tts_audio_path}
        adjacent_groups: 相邻词分组 [[idx, ...], ...]

    Returns:
        dict: {group_key: {"path": tts_audio_path, "indices": [idx, ...]}}
              供 step4 的 apply_replacements 使用
    """
    if adjacent_groups is None:
        adjacent_groups = [[i] for i in range(len(replacements))]

    index_map = {}
    for group in adjacent_groups:
        if len(group) == 1:
            group_key = f"single_{group[0]}"
        else:
            group_key = f"merged_{group[0]}_{group[-1]}"

        if group_key in tts_map:
            index_map[group_key] = {
                "path": tts_map[group_key],
                "indices": group,
            }

    return index_map
