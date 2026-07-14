"""
Step 1: WhisperX 转录模块
将英文音频转录为带词级别时间戳的JSON文件
"""
import json
import os
import subprocess
import sys

from core import config


# 触发缺口补录的最小间隙（秒）：相邻 segment 之间超过此值才检查是否漏检
_GAP_MIN_SECONDS = getattr(config, "TRANSCRIBE_GAP_MIN_SECONDS", 3.0)
# 判定缺口内确实有语音内容的最小平均音量阈值（dBFS，越接近0越响）
_GAP_VOICE_DBFS_THRESHOLD = getattr(config, "TRANSCRIBE_GAP_VOICE_DBFS", -35.0)


def _probe_audio_duration(audio_path: str) -> float:
    """探测音频文件总时长（秒）"""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=30).strip()
        return float(out)
    except Exception:
        return 0.0


def _detect_mean_volume_dbfs(audio_path: str, start: float, end: float) -> float:
    """检测音频片段的平均音量（dBFS）。返回 -inf 表示纯静音或探测失败。"""
    cmd = ["ffmpeg", "-y", "-loglevel", "info",
           "-ss", str(start), "-to", str(end),
           "-i", audio_path, "-af", "volumedetect", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        for line in r.stderr.splitlines():
            if "mean_volume" in line:
                val = line.split(":")[-1].strip().replace("dB", "").strip()
                return float(val)
    except Exception:
        pass
    return float("-inf")


def _find_gaps(segments: list, total_duration: float) -> list:
    """
    找出 segments 之间（以及首尾）超过 _GAP_MIN_SECONDS 的时间间隙。

    Returns:
        list[tuple[float, float]]: [(gap_start, gap_end), ...]
    """
    gaps = []
    prev_end = 0.0
    for seg in segments:
        seg_start = seg.get("start", 0)
        if seg_start - prev_end >= _GAP_MIN_SECONDS:
            gaps.append((prev_end, seg_start))
        prev_end = max(prev_end, seg.get("end", 0))
    if total_duration - prev_end >= _GAP_MIN_SECONDS:
        gaps.append((prev_end, total_duration))
    return gaps


def _retranscribe_gap(audio_path: str, gap_start: float, gap_end: float) -> list:
    """
    对指定时间区间单独提取音频并重新转录，返回该区间内的 segments
    （时间戳已还原为相对原始音频的绝对时间）。

    使用 large-v3 模型而非主转录的 medium 模型：短音频片段缺乏上下文时，
    medium 模型容易产生"幻觉"（复读前文内容而非识别当前音频的真实内容），
    实测 large-v3 在相同窄窗口下能正确识别、不产生幻觉重复。
    仅补录这一分支使用更强模型，不影响主转录流程的速度。
    """
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="bilimix_gapfix_")
    tmp_audio = os.path.join(tmp_dir, "gap.wav")
    try:
        r = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", audio_path, "-ss", str(gap_start), "-to", str(gap_end),
            tmp_audio,
        ], capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(tmp_audio):
            return []

        bin_cmd = config.WHISPERX_BIN if isinstance(config.WHISPERX_BIN, list) \
                  else [config.WHISPERX_BIN]
        gap_model = getattr(config, "WHISPERX_GAP_FILL_MODEL", "large-v3")
        cmd = bin_cmd + [tmp_audio,
            "--model", gap_model,
            "--device", config.WHISPERX_DEVICE,
            "--compute_type", config.WHISPERX_COMPUTE_TYPE,
            "--batch_size", str(config.WHISPERX_BATCH_SIZE),
            "--language", config.WHISPERX_LANGUAGE,
            "--output_dir", tmp_dir,
            "--output_format", "json",
        ]
        env = os.environ.copy()
        env["PYTORCH_ENABLE_WEIGHTS_ONLY_LOAD"] = "0"
        r2 = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
        if r2.returncode != 0:
            print(f"[Step1] 缺口补录失败 ({gap_start:.1f}-{gap_end:.1f}s): {r2.stderr[-300:]}")
            return []

        json_path = os.path.join(tmp_dir, "gap.json")
        if not os.path.exists(json_path):
            return []
        with open(json_path, "r", encoding="utf-8") as f:
            gap_data = json.load(f)

        # 时间戳偏移还原为原始音频的绝对时间
        result_segs = []
        for seg in gap_data.get("segments", []):
            new_seg = dict(seg)
            new_seg["start"] = seg.get("start", 0) + gap_start
            new_seg["end"] = seg.get("end", 0) + gap_start
            for w in new_seg.get("words", []):
                if "start" in w:
                    w["start"] += gap_start
                if "end" in w:
                    w["end"] += gap_start
            result_segs.append(new_seg)
        return result_segs
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _fill_transcription_gaps(transcription: dict, audio_path: str) -> dict:
    """
    检测转录结果中的时间缺口，对确实存在语音内容（非静音）的缺口
    单独重新转录并插入缺失的 segments。

    背景：WhisperX 在处理较长/复杂音频时，VAD 或强制对齐(forced alignment)
    偶尔会漏检某个时间段，导致该段语音内容完全丢失（不产生任何 segment）。
    单独截取该时间段重新转录通常能正确识别出内容。
    """
    segments = transcription.get("segments", [])
    if not segments:
        return transcription

    total_duration = _probe_audio_duration(audio_path)
    if total_duration <= 0:
        return transcription

    gaps = _find_gaps(segments, total_duration)
    if not gaps:
        return transcription

    print(f"[Step1] 检测到 {len(gaps)} 个可疑缺口，逐一核实是否存在漏检语音...")
    gap_model = getattr(config, "WHISPERX_GAP_FILL_MODEL", "large-v3")
    new_segments = []
    for gap_start, gap_end in gaps:
        volume = _detect_mean_volume_dbfs(audio_path, gap_start, gap_end)
        if volume <= _GAP_VOICE_DBFS_THRESHOLD:
            print(f"  [{gap_start:.1f}-{gap_end:.1f}s] 平均音量 {volume:.1f}dB，判定为静音，跳过")
            continue
        print(f"  [{gap_start:.1f}-{gap_end:.1f}s] 平均音量 {volume:.1f}dB，疑似漏检，"
              f"用 {gap_model} 重新转录...")
        gap_segs = _retranscribe_gap(audio_path, gap_start, gap_end)
        if gap_segs:
            print(f"  [{gap_start:.1f}-{gap_end:.1f}s] 补录成功: {len(gap_segs)} 个句子")
            new_segments.extend(gap_segs)
        else:
            print(f"  [{gap_start:.1f}-{gap_end:.1f}s] 补录未获得有效内容")

    if new_segments:
        all_segments = segments + new_segments
        all_segments.sort(key=lambda s: s.get("start", 0))
        transcription = dict(transcription)
        transcription["segments"] = all_segments
        print(f"[Step1] 缺口补录完成，共补充 {len(new_segments)} 个句子，"
              f"总句数: {len(segments)} -> {len(all_segments)}")

    return transcription


