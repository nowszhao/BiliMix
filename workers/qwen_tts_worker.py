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

# 每个汉字预估时长（秒）。
# 中文语速因说话人/情感/句型差异显著，偏快约 0.22s/字，偏慢约 0.40s/字。
# 原值 0.30 对带停顿或感叹语气的句子会截掉末尾字，调整为 0.38 给更多余量。
SECONDS_PER_CHAR = 0.38
# 额外的缓冲时间（秒），防止裁得太紧。
# 原值 1.0s 对长句已足够，但对 3-5 字的短句相对有限，调整为 1.5s。
BUFFER_SECONDS = 1.5
# 绝对最短/最长 max_new_tokens
MIN_TOKENS = 36      # 至少 3 秒
MAX_TOKENS = 360     # 最多 30 秒

# 尾部静音裁剪阈值（归一化振幅）。
# 原值 0.01 会把中文语气词/韵母尾部的自然衰减（"了、吧、呢、啊"）当静音截掉。
# 调低到 0.005，只裁真正接近无声的部分。
SILENCE_THRESHOLD = 0.005
# 尾部静音窗口大小（采样点数，24kHz 下 480 = 20ms）
SILENCE_WINDOW = 480
# 裁剪后最少保留时长（秒）
MIN_AUDIO_DURATION = 0.3
# 裁剪后最大保留时长（秒），超过则强制截断
MAX_AUDIO_DURATION = 30.0
# 尾部静音裁剪后额外保留的余量（秒）。
# 原值 0.1s 太短，语气词尾音容易被刚好截掉，调整为 0.25s。
TAIL_KEEP_SECONDS = 0.25

# 音频质量校验阈值
# 最短有效时长（秒），低于此值视为合成失败
VALIDATION_MIN_DURATION = 0.25
# 最低 RMS 振幅，低于此值视为静音/失败
VALIDATION_MIN_RMS = 0.003


def estimate_max_tokens(text: str, target_duration_s: float = None) -> int:
    """
    根据输入文本长度估算合理的 max_new_tokens。

    如果提供了 target_duration_s（来自原始英文 segment 的时长），
    则以它为基准，让 TTS 合成的中文语音尽量接近原始语速节奏。

    Args:
        text: 待合成的中文文本
        target_duration_s: 原始英文 segment 的目标时长（秒），用于节奏匹配
    """
    n_chars = len(text)
    est_seconds = n_chars * SECONDS_PER_CHAR + BUFFER_SECONDS

    if target_duration_s and target_duration_s > 0:
        # 在文本估算和原始时长之间取加权平衡
        # 中文正常语速下不会比原始英文长太多，也不会奇短
        est_seconds = min(est_seconds, target_duration_s * 1.3)
        est_seconds = max(est_seconds, target_duration_s * 0.5)

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

    # 多保留一点余量（TAIL_KEEP_SECONDS，让语气词尾音自然衰减完毕）
    end_idx = min(len(wav), last_sound_idx + int(sr * TAIL_KEEP_SECONDS))
    return wav[:end_idx]


def validate_audio(wav: np.ndarray, sr: int, text: str) -> tuple:
    """
    校验合成音频质量，检测乱码/静音/过短等异常。

    Returns:
        (is_valid: bool, reason: str)
    """
    dur = len(wav) / sr
    rms = float(np.sqrt(np.mean(wav ** 2)))

    # 1. 时长检查
    if dur < VALIDATION_MIN_DURATION:
        return False, f"音频过短 ({dur:.2f}s < {VALIDATION_MIN_DURATION}s)"

    # 2. 振幅检查：静音或极低音量 = 合成失败
    if rms < VALIDATION_MIN_RMS:
        return False, f"音频振幅过低 (RMS={rms:.5f})"

    # 3. 异常高频能量检查（乱码通常伴随大量高频噪声）
    if len(wav) > sr * 0.5:  # 至少 0.5s 才做频谱分析
        try:
            from scipy import signal
            f, psd = signal.welch(wav, sr, nperseg=min(2048, len(wav)))
            # 计算 4kHz-8kHz 频段能量占比（中文语音主要能量在 4kHz 以下）
            low_mask = (f >= 80) & (f <= 4000)
            high_mask = (f > 4000) & (f <= 8000)
            low_energy = np.sum(psd[low_mask])
            high_energy = np.sum(psd[high_mask])
            if low_energy > 0:
                high_ratio = high_energy / low_energy
                if high_ratio > 1.5:
                    return False, f"异常高频能量 (ratio={high_ratio:.2f})"
        except ImportError:
            pass  # scipy 不可用时跳过频谱检查

    return True, ""


def postprocess_audio(wav: np.ndarray, sr: int, text: str) -> np.ndarray:
    """
    音频后处理：
    1. 裁剪尾部静音
    2. 限制最大时长（防止模型跑飞生成超长音频）

    句末保护：末尾 2 个字的估算时长不计入文本长度裁剪上限，
    避免语气词/尾音（"了、吧、呢、啊"）被误截。
    """
    # 1. 强制限制最大时长
    max_samples = int(MAX_AUDIO_DURATION * sr)
    if len(wav) > max_samples:
        wav = wav[:max_samples]

    # 2. 裁剪尾部静音
    wav = trim_trailing_silence(wav, sr)

    # 3. 基于文本长度的合理时长上限（含句末保护）
    n_chars = len(text)
    # 末尾 2 个字额外豁免，防止语气词尾音被截
    TAIL_PROTECT_CHARS = 2
    protected_extra = TAIL_PROTECT_CHARS * SECONDS_PER_CHAR
    reasonable_max = max(MIN_AUDIO_DURATION,
                         n_chars * SECONDS_PER_CHAR + BUFFER_SECONDS + protected_extra)
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
            target_dur = job.get("target_duration_s")  # 原始英文 segment 时长
            done_count += 1

            # 动态计算 max_new_tokens（基于原始语速匹配）
            max_tokens = estimate_max_tokens(text, target_dur)
            print(f"[QwenTTS] [{done_count}/{total_jobs}] 合成: {text} "
                  f"(max_tokens={max_tokens}"
                  + (f", target={target_dur:.1f}s" if target_dur else "")
                  + ")", file=sys.stderr)

            try:
                t1 = time.time()
                wavs, sr = model.generate_voice_clone(
                    text=text,
                    language=language,
                    voice_clone_prompt=prompt_items,
                    max_new_tokens=max_tokens,
                    temperature=0.3,
                    top_k=20,
                    top_p=0.85,
                    repetition_penalty=1.3,
                )

                # 质量校验
                is_valid, val_reason = validate_audio(wavs[0], sr, text)
                if not is_valid:
                    print(f"[QwenTTS] 质量校验失败 '{text}': {val_reason}", file=sys.stderr)
                    results.append({
                        "text": text,
                        "output_path": "",
                        "error": f"质量校验: {val_reason}",
                    })
                    continue

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
