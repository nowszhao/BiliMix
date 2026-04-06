"""
Step 1: WhisperX 转录模块
将英文音频转录为带词级别时间戳的JSON文件
"""
import json
import os
import subprocess
import sys

from core import config


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
            return json.load(f)

    # 调用 WhisperX 进行转录（使用 conda 环境中的绝对路径）
    print(f"[Step1] 开始转录音频: {audio_path}")
    cmd = [
        config.WHISPERX_BIN, audio_path,
        "--model", config.WHISPERX_MODEL,
        "--device", config.WHISPERX_DEVICE,
        "--compute_type", config.WHISPERX_COMPUTE_TYPE,
        "--batch_size", str(config.WHISPERX_BATCH_SIZE),
        "--language", config.WHISPERX_LANGUAGE,
        "--output_dir", output_dir,
        "--output_format", "json",
    ]

    print(f"[Step1] 执行命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[Step1] WhisperX 转录失败:\n{result.stderr}")
        sys.exit(1)

    print(f"[Step1] 转录完成，读取结果: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


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
