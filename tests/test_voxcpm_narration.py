"""VoxCPM2 语速策略：默认 Confucius 阈值会把 6 字/秒判成异常并重试。"""
import numpy as np

from pipeline.tts_narration import synthesize_narration

TEXT = "今天我们要去公园放风筝玩得很开心啊"
SR = 16000


def _tone(seconds):
    t = np.arange(int(seconds * SR), dtype=np.float32) / SR
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _generate(_chunk, _seed, _limit):
    # 17 汉字 / 2.9 秒 ≈ 5.9 字每秒，符合 VoxCPM2 实测语速
    return _tone(2.9)


def test_default_profile_retries_fast_voxcpm_output():
    audio, report = synthesize_narration(TEXT, "zh", "ref-hash", _generate, SR)
    attempts = report["chunks"][0]["attempts"]
    assert len(attempts) > 1
    assert audio.size > 0


def test_voxcpm_rate_profile_accepts_native_speed():
    audio, report = synthesize_narration(
        TEXT, "zh", "ref-hash", _generate, SR,
        rate_profile=(5.8, (4.2, 7.0)))
    attempts = report["chunks"][0]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["chars_per_second"] > 5.2
    assert report["chunks"][0]["warnings"] == []
    assert audio.size > 0
