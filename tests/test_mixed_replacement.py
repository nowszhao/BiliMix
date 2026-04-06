"""
端到端验证脚本：验证中英混合格式 TTS 替换效果

使用已有的转录数据和生词列表，跑完整的：
  Step 3: Qwen3-TTS 声音克隆合成（中英混合格式，如 "villain，反派"）
  Step 4: 音频替换拼接

最终输出混合音频供试听。
"""
import json
import os
import sys
import time

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from pipeline.step2_identify_difficult_words import locate_words_in_segments
from pipeline.step3_tts_qwen import (
    extract_ref_audio_for_segments,
    group_adjacent_replacements,
    synthesize_with_qwen_tts,
    build_tts_audio_map_for_replacements,
    _build_tts_text,
)
from pipeline.step4_audio_editor import load_audio, apply_replacements, export_audio


def main():
    # 已有的测试数据
    basename = "605fd8e17c8b73a63cbb122cec457eed"
    audio_path = os.path.join(config.DOWNLOAD_DIR, f"{basename}.mp3")
    transcription_path = os.path.join(config.OUTPUT_DIR, f"{basename}.json")
    difficult_words_path = os.path.join(config.RESULT_DIR, basename, "difficult_words.json")

    # 输出到 test_output 目录
    output_dir = os.path.join(config.BASE_DIR, "test_output")
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("🧪 端到端验证：中英混合格式 TTS 替换")
    print(f"📁 音频: {audio_path}")
    print(f"📝 TTS 文本格式: {getattr(config, 'TTS_TEXT_FORMAT', 'mixed')}")
    print("=" * 60)

    # 加载转录数据
    print("\n[1] 加载转录数据...")
    with open(transcription_path, "r", encoding="utf-8") as f:
        transcription = json.load(f)
    segments = transcription.get("segments", [])
    print(f"  segments: {len(segments)}")

    # 加载生词
    print("\n[2] 加载生词列表...")
    with open(difficult_words_path, "r", encoding="utf-8") as f:
        difficult_words = json.load(f)
    print(f"  生词: {len(difficult_words)} 个")

    # 定位时间戳
    print("\n[3] 定位生词时间戳...")
    replacements = locate_words_in_segments(difficult_words, segments)
    print(f"  匹配: {len(replacements)} 处")

    if not replacements:
        print("  没有匹配到任何替换点，退出")
        return

    # 打印合成文本预览
    print("\n[4] 合成文本预览（中英混合格式）:")
    for i, r in enumerate(replacements):
        text = _build_tts_text([i], replacements)
        print(f"  [{i}] {r['english']} @ {r['start']:.2f}s-{r['end']:.2f}s -> TTS: \"{text}\"")

    # 提取参考音频
    print("\n[5] 提取参考音频...")
    ref_dir = os.path.join(output_dir, "ref_audio")
    ref_audio_map = extract_ref_audio_for_segments(
        audio_path, segments, replacements, ref_dir)

    # 相邻词分组
    print("\n[6] 相邻词分组...")
    adjacent_groups = group_adjacent_replacements(replacements)

    # TTS 合成（使用新的混合格式缓存目录，避免与旧缓存冲突）
    print("\n[7] Qwen3-TTS 合成（中英混合格式）...")
    tts_cache_dir = os.path.join(output_dir, "tts_cache_mixed")
    start_time = time.time()

    tts_map = synthesize_with_qwen_tts(
        replacements, ref_audio_map, segments,
        tts_cache_dir,
        adjacent_groups=adjacent_groups,
    )

    elapsed = time.time() - start_time
    print(f"  TTS 合成完成: {len(tts_map)} 条, 耗时 {elapsed:.1f}s")

    # 构建 step4 需要的映射
    tts_index_map = build_tts_audio_map_for_replacements(
        replacements, tts_map, adjacent_groups)

    # 音频替换拼接
    print("\n[8] 音频替换拼接...")
    original_audio = load_audio(audio_path)
    mixed_audio, time_mapping = apply_replacements(
        original_audio, replacements, {}, tts_index_map)

    # 导出
    output_path = os.path.join(output_dir, f"{basename}_mixed_test.{config.OUTPUT_FORMAT}")
    export_audio(mixed_audio, output_path)

    original_duration = len(original_audio) / 1000.0
    mixed_duration = len(mixed_audio) / 1000.0

    print("\n" + "=" * 60)
    print("✅ 验证完成!")
    print(f"🎧 原始音频: {original_duration:.1f}s")
    print(f"🎧 混合音频: {mixed_duration:.1f}s")
    print(f"📁 输出: {output_path}")
    print(f"🔄 替换: {len(replacements)} 处")
    print("=" * 60)
    print("\n请听 test_output 目录下的混合音频，确认效果。")


if __name__ == "__main__":
    main()
