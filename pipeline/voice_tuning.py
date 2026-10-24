"""按参考音色校准合成语音的音域。

Confucius TTS 复刻音色时会整体抬高基频，长时间收听显得尖锐。这里不改模型，
而在合成之后做一次按说话人恒定的变调：

* 目标基频取参考音频的中位基频
* 实际基频取该说话人若干合成单元汇总后的中位基频
* 比例限制在 [MIN_RATIO, MAX_RATIO]，只降不升

比例在同一说话人上恒定，单元内部语调不受影响；变调使用 ffmpeg 的
rubberband（保留共振峰、时长不变），并补偿其电平损失。
"""

from __future__ import annotations

import os
import shutil
import statistics
import subprocess

import numpy as np
import soundfile as sf

MIN_RATIO = 0.70
MAX_RATIO = 1.00
NO_OP_THRESHOLD = 0.97
SAMPLE_UNITS = 30
GAIN_DB = 3.2
F0_MIN = 70.0
F0_MAX = 380.0
MIN_VOICED_FRAMES = 8
MIN_POOLED_FRAMES = 120

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def f0_frames(path: str) -> list[float]:
    """逐帧基频（自相关法）。只返回可信的浊音帧。"""
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        return []
    if data.size == 0 or sr <= 0:
        return []
    x = data.mean(axis=1)
    x = x - x.mean()
    win = max(int(sr * 0.04), 64)
    hop = max(int(sr * 0.02), 32)
    lag_min = max(int(sr / F0_MAX), 2)
    lag_max = min(int(sr / F0_MIN), win - 2)
    if lag_max <= lag_min:
        return []
    values = []
    for start in range(0, len(x) - win, hop):
        frame = x[start:start + win]
        if float(np.sqrt(np.mean(frame ** 2))) < 0.01:
            continue
        frame = frame - frame.mean()
        auto = np.correlate(frame, frame, "full")[win - 1:]
        if auto[0] <= 0:
            continue
        window = auto[lag_min:lag_max]
        if window.size == 0:
            continue
        lag = int(np.argmax(window)) + lag_min
        if auto[lag] / auto[0] < 0.25:
            continue
        values.append(float(sr / lag))
    return values


def median_f0(path: str) -> float | None:
    """中位基频；可信浊音帧不足时返回 None。"""
    values = f0_frames(path)
    if len(values) < MIN_VOICED_FRAMES:
        return None
    return float(statistics.median(values))


def _shift(src: str, dst: str, ratio: float, gain_db: float) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    filters = f"rubberband=pitch={ratio:.4f}:formant=preserved,volume={gain_db}dB"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", src, "-af", filters, dst],
        check=True, capture_output=True,
    )


def tune_tts_map(tts_map: dict, segments: list, ref_map: dict | None,
                 subdir: str = "pitched") -> tuple[dict, dict]:
    """返回 (新 tts_map, 报告)。原始文件不动，结果写到同级 pitched/ 目录。"""
    ref_map = ref_map or {}
    report = {"enabled": True, "speakers": {}, "shifted": 0, "errors": []}

    by_speaker: dict[str, list[int]] = {}
    for index in sorted(tts_map):
        segment = segments[index] if 0 <= index < len(segments) else None
        speaker = (segment or {}).get("speaker") or (segment or {}).get("speaker_id")
        if speaker:
            by_speaker.setdefault(str(speaker), []).append(index)

    ratios: dict[str, float] = {}
    for speaker, indexes in by_speaker.items():
        reference = next((ref_map[i] for i in indexes if ref_map.get(i)), None)
        target = median_f0(reference) if reference else None
        pooled: list[float] = []
        for index in indexes[:SAMPLE_UNITS]:
            pooled.extend(f0_frames(tts_map[index]))
        if not target or len(pooled) < MIN_POOLED_FRAMES:
            report["speakers"][speaker] = {"skipped": "no_pitch_estimate"}
            continue
        synthesized = float(statistics.median(pooled))
        ratio = max(MIN_RATIO, min(MAX_RATIO, target / synthesized))
        report["speakers"][speaker] = {
            "reference_f0": round(target, 1),
            "synthesized_f0": round(synthesized, 1),
            "ratio": round(ratio, 3),
            "units": len(indexes),
        }
        if ratio >= NO_OP_THRESHOLD:
            report["speakers"][speaker]["skipped"] = "already_in_range"
            continue
        ratios[speaker] = ratio

    if not ratios:
        report["enabled"] = False
        return dict(tts_map), report

    tuned = dict(tts_map)
    for index, source in tts_map.items():
        segment = segments[index] if 0 <= index < len(segments) else None
        speaker = (segment or {}).get("speaker") or (segment or {}).get("speaker_id")
        ratio = ratios.get(str(speaker)) if speaker else None
        if not ratio:
            continue
        destination = os.path.join(os.path.dirname(source), subdir,
                                   os.path.basename(source))
        try:
            if not os.path.isfile(destination):
                _shift(source, destination, ratio, GAIN_DB)
            tuned[index] = destination
            report["shifted"] += 1
        except Exception as exc:  # 变调失败保留原音，不让整集失败
            report["errors"].append(f"{index}: {exc}")
    return tuned, report
