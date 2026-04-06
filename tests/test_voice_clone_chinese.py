#!/usr/bin/env python3
"""
快速验证测试：用英文参考音频 + Qwen3-TTS 声音克隆合成中文
测试目的：验证"外国人说中文"的效果是否满足预期

测试内容：
1. 从已有音频中截取一段英文片段作为参考音频
2. 用这段英文参考音频，合成几个中文生词
3. 输出到 test_output/ 目录，供人工试听

用法：
    /root/miniconda3/envs/qwen3-tts/bin/python tests/test_voice_clone_chinese.py
"""
import json
import os
import sys
import time

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import soundfile as sf
import torch
from pydub import AudioSegment

from core import config

# ============ 配置 ============
# 原始音频（已有的测试音频）
AUDIO_PATH = os.path.join(config.DOWNLOAD_DIR, "605fd8e17c8b73a63cbb122cec457eed.mp3")
# 转录结果
TRANSCRIPT_PATH = os.path.join(config.OUTPUT_DIR, "605fd8e17c8b73a63cbb122cec457eed.json")
# Qwen3-TTS 模型
MODEL_PATH = "/root/Qwen3-TTS/models/Qwen/Qwen3-TTS-12Hz-1.7B-Base"
DEVICE = "cpu"
# 输出目录
OUTPUT_DIR = os.path.join(config.BASE_DIR, "test_output")

# 测试合成的中文内容（模拟生词释义场景）
# 格式: (合成文本, 文件名后缀, 说明)
TEST_CASES = [
    # Case 1: 纯中文词
    ("反派", "pure_cn_fanpai", "纯中文：反派"),
    ("得意的坏笑", "pure_cn_smirk", "纯中文：得意的坏笑"),
    ("协助犯罪", "pure_cn_aiding", "纯中文：协助犯罪"),
    ("绑架", "pure_cn_kidnap", "纯中文：绑架"),

    # Case 2: 英文词 + 中文释义（混合模式 - 核心验证点）
    ("villain，反派", "mix_villain", "混合：villain，反派"),
    ("smirk，得意的坏笑", "mix_smirk", "混合：smirk，得意的坏笑"),
    ("aiding and abetting，协助犯罪", "mix_aiding", "混合：aiding and abetting，协助犯罪"),
    ("kidnapped，绑架", "mix_kidnapped", "混合：kidnapped，绑架"),
    ("gorgeous，绚丽的", "mix_gorgeous", "混合：gorgeous，绚丽的"),

    # Case 3: 短句式释义
    ("villain的意思是反派", "sentence_villain", "短句：villain的意思是反派"),
    ("smirk的意思是得意的坏笑", "sentence_smirk", "短句：smirk的意思是得意的坏笑"),
]


