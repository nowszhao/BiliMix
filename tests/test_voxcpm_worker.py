"""VoxCPM2 worker 单元测试：使用假模型，不加载权重、不依赖 GPU/MPS。"""
import json
from pathlib import Path

import numpy as np

from workers import voxcpm_worker


def _tone(seconds, sample_rate=16000):
    t = np.arange(int(seconds * sample_rate), dtype=np.float32) / sample_rate
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


class _FakeModel:
    def __init__(self, seconds=0.8):
        self.seconds = seconds
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs["text"])
        # 短答应短，长句按设定时长；模拟真实引擎的时长分布
        seconds = 0.9 if len(kwargs["text"]) <= 3 else self.seconds
        return _tone(seconds)


def _job(tmp_path, text):
    ref = tmp_path / "ref.wav"
    if not ref.exists():
        ref.write_bytes(b"fake-reference")
    return {"text": text, "ref_audio": str(ref), "lang": "zh",
            "output_path": str(tmp_path / "out.wav")}


def test_normal_utterance_writes_wav_and_engine_report(tmp_path):
    model = _FakeModel(2.9)
    job = _job(tmp_path, "今天我们要去公园放风筝玩得很开心啊")
    voxcpm_worker.synthesize_job(model, job, lambda seed: None, 16000,
                                 {"model": "openbmb/VoxCPM2"})
    assert Path(job["output_path"]).is_file()
    report = json.loads(Path(job["output_path"] + ".quality.json").read_text(encoding="utf-8"))
    assert report["engine"] == "voxcpm2"
    assert report["model"] == "openbmb/VoxCPM2"
    assert len(model.calls) >= 1


def test_short_line_draws_three_candidates(tmp_path):
    model = _FakeModel(0.9)
    job = _job(tmp_path, "好")
    voxcpm_worker.synthesize_job(model, job, lambda seed: None, 16000,
                                 {"model": "openbmb/VoxCPM2"})
    assert len(model.calls) == 3


def test_short_line_keeps_candidate_closest_to_natural_duration(tmp_path):
    class _VariableModel(_FakeModel):
        def generate(self, **kwargs):
            self.calls.append(kwargs["text"])
            # 0.9s 是自然短答；4.0s 更像训练数据泄漏
            return _tone(4.0 if len(self.calls) == 1 else 0.9)

    model = _VariableModel()
    job = _job(tmp_path, "嗯")
    voxcpm_worker.synthesize_job(model, job, lambda seed: None, 16000,
                                 {"model": "openbmb/VoxCPM2"})
    assert len(model.calls) == 3
    import soundfile as sf
    assert sf.info(job["output_path"]).duration < 1.5


def test_non_chinese_is_rejected(tmp_path):
    model = _FakeModel()
    job = _job(tmp_path, "Hello")
    job["lang"] = "en"
    try:
        voxcpm_worker.synthesize_job(model, job, lambda seed: None, 16000,
                                     {"model": "openbmb/VoxCPM2"})
    except ValueError as exc:
        assert "Chinese" in str(exc)
    else:
        raise AssertionError("non-Chinese job should be rejected")


def test_main_end_to_end_with_fake_engine(monkeypatch, tmp_path, capsys):
    """Exercise the real worker entry point without downloading weights."""
    import contextlib
    import io
    import sys
    import types

    class _FakeTTSModel:
        sample_rate = 16000

    class _FakeVoxCPM:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            model = _FakeModel(2.9)
            model.tts_model = _FakeTTSModel()
            return model

    fake_voxcpm = types.ModuleType("voxcpm")
    fake_voxcpm.VoxCPM = _FakeVoxCPM
    fake_torch = types.ModuleType("torch")
    fake_torch.manual_seed = lambda seed: None
    monkeypatch.setitem(sys.modules, "voxcpm", fake_voxcpm)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"fake-reference")
    task = {
        "model": "openbmb/VoxCPM2",
        "device": "cpu",
        "jobs": [
            {"text": "今天我们要去公园放风筝玩得很开心啊", "ref_audio": str(ref),
             "lang": "zh", "output_path": str(tmp_path / "a.wav")},
            {"text": "好", "ref_audio": str(ref), "lang": "zh",
             "output_path": str(tmp_path / "b.wav")},
        ],
    }
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["voxcpm_worker.py", str(task_file)])

    with contextlib.redirect_stderr(io.StringIO()):
        voxcpm_worker.main()
    out = capsys.readouterr().out.strip().splitlines()[-1]
    summary = json.loads(out)
    assert summary["success"] == 2
    assert summary["failed"] == 0
    for job in task["jobs"]:
        assert Path(job["output_path"]).is_file()
        report = json.loads(Path(job["output_path"] + ".quality.json").read_text(encoding="utf-8"))
        assert report["engine"] == "voxcpm2"
