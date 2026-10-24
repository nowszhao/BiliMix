"""Shared video media cache must reuse expensive extraction and demucs work."""

import os
import wave
from pathlib import Path

from pipeline import step0_video_prepare as step0
from pipeline import step_vocal_separation as separation
from pipeline.media_cache import (
    is_shared_media_path,
    shared_media_cache_dir,
    vocal_cache_for_task,
)


def _write_wav(path: Path, frames: int = 1600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * frames)


def test_prepare_video_reuses_content_addressed_audio(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video-one")
    video_cache = tmp_path / "video_cache"
    audio_cache = tmp_path / "shared" / "audio"
    calls = []

    def fake_ffmpeg(args, timeout=600):
        calls.append(args)
        _write_wav(Path(args[-1]))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(step0, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(step0, "_probe_video_duration", lambda _: 0)

    first = step0.prepare_video(
        str(source), str(video_cache), audio_cache_dir=str(audio_cache)
    )
    second = step0.prepare_video(
        str(source), str(video_cache), audio_cache_dir=str(audio_cache)
    )

    assert first["ok"] and second["ok"]
    assert first["audio_path"] == second["audio_path"]
    assert Path(first["audio_path"]).parent == audio_cache
    assert len(first["media_sha256"]) == 64
    assert len(calls) == 1

    # A different source content must not reuse the old audio cache.
    source.write_bytes(b"video-two")
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    third = step0.prepare_video(
        str(source), str(video_cache), audio_cache_dir=str(audio_cache)
    )
    assert third["ok"]
    assert third["media_sha256"] != first["media_sha256"]
    assert third["audio_path"] != first["audio_path"]
    assert len(calls) == 2


def test_separate_vocals_reuses_complete_stems(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    _write_wav(audio)
    cache = tmp_path / "shared" / "vocal_separation"
    calls = []

    def fake_demucs(command, **kwargs):
        calls.append(command)
        cache_dir = Path(command[command.index("-o") + 1])
        basename = Path(command[-1]).stem
        output = cache_dir / "htdemucs" / basename
        _write_wav(output / "vocals.wav")
        _write_wav(output / "no_vocals.wav")
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(separation.subprocess, "run", fake_demucs)

    first = separation.separate_vocals(str(audio), str(cache))
    second = separation.separate_vocals(str(audio), str(cache))

    assert first["ok"] and second["ok"]
    assert first["vocals_path"] == second["vocals_path"]
    assert first["no_vocals_path"] == second["no_vocals_path"]
    assert len(calls) == 1


def test_video_tasks_share_vocal_cache_but_audio_tasks_do_not(tmp_path):
    fallback = str(tmp_path / "task-local")
    analysis = vocal_cache_for_task({"type": "analysis"}, fallback)
    video = vocal_cache_for_task({"type": "video"}, fallback)
    audio = vocal_cache_for_task({"type": "audio"}, fallback)

    assert (
        analysis
        == video
        == str(Path(shared_media_cache_dir()) / "vocal_separation_v1")
    )
    assert audio == fallback


def test_shared_media_path_is_recognized(tmp_path, monkeypatch):
    from pipeline import media_cache
    from core import config

    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    shared = tmp_path / "downloads" / "video_cache" / "_shared" / "audio" / "a.wav"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"audio")

    assert is_shared_media_path(str(shared))
    assert not is_shared_media_path(str(tmp_path / "downloads" / "other.wav"))
    assert Path(media_cache.extracted_audio_dir()).name == "audio_v1"


def test_speaker_worker_loads_waveform_without_torchcodec(tmp_path):
    from workers.analyze_speakers import _load_audio

    audio = tmp_path / "speaker.wav"
    _write_wav(audio)
    loaded = _load_audio(str(audio))

    assert loaded["sample_rate"] == 16000
    assert tuple(loaded["waveform"].shape) == (1, 1600)


def test_confirm_speakers_uses_saved_task_basename(tmp_path, monkeypatch):
    from core import config, task_manager as tm
    from core.files import atomic_json
    from services.web_app import app
    from tests.test_audio_first import analysis_fixture

    task_id = "confirm-folder-test"
    basename = "analysis-custom-folder"
    analysis = analysis_fixture()
    analysis["analysis_id"] = task_id
    analysis["words"][0].update(
        speaker=None,
        speaker_candidates=["A", "B"],
        speaker_assignment="ambiguous_overlap",
        reference_eligible=False,
    )
    folder = tmp_path / "results" / basename
    folder.mkdir(parents=True)
    atomic_json(
        folder / "task_result.json",
        {
            "task_id": task_id,
            "type": "analysis",
            "status": "completed",
            "result": {"analysis": analysis},
        },
    )
    tm.tasks[task_id] = {
        "task_id": task_id,
        "type": "analysis",
        "status": "completed",
        "result": {"analysis": analysis},
        "_basename": basename,
    }
    monkeypatch.setattr(config, "RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    try:
        response = app.test_client().post(
            f"/api/dubbing/{task_id}/confirm-speakers",
            json={"resolutions": [{"word_ids": [0], "speaker_id": "A"}]},
        )
        assert response.status_code == 200, response.get_json()
        assert tm.get_task(task_id)["message"].startswith("人物确认完成")
    finally:
        tm.tasks.pop(task_id, None)
