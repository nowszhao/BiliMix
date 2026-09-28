"""
独立 TTS 合成（bmx tts / /api/tts/*）的测试。

不加载真实模型：synthesize_texts 被替换为「写出假 WAV + 上报进度」的桩函数，
只验证任务存储、路由契约、路径安全与终态流转。

覆盖：
  1. job_id 生成、文本/语种校验
  2. 任务存储生命周期、磁盘持久化、重启中断恢复
  3. /api/tts/* 路由契约与静态路由优先级（/jobs、/languages 不被 /<job_id> 吞掉）
  4. 参考音频路径必须在 data/ 内
"""
import json
import os
import shutil
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core import tts_jobs
from pipeline import tts_direct
from services.tts_api import tts_bp

TERMINAL = ("completed", "error", "cancelled")


@pytest.fixture()
def job_root(tmp_path, monkeypatch):
    """把 TTS 产物目录隔离到临时目录（保持 TTS_JOB_DIR 位于 RESULT_DIR 之下）"""
    result_root = str(tmp_path / "results")
    root = os.path.join(result_root, "tts")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(config, "RESULT_DIR", result_root)
    monkeypatch.setattr(config, "TTS_JOB_DIR", root)
    with tts_jobs.tts_jobs_lock:
        tts_jobs.tts_jobs.clear()
    tts_jobs.tts_cancel_flags.clear()
    yield root
    with tts_jobs.tts_jobs_lock:
        tts_jobs.tts_jobs.clear()
    tts_jobs.tts_cancel_flags.clear()


