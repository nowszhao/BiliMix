"""Portable unified skill and SDK tests; no live model service is contacted."""

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"


def test_client_auth_sessions_are_isolated_by_server(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "sdk"))
    from bilimix_cli import cli

    monkeypatch.setattr(cli, "SESSION_DIR", tmp_path)
    first = cli.BiliMixClient("https://one.invalid")
    assert not first.session.cookies
    first.session.cookies.set("session", "one")
    first._save_cookies()
    second = cli.BiliMixClient("https://two.invalid")
    assert not second.session.cookies
    assert (
        cli.BiliMixClient("https://one.invalid").session.cookies.get("session") == "one"
    )


def test_packages_install_in_an_arbitrary_directory_with_no_local_state(tmp_path):
    install = runpy.run_path(str(ROOT / "scripts/install_skills.py"))["install"]
    target = tmp_path / "another Agent" / "skills"
    result = install(target)
    assert len(result["installed"]) == 1
    check = target / "nows-ytb-dubbing/scripts/workflow.py"
    if sys.version_info >= (3, 10):
        output = subprocess.run(
            [sys.executable, str(check), "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert output.returncode == 0
    (target / "nows-ytb-dubbing/obsolete.txt").write_text("obsolete")
    install(target, True)
    assert not (target / "nows-ytb-dubbing/obsolete.txt").exists()
    assert sorted(p.name for p in target.iterdir()) == ["nows-ytb-dubbing"]
    package = runpy.run_path(str(ROOT / "scripts/package_skills.py"))["package"]
    archives = package(tmp_path / "distributable")
    import zipfile

    for item in archives:
        with zipfile.ZipFile(item["path"]) as archive:
            assert not any(
                "__pycache__" in n or n.endswith("project.json")
                for n in archive.namelist()
            )
            assert "nows-ytb-dubbing/assets/client.json" in archive.namelist()


def test_model_cache_respects_portable_huggingface_environment(tmp_path):
    from pipeline.model_runtime import hub_cache_dir, configure_diarization_environment

    env = {"HF_HOME": str(tmp_path / "HF home")}
    assert hub_cache_dir(env) == tmp_path / "HF home/hub"
    snapshot = (
        hub_cache_dir(env)
        / "models--pyannote--speaker-diarization-community-1/snapshots/revision"
    )
    for item in [
        "config.yaml",
        "embedding/pytorch_model.bin",
        "segmentation/pytorch_model.bin",
        "plda/plda.npz",
    ]:
        p = snapshot / item
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"cache fixture")
    configure_diarization_environment(env)
    assert env["HF_HUB_OFFLINE"] == "1"
    custom = {**env, "BILIMIX_DIARIZATION_MODEL": "someone/another-model"}
    custom.pop("HF_HUB_OFFLINE")
    configure_diarization_environment(custom)
    assert "HF_HUB_OFFLINE" not in custom


def test_asr_uses_selected_runtime_instead_of_a_foreign_path(tmp_path):
    from pipeline.model_runtime import whisperx_command

    runtime = tmp_path / "model runtime"
    runtime.mkdir()
    executable = runtime / ("whisperx.exe" if os.name == "nt" else "whisperx")
    executable.write_bytes(b"launcher")
    assert whisperx_command("whisperx", str(runtime / "python")) == [str(executable)]
    executable.unlink()
    assert whisperx_command("whisperx", str(runtime / "python")) == [
        str(runtime / "python"),
        "-m",
        "whisperx",
    ]
    assert whisperx_command(["custom-python", "-m", "whisperx"]) == [
        "custom-python",
        "-m",
        "whisperx",
    ]


def test_skill_update_rolls_back_on_install_failure(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / "scripts/install_skills.py"))
    install = module["install"]
    destination = tmp_path / "skills"
    install(destination)
    for name in module["SKILLS"]:
        (destination / name / "installed.txt").write_text("original")
    original = os.replace

    def replace(source, target):
        if Path(source).name == "nows-ytb-dubbing" and ".bilimix-install-" in str(
            source
        ):
            raise OSError("installation interrupted")
        return original(source, target)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError, match="installation interrupted"):
        install(destination, True)
    assert all(
        (destination / name / "installed.txt").read_text() == "original"
        for name in module["SKILLS"]
    )
    assert sorted(p.name for p in destination.iterdir()) == sorted(module["SKILLS"])


