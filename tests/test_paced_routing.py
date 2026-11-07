"""Verify the normal dispatch seam without contacting model services."""
from pathlib import Path

import pytest


@pytest.mark.parametrize("reference_mode,expected", [
    ("speaker", [True, False]), ("segment", [False]),
])
def test_language_and_reference_mode_choose_runtime(tmp_path, monkeypatch, reference_mode, expected):
    from core import config
    from pipeline import step3_tts_confucius as dispatch
    monkeypatch.setattr(config, "PACED_ZH_ROOT", str(tmp_path))
    runtimes = []
    monkeypatch.setattr(dispatch, "_runtime", lambda paced: runtimes.append(paced) or paced)
    def synthesize(runtime, segments, indices, *args):
        return {idx: str(tmp_path / f"{idx}.wav") for idx in indices}
    monkeypatch.setattr(dispatch, "_synthesize_batch", synthesize)
    result = dispatch.synthesize_sentences_with_confucius_tts(
        [], [0, 1], {0: "你好", 1: "hello"}, "", str(tmp_path),
        reference_mode=reference_mode, seg_lang_map={0: "zh", 1: "en"})
    assert runtimes == expected
    assert set(result) == {0, 1}


def test_paced_bundle_change_invalidates_completed_direct_job(tmp_path, monkeypatch):
    from core import config
    from pipeline.tts_direct import build_job_id
    monkeypatch.setattr(config, "PACED_ZH_ROOT", str(tmp_path))
    model = tmp_path / "melo" / "config.json"
    model.parent.mkdir()
    model.write_text("one")
    first = build_job_id(str(tmp_path / "ref.wav"), ["好的"], "zh")
    model.write_text("new model")
    assert first != build_job_id(str(tmp_path / "ref.wav"), ["好的"], "zh")
