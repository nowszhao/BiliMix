#!/usr/bin/env python3
"""
Qwen3-TTS 声音克隆 Worker 脚本
在 qwen3-tts conda 环境中运行，通过 JSON 文件与主进程通信。

用法:
    /root/miniconda3/envs/qwen3-tts/bin/python qwen_tts_worker.py <task_json>

task_json 格式:
{
    "model_path": "/root/Qwen3-TTS/models/Qwen/Qwen3-TTS-12Hz-0.6B-Base",
    "device": "cpu",
    "language": "Chinese",
    "jobs": [
        {
            "text": "撤离",
            "ref_audio": "/path/to/ref_speaker0.wav",
            "ref_text": "The animals evacuated quickly from the island.",
            "output_path": "/path/to/output_tts.wav"
        },
        ...
    ]
}

输出: 将每个 job 的合成结果写入 output_path，并输出 JSON 结果到 stdout。
"""
import json
import os
import sys
import time

import numpy as np
import torch
import soundfile as sf


# =============================================
# 音频后处理：裁剪尾部静音和异常长音频
# =============================================
CODEC_HZ = 12  # Qwen3-TTS-12Hz: 每秒 12 个 codec token

# 每个汉字预估时长上限（秒），用于计算 max_new_tokens
SECONDS_PER_CHAR = 0.6
# 额外的缓冲时间（秒），防止裁得太紧
BUFFER_SECONDS = 1.5
# 绝对最短/最长 max_new_tokens
MIN_TOKENS = 36      # 至少 3 秒
MAX_TOKENS = 360     # 最多 30 秒（对短词短语足够）

# 尾部静音裁剪阈值（归一化振幅）
SILENCE_THRESHOLD = 0.01
# 尾部静音窗口大小（采样点数，24kHz 下 480 = 20ms）
SILENCE_WINDOW = 480
# 裁剪后最少保留时长（秒）
MIN_AUDIO_DURATION = 0.3
# 裁剪后最大保留时长（秒），超过则强制截断
MAX_AUDIO_DURATION = 15.0


def estimate_max_tokens(text: str) -> int:
    """
    根据输入文本长度估算合理的 max_new_tokens。
    短词短语不需要生成很长的音频。
    """
    n_chars = len(text)
    est_seconds = n_chars * SECONDS_PER_CHAR + BUFFER_SECONDS
    tokens = int(est_seconds * CODEC_HZ)
    return max(MIN_TOKENS, min(tokens, MAX_TOKENS))


