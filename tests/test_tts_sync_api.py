"""同步合成接口（POST /api/tts/sync）与逐词对齐的契约测试。

不加载任何模型：合成被替换为「写真实 PCM16 WAV 的桩函数」，逐词分析被替换为
固定报告。覆盖请求解析、产物与报告落盘、缓存命中、超时降级和输入校验。
"""
import json
import math
import os
import struct
import sys
import time
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core import tts_jobs
from pipeline import tts_direct, tts_words
from services.tts_api import tts_bp

TERMINAL = ("completed", "error", "cancelled")


def _write_wav(path, seconds=1.0, sample_rate=8000, amplitude=0.2):
    """写一段真实可探测的 PCM16 单声道 WAV（不依赖 ffmpeg 生成）"""
    with wave.open(path, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        frames = bytearray()
        for index in range(int(seconds * sample_rate)):
            value = int(amplitude * 32767 * math.sin(2 * math.pi * 220 * index / sample_rate))
            frames += struct.pack("<h", value)
        stream.writeframes(bytes(frames))
    return path


@pytest.fixture()
def job_root(tmp_path, monkeypatch):
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
def client():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(tts_bp)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def ref_wav(tmp_path):
    return _write_wav(str(tmp_path / "ref.wav"), seconds=3.0)


@pytest.fixture()
def server_ref(ref_wav):
    """服务端 data/ 目录内的一份参考音频（JSON 形态使用）"""
    path = os.path.join(config.DOWNLOAD_DIR, f"_sync_ref_{os.getpid()}.wav")
    with open(ref_wav, "rb") as source, open(path, "wb") as target:
        target.write(source.read())
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def _fake_synthesize(texts, ref_audio, job_id, lang="zh", cancel_check=None,
                     progress_cb=None, ref_text="", style="", cfg_value=None,
                     pace="normal", tempo=1.0, allowed_langs=None):
    job_dir = tts_direct.get_job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)
    outputs = []
    for index, text in enumerate(texts):
        path = _write_wav(os.path.join(job_dir, f"fake_{index}.wav"), seconds=1.0)
        outputs.append({"index": index, "text": text, "output_path": path,
                        "ref_text": ref_text, "style": style})
        if progress_cb:
            progress_cb(index + 1, len(texts))
    return outputs


def _fake_analyze_words(audio_path, text, language="zh", work_dir=None):
    return {
        "version": 1, "unit": "char", "source": "asr_align", "language": language,
        "text": text, "asr_text": text, "coverage": 1.0,
        "items": [{"index": 0, "text": text[:1], "char_start": 0, "char_end": 1,
                   "start": 0.0, "end": 0.2, "score": 0.91}],
        "warnings": [], "duration": 1.0,
    }


@pytest.fixture()
def stubs(monkeypatch):
    # 参考转写续写克隆只在 VoxCPM2 后端可用；不受本机 settings.json 影响
    monkeypatch.setattr(config, "TTS_ENGINE", "voxcpm")
    monkeypatch.setattr("services.tts_api.synthesize_texts", _fake_synthesize)
    monkeypatch.setattr(tts_words, "analyze_words", _fake_analyze_words)
    return monkeypatch


def _multipart(client, ref_wav, **fields):
    with open(ref_wav, "rb") as handle:
        data = dict(fields)
        data["ref_audio"] = (handle, os.path.basename(ref_wav))
        return client.post("/api/tts/sync", data=data,
                           content_type="multipart/form-data")


