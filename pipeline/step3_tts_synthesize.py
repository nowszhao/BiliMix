"""
Step 3: TTS 中文语音合成模块
将中文翻译文本合成为语音片段 (使用 edge-tts)

语音特性：
- 使用自然的语速和音高，避免机械感
- 通过 rate/pitch 参数调节韵律，让合成语音融入英文播客不突兀
"""
import asyncio
import hashlib
import os
import sys

import edge_tts
from pydub import AudioSegment

from core import config


def _text_hash(text: str) -> str:
    """生成文本的短哈希，用于缓存文件名（包含语音参数以区分不同配置）"""
    voice = getattr(config, "TTS_VOICE", "default")
    rate = getattr(config, "TTS_RATE", "+0%")
    pitch = getattr(config, "TTS_PITCH", "+0Hz")
    key = f"{text}|{voice}|{rate}|{pitch}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


async def _synthesize_one(text: str, output_path: str, voice: str = None,
                          rate: str = None, pitch: str = None):
    """
    异步合成单条中文文本为音频文件。

    Args:
        text: 中文文本
        output_path: 输出mp3文件路径
        voice: TTS 声音名称
        rate: 语速调整，如 "+10%", "-5%"
        pitch: 音高调整，如 "+5Hz", "-10Hz"
    """
    if voice is None:
        voice = config.TTS_VOICE
    if rate is None:
        rate = getattr(config, "TTS_RATE", "+0%")
    if pitch is None:
        pitch = getattr(config, "TTS_PITCH", "+0Hz")

    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await communicate.save(output_path)


def synthesize_text(text: str, cache_dir: str = None, voice: str = None) -> str:
    """
    合成单条中文文本为音频，支持缓存。

    Args:
        text: 中文文本
        cache_dir: 缓存目录
        voice: TTS 声音名称

    Returns:
        str: 生成的音频文件路径 (.mp3)
    """
    if cache_dir is None:
        cache_dir = config.TTS_CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)

    # 使用哈希作为缓存key（包含语音参数）
    h = _text_hash(text)
    cache_path = os.path.join(cache_dir, f"tts_{h}.mp3")

    if os.path.exists(cache_path):
        return cache_path

    print(f"  [TTS] 合成: {text}")
    asyncio.run(_synthesize_one(text, cache_path, voice))

    return cache_path


def synthesize_batch(texts: list, cache_dir: str = None, voice: str = None) -> list:
    """
    批量合成中文文本为音频。

    Args:
        texts: 中文文本列表
        cache_dir: 缓存目录
        voice: TTS 声音名称

    Returns:
        list[str]: 生成的音频文件路径列表
    """
    paths = []
    for i, text in enumerate(texts):
        print(f"[Step3] 合成进度 [{i+1}/{len(texts)}]")
        path = synthesize_text(text, cache_dir, voice)
        paths.append(path)
    return paths


def get_audio_duration(audio_path: str) -> float:
    """
    获取音频文件的时长（秒）。

    Args:
        audio_path: 音频文件路径

    Returns:
        float: 时长（秒）
    """
    audio = AudioSegment.from_file(audio_path)
    return len(audio) / 1000.0


if __name__ == "__main__":
    # 独立测试
    test_words = ["撤离", "争分夺秒", "闪闪发光", "北斗七星"]
    print("测试 TTS 合成:")
    for word in test_words:
        path = synthesize_text(word)
        duration = get_audio_duration(path)
        print(f"  '{word}' -> {path} ({duration:.2f}s)")
    print("测试完成!")
