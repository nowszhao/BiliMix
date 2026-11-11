"""TTS 后端分发与缓存身份：默认仍为 Confucius，可切换到 VoxCPM2。"""
import sys
from pathlib import Path

from core import config
from pipeline import step3_tts_confucius as dispatch
from pipeline import tts_direct
from pipeline.tts_cache import voxcpm_model_fingerprint


def test_default_runtime_is_confucius(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENGINE", "confucius-tts")
    monkeypatch.setattr(dispatch, "model_fingerprint", lambda *a: "confucius-fp")
    runtime = dispatch._runtime()
    assert runtime.script.endswith("confucius_tts_worker.py")
    assert runtime.cache_prefix == "confucius_tts"
    assert runtime.model_id == "confucius-fp"


def test_voxcpm_runtime_is_selected(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENGINE", "voxcpm")
    monkeypatch.setattr(config, "VOXCPM_PYTHON", sys.executable)
    monkeypatch.setattr(config, "VOXCPM_MODEL", "openbmb/VoxCPM2")
    monkeypatch.setattr(config, "VOXCPM_NUM_WORKERS", 2)
    runtime = dispatch._runtime()
    assert runtime.script.endswith("voxcpm_worker.py")
    assert runtime.python == sys.executable
    assert runtime.cache_prefix == "voxcpm2"
    assert runtime.num_workers == 2
    assert runtime.options["model"] == "openbmb/VoxCPM2"
    assert runtime.options["device"] == "cpu"
    assert runtime.env["BILIMIX_VOXCPM_MODEL"] == "openbmb/VoxCPM2"


def test_direct_job_id_changes_with_engine(monkeypatch, tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"reference-bytes")
    monkeypatch.setattr(config, "TTS_ENGINE", "confucius-tts")
    confucius_id = tts_direct.build_job_id(str(ref), ["你好"], "zh")
    monkeypatch.setattr(config, "TTS_ENGINE", "voxcpm")
    monkeypatch.setattr(config, "VOXCPM_MODEL", "openbmb/VoxCPM2")
    monkeypatch.setattr(config, "VOXCPM_PYTHON", sys.executable)
    vox_id = tts_direct.build_job_id(str(ref), ["你好"], "zh")
    assert confucius_id != vox_id


def test_voxcpm_fingerprint_tracks_worker_and_model(tmp_path):
    worker = tmp_path / "voxcpm_worker.py"
    worker.write_text("first version\n", encoding="utf-8")
    first = voxcpm_model_fingerprint(worker, "openbmb/VoxCPM2")
    worker.write_text("second version, longer content\n", encoding="utf-8")
    second = voxcpm_model_fingerprint(worker, "openbmb/VoxCPM2")
    assert first != second
    assert first != voxcpm_model_fingerprint(worker, "another/model")