def _wait_terminal(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/tts/{job_id}").get_json()
        if payload["status"] in TERMINAL:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内结束")


class TestSyncSynthesis:

    def test_multipart_returns_audio(self, client, job_root, ref_wav, stubs):
        response = _multipart(client, ref_wav, text="你好，世界。")
        assert response.status_code == 200, response.get_json()
        payload = response.get_json()
        assert payload["status"] == "completed"
        assert payload["mode"] == "sync"
        assert payload["words"] is None
        assert payload["audio"]["url"].startswith("/api/audio/tts/")
        assert payload["audio"]["filename"] == "speech.wav"
        assert payload["audio"]["duration"] > 0
        assert payload["audio"]["sample_rate"] == 8000
        assert payload["audio"]["sha256"]
        assert payload["reference"]["clone_mode"] == "reference"
        assert payload["cache"]["hit"] is False

        job_id = payload["job_id"]
        assert os.path.isfile(os.path.join(job_root, job_id, "speech.wav"))
        result = client.get(f"/api/tts/{job_id}/result").get_json()
        assert result["mode"] == "sync"
        assert result["outputs"][0]["audio_url"] == payload["audio"]["url"]

    def test_include_words_writes_report(self, client, job_root, ref_wav, stubs):
        response = _multipart(client, ref_wav, text="你好，世界。",
                              include_words="true",
                              ref_text="这是一段参考。")
        assert response.status_code == 200, response.get_json()
        payload = response.get_json()
        assert payload["reference"]["clone_mode"] == "prompt+reference"

        words = payload["words"]
        assert words and words["coverage"] == 1.0
        assert words["unit"] == "char"
        assert words["items"][0]["text"] == "你"
        assert payload["words_url"].endswith("words_0000.json")
        assert os.path.isfile(words["path"])
        with open(words["path"], encoding="utf-8") as stream:
            assert json.load(stream)["text"] == "你好，世界。"

        result = client.get(f"/api/tts/{payload['job_id']}/result").get_json()
        assert result["include_words"] is True
        assert result["outputs"][0]["words"]["index"] == 0
        assert result["words"][0]["asr_text"] == "你好，世界。"

    def test_second_identical_request_hits_cache(self, client, job_root, ref_wav,
                                                 stubs):
        calls = []
        original = _fake_synthesize

        def counting(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        stubs.setattr("services.tts_api.synthesize_texts", counting)

        first = _multipart(client, ref_wav, text="缓存测试").get_json()
        second = _multipart(client, ref_wav, text="缓存测试").get_json()
        assert first["job_id"] == second["job_id"]
        assert second["cache"]["hit"] is True
        assert len(calls) == 1

    def test_pace_and_tempo_propagate(self, client, job_root, ref_wav, stubs):
        seen = {}
        original = _fake_synthesize

        def recording(texts, ref_audio, job_id, **kwargs):
            seen.update(kwargs)
            return original(texts, ref_audio, job_id, **kwargs)

        stubs.setattr("services.tts_api.synthesize_texts", recording)
        payload = _multipart(client, ref_wav, text="Learning English is easier.",
                             language="en", pace="word", tempo="0.8").get_json()
        assert payload["status"] == "completed"
        assert payload["pace"] == "word"
        assert payload["tempo"] == 0.8
        assert seen["pace"] == "word" and seen["tempo"] == 0.8
        assert seen["allowed_langs"] == ["zh", "en"], "同步接口显式放开中英"

    def test_style_is_echoed_and_sanitized(self, client, job_root, ref_wav, stubs):
        payload = _multipart(client, ref_wav, text="语气测试",
                             style="语速稍快，语气兴奋。").get_json()
        assert payload["style"] == "语速稍快 语气兴奋", "标点必须被替换，避免被长句规划切断"
        assert payload["style_applied"] is True
        assert payload["style_warning"] is None

        plain = _multipart(client, ref_wav, text="语气测试").get_json()
        assert plain["style"] is None
        assert plain["job_id"] != payload["job_id"], "风格指令必须改变任务身份"

    def test_style_falls_back_when_instruction_is_read_aloud(self, client, job_root,
                                                             ref_wav, stubs):
        """引擎把指令当正文念出时必须丢弃该音频，改用无指令重合成。"""
        calls = []
        original = _fake_synthesize

        def recording(texts, ref_audio, job_id, **kwargs):
            calls.append(kwargs.get("style", ""))
            return original(texts, ref_audio, job_id, **kwargs)

        stubs.setattr("services.tts_api.synthesize_texts", recording)
        stubs.setattr(
            tts_words, "analyze_words",
            lambda audio_path, text, language="zh", work_dir=None: {
                "version": 1, "unit": "char", "source": "asr_align",
                "language": language, "text": text,
                "asr_text": "语气亲切语速适中" + text,
                "coverage": 1.0, "items": [], "warnings": [], "duration": 1.0})

        payload = _multipart(client, ref_wav, text="一格格英语每天一句。",
                             style="语气亲切 语速适中").get_json()
        assert payload["status"] == "completed"
        assert payload["style_applied"] is False
        assert payload["style_warning"] == "instruction_spoken_by_engine"
        assert calls == ["语气亲切 语速适中", ""], "失败后必须无指令重合成"
        assert payload["audio"]["url"].endswith("speech.wav")

    def test_fallback_recomputes_words_from_clean_audio(self, client, job_root,
                                                        ref_wav, stubs):
        """回退后逐词必须来自重合成后的音频，不能沿用被污染的识别结果。"""
        seen = []

        def staged(audio_path, text, language="zh", work_dir=None):
            seen.append(audio_path)
            asr = ("语气亲切" if len(seen) == 1 else "") + text
            return {"version": 1, "unit": "char", "source": "asr_align",
                    "language": language, "text": text, "asr_text": asr,
                    "coverage": 1.0, "items": [], "warnings": [], "duration": 1.0}

        stubs.setattr(tts_words, "analyze_words", staged)
        payload = _multipart(client, ref_wav, text="逐词回退", style="语气亲切",
                             include_words="true").get_json()
        assert payload["style_applied"] is False
        assert payload["words"]["asr_text"] == "逐词回退"
        assert len(seen) == 2, "回退后必须重新回读"

    def test_style_falls_back_when_verification_unavailable(self, client, job_root,
                                                            ref_wav, stubs):
        calls = []
        original = _fake_synthesize

        def recording(texts, ref_audio, job_id, **kwargs):
            calls.append(kwargs.get("style", ""))
            return original(texts, ref_audio, job_id, **kwargs)

        def broken(*args, **kwargs):
            raise RuntimeError("whisperx missing")

        stubs.setattr("services.tts_api.synthesize_texts", recording)
        stubs.setattr(tts_words, "analyze_words", broken)

        payload = _multipart(client, ref_wav, text="校验不可用",
                             style="语气温柔").get_json()
        assert payload["style_applied"] is False
        assert payload["style_warning"] == "verification_unavailable"
        assert calls == ["语气温柔", ""]

    def test_style_raises_guidance_strength(self, client, job_root, ref_wav, stubs):
        """默认 cfg 2.0 下指令几乎无效；带 style 时必须提高到 STYLE_CFG_VALUE。"""
        seen = []
        original = _fake_synthesize

        def recording(texts, ref_audio, job_id, **kwargs):
            seen.append(kwargs.get("cfg_value"))
            return original(texts, ref_audio, job_id, **kwargs)

        stubs.setattr("services.tts_api.synthesize_texts", recording)
        _multipart(client, ref_wav, text="引导强度", style="语气温柔")
        _multipart(client, ref_wav, text="引导强度")
        assert seen == [2.8, None]

    def test_rejects_invalid_pace_and_tempo(self, client, job_root, ref_wav, stubs):
        response = _multipart(client, ref_wav, text="节奏", pace="bogus")
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_pace"

        response = _multipart(client, ref_wav, text="节奏", tempo="0.8")
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_tempo"

        response = _multipart(client, ref_wav, text="节奏", pace="slow", tempo="0.4")
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_tempo"

    def test_style_rejects_too_long_and_wrong_engine(self, client, job_root,
                                                     ref_wav, stubs):
        response = _multipart(client, ref_wav, text="太长", style="语" * 41)
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_style"

        stubs.setattr(config, "TTS_ENGINE", "confucius-tts")
        response = _multipart(client, ref_wav, text="引擎", style="语气温柔")
        assert response.status_code == 400
        assert response.get_json()["code"] == "unsupported_style"

    def test_words_flag_changes_job_identity(self, client, job_root, ref_wav, stubs):
        plain = _multipart(client, ref_wav, text="身份").get_json()
        with_words = _multipart(client, ref_wav, text="身份",
                                include_words="true").get_json()
        assert plain["job_id"] != with_words["job_id"]
        assert with_words["cache"]["hit"] is False

    def test_json_body_with_server_path(self, client, job_root, server_ref, stubs):
        response = client.post("/api/tts/sync", json={
            "text": "服务端路径", "ref_audio_path": server_ref,
            "include_words": True, "timeout_ms": 30000})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["reference"]["clone_mode"] == "reference"

    def test_timeout_returns_job_for_polling(self, client, job_root, ref_wav,
                                             monkeypatch):
        def slow_synthesize(texts, ref_audio, job_id, **kwargs):
            time.sleep(1.2)
            return _fake_synthesize(texts, ref_audio, job_id, **kwargs)

        monkeypatch.setattr("services.tts_api.synthesize_texts", slow_synthesize)
        response = _multipart(client, ref_wav, text="超时降级", timeout_ms="1000")
        assert response.status_code == 202, response.get_json()
        payload = response.get_json()
        assert payload["job_id"]
        assert payload["poll_url"] == f"/api/tts/{payload['job_id']}"
        assert payload["result_url"].endswith("/result")

        final = _wait_terminal(client, payload["job_id"])
        assert final["status"] == "completed"


class TestSyncValidation:

    def test_rejects_short_reference(self, client, job_root, tmp_path, stubs):
        short = _write_wav(str(tmp_path / "short.wav"), seconds=0.5)
        response = _multipart(client, short, text="太短")
        assert response.status_code == 400
        assert response.get_json()["code"] == "ref_too_short"

    def test_rejects_unsupported_format(self, client, job_root, tmp_path, stubs):
        bad = tmp_path / "ref.txt"
        bad.write_text("not audio", encoding="utf-8")
        response = _multipart(client, str(bad), text="格式")
        assert response.status_code == 400
        assert response.get_json()["code"] == "unsupported_ref_format"

    def test_rejects_path_outside_data(self, client, job_root, stubs):
        response = client.post("/api/tts/sync",
                               json={"text": "越权", "ref_audio_path": "/etc/hosts"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "ref_path_forbidden"

    def test_rejects_empty_and_oversized_text(self, client, job_root, ref_wav, stubs):
        assert _multipart(client, ref_wav, text="   ").status_code == 400
        oversized = "字" * (tts_direct.MAX_SYNC_TEXT_CHARS + 1)
        response = _multipart(client, ref_wav, text=oversized)
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_text"

    def test_ref_text_requires_prompt_capable_engine(self, client, job_root,
                                                     ref_wav, stubs):
        stubs.setattr(config, "TTS_ENGINE", "confucius-tts")
        response = _multipart(client, ref_wav, text="引擎", ref_text="参考")
        assert response.status_code == 400
        assert response.get_json()["code"] == "unsupported_ref_text"

    def test_missing_reference(self, client, job_root, stubs):
        response = client.post("/api/tts/sync", json={"text": "缺参考"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "missing_ref_audio"


class TestPaceProcessing:

    def test_word_pace_inserts_separators(self):
        assert tts_direct.apply_pace("Learning English is easier.", "en",
                                     "word") == "Learning, English, is, easier."
        assert tts_direct.apply_pace("学习英语", "zh", "word") == "学，习，英，语"
        assert tts_direct.apply_pace("Learning English", "en", "slow") == "Learning English"
        assert tts_direct.apply_pace("学习英语", "zh", "normal") == "学习英语"

    def test_tempo_slows_audio_without_resampling(self, tmp_path):
        path = _write_wav(str(tmp_path / "tempo.wav"), seconds=1.0)
        slowed = tts_direct._apply_tempo(path, 0.75)
        with wave.open(slowed, "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
            assert stream.getframerate() == 8000, "采样率不变，说明不是重采样"
        assert duration == pytest.approx(1.33, abs=0.06)


class TestWordAlignment:

    def test_char_timings_index_the_input_text(self):
        tokens = [{"text": "今天", "start": 0.0, "end": 0.4},
                  {"text": "天气", "start": 0.4, "end": 0.9}]
        report = tts_words.align_to_text("今天天气", tokens, "zh")
        assert report["coverage"] == 1.0
        assert [item["text"] for item in report["items"]] == ["今", "天", "天", "气"]
        assert report["items"][0]["char_start"] == 0
        assert report["items"][-1]["end"] == pytest.approx(0.9)
        assert [item["start"] for item in report["items"]] == sorted(
            item["start"] for item in report["items"])
        assert report["warnings"] == []

    def test_unmatched_text_is_reported_not_patched(self):
        tokens = [{"text": "今天", "start": 0.0, "end": 0.4}]
        report = tts_words.align_to_text("今天下大雨", tokens, "zh")
        assert report["coverage"] < 1.0
        missing = [item for item in report["items"] if item["start"] is None]
        assert missing, "未识别到的字必须保留为 null 时间"
        assert any(warning.startswith("unmatched_units") for warning in report["warnings"])

    def test_empty_asr_is_explicit(self):
        report = tts_words.align_to_text("你好", [], "zh")
        assert report["coverage"] == 0.0
        assert "asr_empty" in report["warnings"]
        assert all(item["start"] is None for item in report["items"])

    def test_instruction_spoken_detects_read_aloud_control(self):
        style = "语气亲切 语速适中"
        leaked = "语气亲切语速适中一格格英语每天一句"
        assert tts_words.instruction_spoken(leaked, style) is True
        assert tts_words.instruction_spoken("一格格英语每天一句", style) is False
        assert tts_words.instruction_spoken("anything", "") is False

    def test_guidance_override_keeps_shared_runtime_intact(self):
        from pipeline.step3_tts_confucius import _Runtime, _with_cfg

        runtime = _Runtime("script", "python", {}, {"cfg_value": 2.0, "model": "m"},
                           "model-id", "voxcpm2", 1)
        boosted = _with_cfg(runtime, 2.8)
        assert boosted.options["cfg_value"] == 2.8
        assert runtime.options["cfg_value"] == 2.0, "共享运行时不得被修改"
        assert _with_cfg(runtime, None) is runtime

    def test_word_unit_for_english(self):
        tokens = [{"text": "hello", "start": 0.0, "end": 0.3},
                  {"text": "world", "start": 0.3, "end": 0.6}]
        report = tts_words.align_to_text("Hello, world!", tokens, "en")
        assert report["unit"] == "word"
        assert report["coverage"] == 1.0
        assert report["items"][1]["start"] == pytest.approx(0.3)