@pytest.fixture()
def ref_audio():
    """在服务端 downloads 目录里造一个（内容无关的）参考音频"""
    path = os.path.join(config.DOWNLOAD_DIR, f"_tts_test_ref_{os.getpid()}.wav")
    with open(path, "wb") as f:
        f.write(b"RIFF")
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture()
def client():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(tts_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _wait_terminal(client, job_id, timeout=10.0):
    """轮询直到任务进入终态"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/tts/{job_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        if data["status"] in TERMINAL:
            return data
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内进入终态")


def _fake_synthesize(texts, ref_audio, job_id, lang=tts_direct.DEFAULT_LANG,
                     cancel_check=None, progress_cb=None):
    """桩：为每条文本写一个假 WAV，并按真实实现上报进度"""
    job_dir = tts_direct.get_job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)
    outputs = []
    for i, text in enumerate(texts):
        path = os.path.join(job_dir, f"fake_{i}.wav")
        with open(path, "wb") as f:
            f.write(b"RIFF0000WAVE")
        outputs.append({"index": i, "text": text, "output_path": path})
        if progress_cb:
            progress_cb(i + 1, len(texts))
    return outputs


# ============================================================
# 1. job_id 与输入校验
# ============================================================

class TestTtsDirectValidation:

    def test_build_job_id_is_deterministic(self, ref_audio):
        a = tts_direct.build_job_id(ref_audio, ["你好", "世界"], "zh")
        b = tts_direct.build_job_id(ref_audio, ["你好", "世界"], "zh")
        c = tts_direct.build_job_id(ref_audio, ["你好", "世界"], "en")
        assert a == b, "相同输入应得到相同 job_id（可复用缓存）"
        assert a != c, "语种不同应视为不同任务"
        assert a.startswith("tts_")

    def test_validate_texts_strips_and_rejects_empty(self):
        assert tts_direct.validate_texts(["  你好  ", "", "  "]) == ["你好"]
        with pytest.raises(ValueError):
            tts_direct.validate_texts(["", "   "])
        with pytest.raises(ValueError):
            tts_direct.validate_texts("你好")

    def test_validate_texts_limits(self):
        with pytest.raises(ValueError):
            tts_direct.validate_texts([f"t{i}" for i in range(tts_direct.MAX_TEXTS + 1)])
        with pytest.raises(ValueError):
            tts_direct.validate_texts(["x" * (tts_direct.MAX_TEXT_CHARS + 1)])

    def test_validate_lang(self):
        assert tts_direct.validate_lang("ZH") == "zh"
        assert tts_direct.validate_lang(None) == "zh"
        with pytest.raises(ValueError):
            tts_direct.validate_lang("fr")


# ============================================================
# 2. 任务存储
# ============================================================

class TestTtsJobStore:

    def test_lifecycle_and_persistence(self, job_root):
        tts_jobs.create_job("tts_abc", texts=["你好"], ref_audio="/x/a.wav",
                            lang="zh", title="demo")
        job = tts_jobs.get_job("tts_abc")
        assert job["status"] == "queued"
        assert job["lang"] == "zh"

        # job.json 已落盘
        assert os.path.isfile(os.path.join(job_root, "tts_abc", "job.json"))

        tts_jobs.update_job("tts_abc", status="processing", progress=50,
                            message="合成中")
        assert tts_jobs.get_job("tts_abc")["progress"] == 50

        tts_jobs.update_job("tts_abc", status="completed", progress=100,
                            outputs=[{"index": 0, "text": "你好",
                                      "output_path": "/x/0.wav"}],
                            output_count=1)

        # 清空内存后仍能从磁盘读回终态
        with tts_jobs.tts_jobs_lock:
            tts_jobs.tts_jobs.clear()
        restored = tts_jobs.get_job("tts_abc")
        assert restored["status"] == "completed"
        assert restored["output_count"] == 1
        assert restored["finished_at"] > 0

        jobs = tts_jobs.list_jobs()
        assert [j["job_id"] for j in jobs] == ["tts_abc"]

    def test_interrupted_job_marked_error_on_reload(self, job_root):
        """重启前处于 processing 的任务，恢复时应标记为中断而非继续等待"""
        job_dir = os.path.join(job_root, "tts_stale")
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"job_id": "tts_stale", "status": "processing",
                       "progress": 42, "created_at": 1.0}, f)

        job = tts_jobs.get_job("tts_stale")
        assert job["status"] == "error"
        assert "中断" in job["message"]

    def test_delete_removes_files(self, job_root):
        tts_jobs.create_job("tts_del", texts=["你好"])
        job_dir = os.path.join(job_root, "tts_del")
        with open(os.path.join(job_dir, "fake.wav"), "wb") as f:
            f.write(b"RIFF")

        cleaned = tts_jobs.delete_job("tts_del")
        assert not os.path.isdir(job_dir)
        assert cleaned == ["data/results/tts/tts_del/"]
        assert tts_jobs.get_job("tts_del") == {}

    def test_cancel_flags(self, job_root):
        tts_jobs.create_job("tts_c")
        assert tts_jobs.is_cancelled("tts_c") is False
        assert tts_jobs.request_cancel("tts_c") is True
        assert tts_jobs.is_cancelled("tts_c") is True
        tts_jobs.clear_cancel("tts_c")
        assert tts_jobs.is_cancelled("tts_c") is False
        assert tts_jobs.request_cancel("no_such_job") is False


# ============================================================
# 3. API 契约
# ============================================================

class TestTtsApi:

    def test_synthesize_then_result(self, client, job_root, ref_audio,
                                    monkeypatch):
        monkeypatch.setattr("services.tts_api.synthesize_texts",
                            _fake_synthesize)

        resp = client.post("/api/tts/synthesize", json={
            "ref_audio": ref_audio,
            "texts": ["你好", "世界"],
            "lang": "zh",
            "title": "测试",
        })
        assert resp.status_code == 202, resp.get_json()
        payload = resp.get_json()
        job_id = payload["job_id"]
        assert payload["text_count"] == 2

        final = _wait_terminal(client, job_id)
        assert final["status"] == "completed"
        assert final["progress"] == 100
        assert final["output_count"] == 2

        result = client.get(f"/api/tts/{job_id}/result").get_json()
        assert result["output_count"] == 2
        assert len(result["outputs"]) == 2
        first = result["outputs"][0]
        assert first["text"] == "你好"
        assert first["audio_url"].startswith("/api/audio/tts/")
        assert result["audio_url"] == first["audio_url"]
        # 产物确实位于 RESULT_DIR 下，/api/audio 才能取到
        assert os.path.isfile(os.path.join(job_root, job_id, "fake_0.wav"))

    def test_synthesize_single_text_field(self, client, job_root, ref_audio,
                                          monkeypatch):
        monkeypatch.setattr("services.tts_api.synthesize_texts",
                            _fake_synthesize)
        resp = client.post("/api/tts/synthesize",
                           json={"ref_audio": ref_audio, "text": "单条文本"})
        assert resp.status_code == 202
        job_id = resp.get_json()["job_id"]
        _wait_terminal(client, job_id)

        result = client.get(f"/api/tts/{job_id}/result").get_json()
        assert result["texts"] == ["单条文本"]

    def test_resubmit_is_idempotent(self, client, job_root, ref_audio,
                                    monkeypatch):
        monkeypatch.setattr("services.tts_api.synthesize_texts",
                            _fake_synthesize)
        body = {"ref_audio": ref_audio, "texts": ["幂等"]}
        first = client.post("/api/tts/synthesize", json=body)
        job_id = first.get_json()["job_id"]
        _wait_terminal(client, job_id)

        second = client.post("/api/tts/synthesize", json=body)
        assert second.status_code == 200
        assert second.get_json()["reused"] is True
        assert second.get_json()["job_id"] == job_id

    def test_rejects_ref_outside_data_dir(self, client):
        resp = client.post("/api/tts/synthesize", json={
            "ref_audio": "/etc/hosts", "texts": ["你好"]})
        assert resp.status_code == 400
        assert "data/" in resp.get_json()["error"]

    def test_rejects_missing_ref_and_empty_text(self, client, ref_audio):
        assert client.post("/api/tts/synthesize",
                           json={"texts": ["你好"]}).status_code == 400
        assert client.post("/api/tts/synthesize",
                           json={"ref_audio": ref_audio,
                                 "texts": []}).status_code == 400
        assert client.post("/api/tts/synthesize",
                           json={"ref_audio": ref_audio,
                                 "text": "你好",
                                 "lang": "fr"}).status_code == 400

    def test_nonexistent_ref_file(self, client):
        ghost = os.path.join(config.DOWNLOAD_DIR, "_tts_ghost_不存在.wav")
        resp = client.post("/api/tts/synthesize",
                           json={"ref_audio": ghost, "texts": ["你好"]})
        assert resp.status_code == 400

    def test_static_routes_not_shadowed_by_job_id(self, client, job_root):
        """校验 Flask 路由优先级：/jobs 与 /languages 不被 /<job_id> 吞掉"""
        jobs = client.get("/api/tts/jobs").get_json()
        assert "jobs" in jobs

        langs = client.get("/api/tts/languages").get_json()
        assert "zh" in langs["languages"]

        # 不存在的 job_id 才走动态路由
        assert client.get("/api/tts/tts_nope").status_code == 404

    def test_output_outside_result_dir_has_no_url(self, client, tmp_path,
                                                  monkeypatch, ref_audio):
        """产物若不在 RESULT_DIR 下，不应生成带 ../ 的下载地址"""
        from services.tts_api import _tts_audio_url

        inside = os.path.join(config.RESULT_DIR, "a.wav")
        assert _tts_audio_url(inside) == "/api/audio/a.wav"

        outside = str(tmp_path / "elsewhere" / "b.wav")
        assert _tts_audio_url(outside) == ""
        assert _tts_audio_url("") == ""

    def test_list_and_delete(self, client, job_root, ref_audio, monkeypatch):
        monkeypatch.setattr("services.tts_api.synthesize_texts",
                            _fake_synthesize)
        job_id = client.post("/api/tts/synthesize", json={
            "ref_audio": ref_audio, "texts": ["待删除"]}).get_json()["job_id"]
        _wait_terminal(client, job_id)

        listed = client.get("/api/tts/jobs").get_json()["jobs"]
        assert job_id in [j["job_id"] for j in listed]

        by_status = client.get("/api/tts/jobs?status=completed").get_json()
        assert job_id in [j["job_id"] for j in by_status["jobs"]]
        assert client.get("/api/tts/jobs?status=error").get_json()["jobs"] == []

        resp = client.delete(f"/api/tts/{job_id}")
        assert resp.status_code == 200
        assert not os.path.isdir(os.path.join(job_root, job_id))
        assert client.get(f"/api/tts/{job_id}").status_code == 404