def select_ref_segment(transcript_path: str):
    """从转录结果中选取一段较长的 segment 作为参考"""
    with open(transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments = data["segments"]

    # 按时长排序，选最长的
    seg_info = []
    for i, seg in enumerate(segments):
        duration = seg["end"] - seg["start"]
        seg_info.append((i, seg["start"], seg["end"], duration, seg["text"]))

    seg_info.sort(key=lambda x: x[3], reverse=True)

    # 打印前 5 个最长的 segment
    print("最长的 5 个 segment：")
    for idx, start, end, dur, text in seg_info[:5]:
        print(f"  [{idx}] {start:.1f}s-{end:.1f}s ({dur:.1f}s): {text[:60]}")

    # 选最长的
    best = seg_info[0]
    print(f"\n选取 segment[{best[0]}] 作为参考: {best[4][:80]}")
    return best[1], best[2]  # start, end


def extract_ref_audio(audio_path: str, start_s: float, end_s: float, output_path: str):
    """从原始音频中截取参考片段"""
    audio = AudioSegment.from_file(audio_path)
    start_ms = int(start_s * 1000)
    end_ms = int(end_s * 1000)
    clip = audio[start_ms:end_ms]
    clip.export(output_path, format="wav")
    print(f"参考音频: {start_s:.1f}s-{end_s:.1f}s -> {output_path} ({len(clip)/1000:.1f}s)")
    return output_path


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Step 1: 选取参考 segment
    print("=" * 60)
    print("Step 1: 选取参考音频片段")
    print("=" * 60)
    ref_start, ref_end = select_ref_segment(TRANSCRIPT_PATH)

    # Step 2: 截取参考音频
    ref_audio_path = os.path.join(OUTPUT_DIR, "ref_audio.wav")
    extract_ref_audio(AUDIO_PATH, ref_start, ref_end, ref_audio_path)

    # Step 3: 加载 Qwen3-TTS 模型
    print("\n" + "=" * 60)
    print("Step 2: 加载 Qwen3-TTS 模型")
    print("=" * 60)

    from qwen_tts import Qwen3TTSModel

    dtype = torch.float32 if DEVICE == "cpu" else torch.bfloat16
    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(
        MODEL_PATH,
        device_map=DEVICE,
        dtype=dtype,
    )
    print(f"模型加载完成 ({time.time() - t0:.1f}s)")

    # Step 4: 构建 voice clone prompt（只提取音色，不使用参考文本）
    print("\n" + "=" * 60)
    print("Step 3: 构建声音克隆 prompt (x_vector_only_mode)")
    print("=" * 60)

    prompt_items = model.create_voice_clone_prompt(
        ref_audio=ref_audio_path,
        ref_text=None,
        x_vector_only_mode=True,
    )
    print("voice clone prompt 构建完成")

    # Step 5: 逐个合成测试用例
    print("\n" + "=" * 60)
    print(f"Step 4: 合成 {len(TEST_CASES)} 个测试用例")
    print("=" * 60)

    results = []
    for i, (text, suffix, desc) in enumerate(TEST_CASES, 1):
        output_path = os.path.join(OUTPUT_DIR, f"test_{suffix}.wav")
        print(f"\n[{i}/{len(TEST_CASES)}] {desc}")
        print(f"  合成文本: \"{text}\"")

        try:
            t1 = time.time()
            # 估算 max_new_tokens
            n_chars = len(text)
            max_tokens = max(36, min(int((n_chars * 0.6 + 1.5) * 12), 360))

            wavs, sr = model.generate_voice_clone(
                text=text,
                language="Chinese",
                voice_clone_prompt=prompt_items,
                max_new_tokens=max_tokens,
                temperature=0.7,
                top_k=50,
                top_p=0.95,
                repetition_penalty=1.05,
            )

            wav_data = wavs[0]
            duration = len(wav_data) / sr
            elapsed = time.time() - t1

            sf.write(output_path, wav_data, sr)
            print(f"  -> {output_path} ({duration:.2f}s, 耗时 {elapsed:.1f}s)")

            results.append({
                "text": text,
                "desc": desc,
                "file": output_path,
                "duration": round(duration, 2),
                "elapsed": round(elapsed, 1),
            })

        except Exception as e:
            print(f"  -> 失败: {e}")
            results.append({
                "text": text,
                "desc": desc,
                "file": "",
                "error": str(e),
            })

    # Step 6: 总结
    print("\n" + "=" * 60)
    print("测试完成！输出文件列表：")
    print("=" * 60)
    print(f"\n参考音频（英文原声）:")
    print(f"  {ref_audio_path}")
    print(f"\n合成结果：")
    for r in results:
        if r.get("file"):
            print(f"  ✅ {r['desc']}")
            print(f"     文件: {r['file']} ({r['duration']}s)")
        else:
            print(f"  ❌ {r['desc']}: {r.get('error', '未知错误')}")

    print(f"\n请对比试听：")
    print(f"  1. 先听 ref_audio.wav（英文原声）感受说话人音色")
    print(f"  2. 再听 test_pure_cn_*.wav（纯中文），看音色是否一致")
    print(f"  3. 重点听 test_mix_*.wav（英文+中文混合），看是否有\"外国人说中文\"的感觉")
    print(f"  4. 对比 test_sentence_*.wav（短句式），看哪种格式最自然")

    # 保存结果 JSON
    result_file = os.path.join(OUTPUT_DIR, "test_results.json")
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {result_file}")


if __name__ == "__main__":
    main()
