"""音高归一：基频估计与按说话人恒定比例的变调。"""

import math
import wave

import pytest

from pipeline import voice_tuning


def _write_tone(path, hz, seconds=3.0, rate=16000):
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"".join(
            int(12000 * math.sin(2 * math.pi * hz * i / rate)).to_bytes(2, "little", signed=True)
            for i in range(frames)
        ))


def test_median_f0_tracks_tone(tmp_path):
    path = tmp_path / "tone.wav"
    _write_tone(path, 150.0)
    value = voice_tuning.median_f0(str(path))
    assert value is not None
    assert 140.0 < value < 160.0


def test_tune_uses_one_ratio_per_speaker(monkeypatch, tmp_path):
    tts = {i: str(tmp_path / f"unit{i}.wav") for i in range(4)}
    _write_tone(tts[0], 170.0)
    _write_tone(tts[1], 170.0)
    _write_tone(tts[2], 130.0)
    _write_tone(tts[3], 130.0)
    segments = [{"speaker": "SPEAKER_00"}, {"speaker": "SPEAKER_00"},
                {"speaker": "SPEAKER_01"}, {"speaker": "SPEAKER_01"}]
    reference_a = tmp_path / "ref_a.wav"
    reference_b = tmp_path / "ref_b.wav"
    _write_tone(reference_a, 140.0)
    _write_tone(reference_b, 100.0)
    references = {0: str(reference_a), 2: str(reference_b)}

    shifted = []
    monkeypatch.setattr(voice_tuning, "_shift",
                        lambda src, dst, ratio, gain: shifted.append((src, ratio)))

    tuned, report = voice_tuning.tune_tts_map(tts, segments, references)

    assert report["shifted"] == 4
    assert len(shifted) == 4
    ratios = {}
    for source, ratio in shifted:
        index = int(source.rsplit("unit", 1)[1].split(".")[0])
        ratios.setdefault(segments[index]["speaker"], set()).add(round(ratio, 3))
    assert len(ratios["SPEAKER_00"]) == 1
    assert len(ratios["SPEAKER_01"]) == 1
    assert ratios["SPEAKER_00"] != ratios["SPEAKER_01"]
    assert all(voice_tuning.MIN_RATIO <= value <= voice_tuning.MAX_RATIO
               for _, value in shifted)
    assert all(tuned[i] != tts[i] for i in tts)


def test_tune_skips_when_already_in_range(monkeypatch, tmp_path):
    unit = str(tmp_path / "unit0.wav")
    _write_tone(unit, 140.0)
    reference = str(tmp_path / "ref.wav")
    _write_tone(reference, 139.0)
    monkeypatch.setattr(voice_tuning, "_shift",
                        lambda *args: pytest.fail("must not shift"))

    tuned, report = voice_tuning.tune_tts_map(
        {0: unit}, [{"speaker": "SPEAKER_00"}], {0: reference}
    )

    assert report["enabled"] is False
    assert tuned == {0: unit}
    assert report["speakers"]["SPEAKER_00"]["skipped"] == "already_in_range"