def trim_trailing_silence(wav: np.ndarray, sr: int) -> np.ndarray:
    """
    裁剪音频尾部的静音部分。
    从末尾向前扫描，找到第一个超过阈值的位置，然后保留到该位置+少量余量。
    """
    if len(wav) < sr * MIN_AUDIO_DURATION:
        return wav

    # 计算每个窗口的 RMS
    window = min(SILENCE_WINDOW, len(wav) // 4)
    if window < 10:
        return wav

    # 从末尾往前找非静音点
    last_sound_idx = len(wav) - 1
    for i in range(len(wav) - window, 0, -window):
        chunk = wav[i:i + window]
        rms = np.sqrt(np.mean(chunk ** 2))
        if rms > SILENCE_THRESHOLD:
            last_sound_idx = i + window
            break
    else:
        # 整段都是静音，保留最小时长
        return wav[:int(sr * MIN_AUDIO_DURATION)]

    # 多保留一点余量（0.1 秒）
    end_idx = min(len(wav), last_sound_idx + int(sr * 0.1))
    return wav[:end_idx]


def postprocess_audio(wav: np.ndarray, sr: int, text: str) -> np.ndarray:
    """
    音频后处理：
    1. 裁剪尾部静音
    2. 限制最大时长（防止模型跑飞生成超长音频）
    """
    # 1. 强制限制最大时长
    max_samples = int(MAX_AUDIO_DURATION * sr)
    if len(wav) > max_samples:
        wav = wav[:max_samples]

    # 2. 裁剪尾部静音
    wav = trim_trailing_silence(wav, sr)

    # 3. 基于文本长度的合理时长上限
    n_chars = len(text)
    reasonable_max = max(MIN_AUDIO_DURATION, n_chars * SECONDS_PER_CHAR + BUFFER_SECONDS)
    reasonable_samples = int(reasonable_max * sr)
    if len(wav) > reasonable_samples:
        # 不直接硬截，先尝试在合理范围附近找一个静音点截断
        search_start = reasonable_samples
        search_end = min(len(wav), int(reasonable_samples * 1.5))
        best_cut = search_end
        window = SILENCE_WINDOW
        min_rms = float('inf')
        for i in range(search_start, search_end - window, window):
            chunk = wav[i:i + window]
            rms = np.sqrt(np.mean(chunk ** 2))
            if rms < min_rms:
                min_rms = rms
                best_cut = i
            if rms < SILENCE_THRESHOLD:
                best_cut = i
                break
        wav = wav[:best_cut]

    return wav


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "用法: python qwen_tts_worker.py <task_json>"}))
        sys.exit(1)

    task_file = sys.argv[1]
    if not os.path.exists(task_file):
        print(json.dumps({"error": f"任务文件不存在: {task_file}"}))
        sys.exit(1)

    with open(task_file, "r", encoding="utf-8") as f:
        task = json.load(f)

    model_path = task["model_path"]
    device = task.get("device", "cpu")
    language = task.get("language", "Chinese")
    jobs = task.get("jobs", [])
    icl_mode = task.get("icl_mode", False)

    if icl_mode:
        print(f"[QwenTTS] ⚡ ICL 模式：保留参考音频的语气和韵律", file=sys.stderr)

    if not jobs:
        print(json.dumps({"results": [], "message": "没有合成任务"}))
        return

    # 加载模型
    print(f"[QwenTTS] 加载模型: {model_path} (device={device})", file=sys.stderr)
    t0 = time.time()

    from qwen_tts import Qwen3TTSModel

    dtype = torch.float32 if device == "cpu" else torch.bfloat16
    model = Qwen3TTSModel.from_pretrained(
        model_path,
        device_map=device,
        dtype=dtype,
    )
    load_time = time.time() - t0
    print(f"[QwenTTS] 模型加载完成 ({load_time:.1f}s)", file=sys.stderr)

    # 按参考音频分组，相同参考音频的 job 共享 voice clone prompt
    ref_groups = {}
    for job in jobs:
        ref_key = job["ref_audio"]
        if ref_key not in ref_groups:
            ref_groups[ref_key] = {
                "ref_audio": job["ref_audio"],
                "ref_text": job.get("ref_text", ""),
                "jobs": [],
            }
        ref_groups[ref_key]["jobs"].append(job)

    results = []
    total_jobs = len(jobs)
    done_count = 0

    for ref_key, group in ref_groups.items():
        ref_audio = group["ref_audio"]
        ref_text = group["ref_text"]
        group_jobs = group["jobs"]

        print(f"[QwenTTS] 构建说话人 prompt: {os.path.basename(ref_audio)} "
              f"({len(group_jobs)} 个词)", file=sys.stderr)

        try:
            # 创建 voice clone prompt（可复用）
            # ICL 模式(x_vector_only_mode=False): 同时提取音色和韵律特征（语气、语速、情感）
            #   - 参考音频 + 参考文本告诉模型「这个人用这种语气说了这句话」
            #   - 适合需要保留原句情感语调的场景
            # x-vector 模式(x_vector_only_mode=True): 仅提取音色嵌入
            #   - 参考文本传 None，不受参考文本语言影响
            #   - 适合纯音色克隆，中文发音更稳定
            ref_text_for_prompt = ref_text if icl_mode else None
            use_x_vector_only = not icl_mode
            prompt_items = model.create_voice_clone_prompt(
                ref_audio=ref_audio,
                ref_text=ref_text_for_prompt,
                x_vector_only_mode=use_x_vector_only,
            )
            mode_label = "ICL(音色+语气)" if icl_mode else "x-vector(仅音色)"
            print(f"         prompt 模式: {mode_label}", file=sys.stderr)
        except Exception as e:
            print(f"[QwenTTS] 构建 prompt 失败: {e}", file=sys.stderr)
            for job in group_jobs:
                results.append({
                    "text": job["text"],
                    "output_path": "",
                    "error": str(e),
                })
                done_count += 1
            continue

        # 批量合成该说话人的所有词
        for job in group_jobs:
            text = job["text"]
            output_path = job["output_path"]
            done_count += 1

            # 动态计算 max_new_tokens
            max_tokens = estimate_max_tokens(text)
            print(f"[QwenTTS] [{done_count}/{total_jobs}] 合成: {text} "
                  f"(max_tokens={max_tokens})", file=sys.stderr)

            try:
                t1 = time.time()
                wavs, sr = model.generate_voice_clone(
                    text=text,
                    language=language,
                    voice_clone_prompt=prompt_items,
                    max_new_tokens=max_tokens,
                    temperature=0.7,
                    top_k=50,
                    top_p=0.95,
                    repetition_penalty=1.05,
                )

                # 后处理：裁剪静音和异常长度
                wav_data = postprocess_audio(wavs[0], sr, text)

                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                sf.write(output_path, wav_data, sr)
                elapsed = time.time() - t1

                raw_dur = len(wavs[0]) / sr
                final_dur = len(wav_data) / sr

                results.append({
                    "text": text,
                    "output_path": output_path,
                    "duration": round(final_dur, 2),
                    "raw_duration": round(raw_dur, 2),
                    "elapsed": round(elapsed, 1),
                })
                print(f"         -> {os.path.basename(output_path)} "
                      f"(raw={raw_dur:.1f}s, final={final_dur:.1f}s, "
                      f"耗时={elapsed:.1f}s)", file=sys.stderr)

            except Exception as e:
                print(f"[QwenTTS] 合成失败 '{text}': {e}", file=sys.stderr)
                results.append({
                    "text": text,
                    "output_path": "",
                    "error": str(e),
                })

    # 输出结果 JSON 到 stdout
    output = {
        "results": results,
        "total": total_jobs,
        "success": sum(1 for r in results if r.get("output_path")),
        "failed": sum(1 for r in results if r.get("error")),
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