def test_unified_workflow_uses_real_sdk_from_a_foreign_directory(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import hashlib
    import threading

    video = tmp_path / "视频 source.mp4"
    video.write_bytes(b"portable media")
    media_hash = hashlib.sha256(video.read_bytes()).hexdigest()
    jobs = {}
    uploads = []
    analysis = dict(
        schema_version=1,
        analysis_id="analysis",
        source_media_sha256=media_hash,
        dubbing_ready=True,
        words=[
            dict(
                word_id=0,
                text="Hello.",
                start=0,
                end=2,
                speaker="A",
                speaker_verified=True,
            )
        ],
        references={"A": dict(speaker="A", start=0, end=2)},
        unresolved_segments=[],
        missing_reference_speakers=[],
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, value):
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/octet-stream"
                if isinstance(value, bytes)
                else "application/json",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.path = self.path.split("?")[0]
            if self.path == "/api":
                return self.send(
                    dict(dubbing_protocol=1, tts_reference_modes=["speaker", "segment"])
                )
            if self.path == "/api/auth/check":
                return self.send(dict(authenticated=True, auth_enabled=False))
            if self.path == "/api/tasks":
                return self.send({"tasks": list(jobs.values())})
            if self.path.startswith("/api/audio/"):
                return self.send(b"complete deliverable")
            task_id = self.path.split("/")[3]
            task = dict(jobs[task_id], status="completed")
            if self.path.endswith("/result"):
                if task_id == "analysis":
                    task["result"] = {"analysis": analysis}
                else:
                    task.update(
                        result=dict(basename="dub", mixed_audio="/api/audio/dub.mp3"),
                        video_result=dict(
                            video_url="/api/audio/dub.mp4",
                            subtitle_url="/api/audio/dub.ass",
                            subtitle_layout=dict(
                                status="PASS",
                                max_observed_lines=4,
                                max_estimated_height_ratio=0.2,
                            ),
                        ),
                        tts_reference_manifest=dict(
                            mode="speaker",
                            fallback_count=0,
                            units=[
                                dict(
                                    segment_index=0,
                                    requested_mode="speaker",
                                    actual_mode="speaker",
                                )
                            ],
                        ),
                    )
            return self.send(task)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/api/upload":
                uploads.append(raw)
                return self.send({"local_path": f"/srv/upload{len(uploads)}"})
            body = json.loads(raw)
            if self.path == "/api/parse-subtitle":
                return self.send(dict(count=1, bilingual_count=1))
            task_id = "analysis" if body["type"] == "analysis" else "dub"
            jobs[task_id] = dict(
                task_id=task_id,
                type=body["type"],
                title=body["title"],
                url="file://" + body["local_path"],
            )
            return self.send({"task_id": task_id})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "sdk"),
        "BILIMIX_HOME": str(tmp_path / "client-home"),
    }
    for key in ("BILIMIX_CLI", "BILIMIX_SERVER"):
        env.pop(key, None)
    work = tmp_path / "portable project"

    def run(*args):
        out = subprocess.run(
            [
                sys.executable,
                str(SKILLS / "nows-ytb-dubbing/scripts/workflow.py"),
                "run",
                "--workdir",
                str(work),
                *map(str, args),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert out.returncode == 0, out.stdout + out.stderr
        return json.loads(out.stdout)

    try:
        action = run(
            "--input",
            video,
            "--server",
            f"http://127.0.0.1:{server.server_port}",
            "--width",
            "1280",
            "--height",
            "720",
        )
        request = json.loads(Path(action["input"]).read_text())
        Path(action["output"]).write_text(
            json.dumps(
                dict(input_id=request["input_id"], sentences=[dict(text="Hello.")])
            )
        )
        action = run()
        request = json.loads(Path(action["input"]).read_text())
        Path(action["output"]).write_text(
            json.dumps(
                dict(
                    input_id=request["input_id"],
                    units=[
                        dict(
                            source_unit_ids=["s_000001"],
                            text_en="Hello.",
                            text_zh="你好。",
                        )
                    ],
                )
            )
        )
        action = run()
        assert action["stage"] == "completed"
        assert len(jobs) == 2 and len(uploads) == 2
        assert jobs["dub"]["title"] == "视频 source"
        assert action["tts_reference_quality"]["total_units"] == 1
        assert (
            Path(action["artifacts"]["dubbed.mp4"]).read_bytes()
            == b"complete deliverable"
        )
        run()
        assert len(jobs) == 2 and len(uploads) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_update_retires_only_the_owned_old_workflows(tmp_path):
    module=runpy.run_path(str(ROOT/'scripts/install_skills.py'))
    install=module['install'];install(tmp_path)
    for name in module['RETIRED_SKILLS']:
        folder=tmp_path/name;folder.mkdir();(folder/'SKILL.md').write_text(f'---\nname: {name}\n---\n')
    unrelated=tmp_path/'nows-ytb-vcover';unrelated.mkdir()
    result=install(tmp_path,True)
    assert len(result['removed'])==2
    assert sorted(p.name for p in tmp_path.iterdir())==['nows-ytb-dubbing','nows-ytb-vcover']


def test_retirement_failure_restores_the_previous_installation(tmp_path,monkeypatch):
    module=runpy.run_path(str(ROOT/'scripts/install_skills.py'))
    install=module['install'];install(tmp_path)
    marker=tmp_path/'nows-ytb-dubbing/original.txt';marker.write_text('original')
    for name in module['RETIRED_SKILLS']:
        folder=tmp_path/name;folder.mkdir();(folder/'SKILL.md').write_text(f'---\nname: {name}\n---\n')
    replace=os.replace
    def fail(source,target):
        if Path(source).name==module['RETIRED_SKILLS'][-1]:raise OSError('retirement interrupted')
        return replace(source,target)
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError):install(tmp_path,True)
    assert marker.read_text()=='original'
    assert all((tmp_path/name/'SKILL.md').is_file() for name in module['RETIRED_SKILLS'])
