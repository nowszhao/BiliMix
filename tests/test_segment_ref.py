"""
测试脚本：验证 segment 级别参考音频策略

对比新策略（每个 segment 独立参考音频）与旧策略（全篇共享参考音频）的效果差异。

新策略的核心改进：
1. 每个生词使用其所在句子的原声作为克隆参考 → 音色匹配说话人
2. 参考音频的语气、语速与目标句子一致 → 语调更自然

输出：test_output/<basename>_segref_test.mp3
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
    print("🧪 测试：Segment 级别参考音频策略")
    print(f"📁 音频: {audio_path}")
    print(f"📝 TTS 文本格式: {getattr(config, 'TTS_TEXT_FORMAT', 'mixed')}")
    print(f"📏 Segment 参考最小时长: {getattr(config, 'SEGMENT_REF_MIN_DURATION', 3.0)}s")
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

    # 打印每个替换点所在的 segment 信息
    print("\n[4] 替换点 segment 分布:")
    seg_count = {}
    for r in replacements:
        si = r["segment_index"]
        if si not in seg_count:
            seg_count[si] = []
        seg_count[si].append(r["english"])
    for si in sorted(seg_count.keys()):
        seg = segments[si]
        seg_text = seg.get("text", "")[:60]
        print(f"  seg[{si}] ({seg['start']:.1f}s-{seg['end']:.1f}s): "
              f"{', '.join(seg_count[si])} | \"{seg_text}\"")

    # 提取参考音频（新策略：segment 级别）
    print("\n[5] 提取参考音频（segment 级别）...")
    ref_dir = os.path.join(output_dir, "ref_audio_segref")
    ref_audio_map, ref_source_map = extract_ref_audio_for_segments(
        audio_path, segments, replacements, ref_dir)

    # 打印参考音频映射
    print("\n  参考音频映射:")
    unique_refs = set(ref_audio_map.values())
    print(f"  共 {len(unique_refs)} 个独立参考音频文件")
    for seg_idx, ref_path in sorted(ref_audio_map.items()):
        print(f"    seg[{seg_idx}] -> {os.path.basename(ref_path)}")

    # 相邻词分组
    print("\n[6] 相邻词分组...")
    adjacent_groups = group_adjacent_replacements(replacements)

    # TTS 合成（使用新的 segment 级别参考音频，缓存目录独立）
    print("\n[7] Qwen3-TTS 合成（segment 级别参考）...")
    tts_cache_dir = os.path.join(output_dir, "tts_cache_segref")
    start_time = time.time()

    tts_map = synthesize_with_qwen_tts(
        replacements, ref_audio_map, segments,
        tts_cache_dir,
        adjacent_groups=adjacent_groups,
        ref_source_map=ref_source_map,
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
    output_path = os.path.join(output_dir, f"{basename}_segref_test.{config.OUTPUT_FORMAT}")
    export_audio(mixed_audio, output_path)

    original_duration = len(original_audio) / 1000.0
    mixed_duration = len(mixed_audio) / 1000.0

    print("\n" + "=" * 60)
    print("✅ 测试完成!")
    print(f"🎧 原始音频: {original_duration:.1f}s")
    print(f"🎧 混合音频: {mixed_duration:.1f}s")
    print(f"📁 输出: {output_path}")
    print(f"🔄 替换: {len(replacements)} 处")
    print(f"🎤 独立参考音频: {len(unique_refs)} 个")
    print("=" * 60)
    print("\n💡 对比试听:")
    print(f"  旧版（共享参考）: test_output/{basename}_mixed_test.mp3")
    print(f"  新版（segment级参考）: test_output/{basename}_segref_test.mp3")


if __name__ == "__main__":
    main()