def transcribe(audio_path: str, output_dir: str = None) -> dict:
    """
    使用 WhisperX 转录音频，返回词级别时间戳数据。

    如果已有缓存的转录结果JSON，直接读取返回。
    否则调用 whisperx 命令行工具进行转录。

    Args:
        audio_path: 音频文件路径
        output_dir: 输出目录，默认使用 config.OUTPUT_DIR

    Returns:
        dict: WhisperX 转录结果 (segments + word_segments)
    """
    if output_dir is None:
        output_dir = config.OUTPUT_DIR

    os.makedirs(output_dir, exist_ok=True)

    # 检查是否已有缓存的转录结果
    basename = os.path.splitext(os.path.basename(audio_path))[0]
    json_path = os.path.join(output_dir, f"{basename}.json")

    if os.path.exists(json_path):
        print(f"[Step1] 发现已有转录缓存: {json_path}")
        with open(json_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        cached = _fill_transcription_gaps(cached, audio_path)
        return cached

    # 调用 WhisperX 进行转录
    # WHISPERX_BIN 支持 str（单二进制）或 list（如 [python, wrapper.py]）
    print(f"[Step1] 开始转录音频: {audio_path}")
    bin_cmd = config.WHISPERX_BIN if isinstance(config.WHISPERX_BIN, list) \
              else [config.WHISPERX_BIN]
    cmd = bin_cmd + [audio_path,
        "--model", config.WHISPERX_MODEL,
        "--device", config.WHISPERX_DEVICE,
        "--compute_type", config.WHISPERX_COMPUTE_TYPE,
        "--batch_size", str(config.WHISPERX_BATCH_SIZE),
        "--language", config.WHISPERX_LANGUAGE,
        "--output_dir", output_dir,
        "--output_format", "json",
    ]

    # CPU 多线程加速：whisperx 默认只用 4 线程，多核机器显式指定线程数
    threads = getattr(config, "WHISPERX_THREADS", 0)
    if threads and threads > 0:
        cmd += ["--threads", str(threads)]

    # 说话人分离（Diarization）
    if getattr(config, "WHISPERX_DIARIZE", False):
        hf_token = getattr(config, "WHISPERX_HF_TOKEN", "")
        if hf_token:
            cmd += ["--diarize", "--hf_token", hf_token]
        else:
            # 尝试从环境变量读取
            import os as _os
            env_token = _os.environ.get("HF_TOKEN", "")
            if env_token:
                cmd += ["--diarize", "--hf_token", env_token]
            else:
                print("[Step1] ⚠️ 未设置 WHISPERX_HF_TOKEN，跳过说话人分离")

        min_s = getattr(config, "WHISPERX_MIN_SPEAKERS", 0)
        max_s = getattr(config, "WHISPERX_MAX_SPEAKERS", 0)
        if min_s > 0:
            cmd += ["--min_speakers", str(min_s)]
        if max_s > 0:
            cmd += ["--max_speakers", str(max_s)]

    print(f"[Step1] 执行命令: {' '.join(cmd)}")

    # 设置环境变量
    # - OMP_NUM_THREADS 等控制 PyTorch / CTranslate2 底层 OpenMP 线程
    # - PyTorch >= 2.6 默认 torch.load(weights_only=True)，
    #   pyannote VAD 模型需要 weights_only=False 才能加载
    env = os.environ.copy()
    env["PYTORCH_ENABLE_WEIGHTS_ONLY_LOAD"] = "0"
    threads = getattr(config, "WHISPERX_THREADS", 0)
    if threads and threads > 0:
        for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                     "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env[var] = str(threads)

    result = subprocess.run(cmd, capture_output=True, text=True, env=env)

    if result.returncode != 0:
        print(f"[Step1] WhisperX 转录失败:\n{result.stderr}")
        raise RuntimeError(f"WhisperX 转录失败 (code={result.returncode}): {result.stderr[-500:]}")

    print(f"[Step1] 转录完成，读取结果: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        transcription = json.load(f)

    # 检测并补录 VAD/对齐漏检的语音片段
    transcription = _fill_transcription_gaps(transcription, audio_path)
    # 补录结果落盘，避免下次读缓存时重复补录
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(transcription, f, ensure_ascii=False, indent=2)

    return transcription


def extract_full_text(transcription: dict) -> str:
    """
    从转录结果中提取完整的英文文本。

    Args:
        transcription: WhisperX 转录结果

    Returns:
        str: 完整的英文文本
    """
    segments = transcription.get("segments", [])
    return " ".join(seg["text"].strip() for seg in segments if seg.get("text"))


def extract_word_timestamps(transcription: dict) -> list:
    """
    从转录结果中提取词级别时间戳。

    Args:
        transcription: WhisperX 转录结果

    Returns:
        list[dict]: 每个元素包含 word, start, end, score
    """
    words = []
    for seg in transcription.get("segments", []):
        for w in seg.get("words", []):
            if "start" in w and "end" in w:
                words.append({
                    "word": w["word"],
                    "start": w["start"],
                    "end": w["end"],
                    "score": w.get("score", 0),
                })
    return words



if __name__ == "__main__":
    # 独立测试
    if len(sys.argv) < 2:
        print("用法: python -m pipeline.step1_transcribe <audio_file>")
        sys.exit(1)

    audio_file = sys.argv[1]
    data = transcribe(audio_file)
    text = extract_full_text(data)
    words = extract_word_timestamps(data)
    print(f"\n完整文本:\n{text}")
    print(f"\n总词数: {len(words)}")
    print(f"前10个词: {words[:10]}")
