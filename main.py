"""
英文播客生词替换工具 - 主流程
将英文播客中的生僻词汇和短语替换为中文语音

流水线:
  Step 1: WhisperX 转录 (音频 -> 词级别时间戳)
  Step 2: Ollama 大模型识别难词 + 翻译
  Step 3: Edge-TTS 合成中文语音
  Step 4: 音频编辑拼接，生成最终混合音频
  Step 5: 保存生词本供复习
"""
import json
import os
import sys
import time

# 确保项目根目录在 sys.path 中
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core import config
from pipeline.step1_transcribe import transcribe, extract_full_text, extract_word_timestamps
from pipeline.step2_identify_difficult_words import identify_difficult_words_by_segments, locate_words_in_segments
from pipeline.step3_tts_synthesize import synthesize_text, get_audio_duration
from pipeline.step4_audio_editor import (
    load_audio,
    apply_replacements,
    export_audio,
)


def save_vocabulary_book(difficult_words: list, replacements: list, output_path: str):
    """
    保存生词本，记录所有识别的难词/短语及其翻译，方便复习。

    Args:
        difficult_words: 大模型识别的完整难词列表
        replacements: 实际被替换的列表（含时间戳）
        output_path: 生词本输出路径
    """
    # 创建替换映射，用于标记哪些词被成功替换了
    replaced_set = {r["english"].lower() for r in replacements}

    book = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "difficulty_level": config.DIFFICULTY_LEVEL,
        "total_difficult_items": len(difficult_words),
        "replaced_items": len(replacements),
        "vocabulary": [],
    }

    for w in difficult_words:
        entry = {
            "english": w["english"],
            "chinese": w["chinese"],
            "type": w["type"],
            "replaced_in_audio": w["english"].lower() in replaced_set,
        }
        book["vocabulary"].append(entry)

    # 添加时间戳信息
    for rep in replacements:
        for entry in book["vocabulary"]:
            if entry["english"].lower() == rep["english"].lower():
                entry["audio_timestamp"] = {
                    "start": round(rep["start"], 2),
                    "end": round(rep["end"], 2),
                }
                break

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    print(f"[生词本] 已保存: {output_path}")

    # 同时输出易读的文本版本
    txt_path = output_path.replace(".json", ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"📚 生词本 (难度: {config.DIFFICULTY_LEVEL})\n")
        f.write(f"生成时间: {book['generated_at']}\n")
        f.write(f"{'='*50}\n\n")

        f.write("📝 生词:\n")
        for entry in book["vocabulary"]:
            if entry["type"] == "word":
                marker = "✅" if entry["replaced_in_audio"] else "❌"
                f.write(f"  {marker} {entry['english']} — {entry['chinese']}\n")

        f.write(f"\n📖 短语/习语:\n")
        for entry in book["vocabulary"]:
            if entry["type"] == "phrase":
                marker = "✅" if entry["replaced_in_audio"] else "❌"
                f.write(f"  {marker} {entry['english']} — {entry['chinese']}\n")

        f.write(f"\n{'='*50}\n")
        f.write(f"✅ = 已在音频中替换为中文  ❌ = 未能在音频中匹配到\n")

    print(f"[生词本] 文本版: {txt_path}")


def main(audio_path: str):
    """
    主流程入口。

    Args:
        audio_path: 输入音频文件路径
    """
    start_time = time.time()
    basename = os.path.splitext(os.path.basename(audio_path))[0]

    # 创建输出目录
    result_dir = os.path.join(config.RESULT_DIR, basename)
    os.makedirs(result_dir, exist_ok=True)

    print("=" * 60)
    print("🎧 英文播客生词替换工具")
    print(f"📁 输入: {audio_path}")
    print(f"📊 难度等级: {config.DIFFICULTY_LEVEL}")
    print("=" * 60)

    # ============================================
    # Step 1: WhisperX 转录
    # ============================================
    print("\n" + "=" * 40)
    print("📝 Step 1: 转录音频")
    print("=" * 40)
    transcription = transcribe(audio_path)
    full_text = extract_full_text(transcription)
    word_timestamps = extract_word_timestamps(transcription)
    print(f"[Step1] 转录完成: {len(word_timestamps)} 个词, 文本长度 {len(full_text)} 字符")

    # ============================================
    # Step 2: 大模型逐句识别难词
    # ============================================
    print("\n" + "=" * 40)
    print("🔍 Step 2: 逐句识别难词/短语")
    print("=" * 40)
    segments = transcription.get("segments", [])
    difficult_words = identify_difficult_words_by_segments(segments)

    if not difficult_words:
        print("[Step2] 未识别到难词，无需替换。流程结束。")
        return

    # 保存大模型识别结果
    llm_result_path = os.path.join(result_dir, "difficult_words.json")
    with open(llm_result_path, "w", encoding="utf-8") as f:
        json.dump(difficult_words, f, ensure_ascii=False, indent=2)

    # ============================================
    # Step 3: 定位时间戳 + TTS 合成
    # ============================================
    print("\n" + "=" * 40)
    print("🎯 Step 3: 定位时间戳 & 合成中文语音")
    print("=" * 40)

    # 3a. 在 segments.words 中定位难词的精确时间戳
    replacements = locate_words_in_segments(difficult_words, segments)
    print(f"\n[Step3] 共 {len(replacements)} 处需要替换")

    if not replacements:
        print("[Step3] 无匹配的替换项。可能是因为大模型识别的词在音频中未被精确匹配。")
        # 仍然保存生词本
        vocab_path = os.path.join(result_dir, "vocabulary_book.json")
        save_vocabulary_book(difficult_words, replacements, vocab_path)
        return

    # 3b. 对所有需要替换的中文文本进行 TTS 合成
    unique_chinese = list(set(r["chinese"] for r in replacements))
    print(f"[Step3] 需合成 {len(unique_chinese)} 条中文语音")

    tts_audio_map = {}
    for i, text in enumerate(unique_chinese):
        print(f"[Step3] TTS [{i+1}/{len(unique_chinese)}]", end=" ")
        path = synthesize_text(text)
        duration = get_audio_duration(path)
        tts_audio_map[text] = path
        print(f"  '{text}' -> {duration:.2f}s")

    # ============================================
    # Step 4: 音频拼接
    # ============================================
    print("\n" + "=" * 40)
    print("🔧 Step 4: 音频编辑替换")
    print("=" * 40)
    original_audio = load_audio(audio_path)
    mixed_audio, time_mapping = apply_replacements(original_audio, replacements, tts_audio_map)

    # 导出最终音频
    output_audio_path = os.path.join(result_dir, f"{basename}_mixed.{config.OUTPUT_FORMAT}")
    export_audio(mixed_audio, output_audio_path)

    # ============================================
    # Step 5: 保存生词本
    # ============================================
    print("\n" + "=" * 40)
    print("📚 Step 5: 生成生词本")
    print("=" * 40)
    vocab_path = os.path.join(result_dir, "vocabulary_book.json")
    save_vocabulary_book(difficult_words, replacements, vocab_path)

    # ============================================
    # 完成汇总
    # ============================================
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("✅ 处理完成!")
    print(f"⏱️  总耗时: {elapsed:.1f}s")
    print(f"🎧 混合音频: {output_audio_path}")
    print(f"📚 生词本: {vocab_path}")
    print(f"📝 识别难词: {len(difficult_words)} 个")
    print(f"🔄 实际替换: {len(replacements)} 处")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python main.py <音频文件路径>")
        print("示例: python main.py podcast.mp3")
        sys.exit(1)

    audio_file = sys.argv[1]
    if not os.path.exists(audio_file):
        print(f"错误: 文件不存在 - {audio_file}")
        sys.exit(1)

    main(audio_file)
