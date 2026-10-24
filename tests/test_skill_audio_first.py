"""The unified skill preserves speech ownership through FULL, translation and export."""

import importlib
import json
from pathlib import Path
import subprocess
import sys
import pytest
from pipeline.dubbing_plan import validate_manifest
from pipeline.speaker_analysis import build_analysis, confirm_speakers

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/nows-ytb-dubbing/scripts"


@pytest.fixture
def skill(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("_language"), importlib.import_module("_export")


def source():
    return build_analysis(
        {
            "segments": [
                {
                    "speaker": speaker,
                    "words": [
                        dict(word=w, start=start + i * 0.5, end=start + (i + 1) * 0.5)
                        for i, w in enumerate(text.split())
                    ],
                }
                for speaker, start, text in [
                    ("A", 0, "We can finish this sentence."),
                    ("B", 4, "Yes we can finish it."),
                ]
            ]
        },
        "media",
    )


def translate_all(skill, work, analysis):
    language, _ = skill
    action = language.advance_language(work, analysis, "dubbing", {})
    request = json.loads(Path(action["input"]).read_text())
    sentences = [dict(text=t["text"]) for t in analysis["turns"]]
    Path(action["output"]).write_text(
        json.dumps(dict(input_id=request["input_id"], sentences=sentences))
    )
    assert (
        language.advance_language(work, analysis, "dubbing", {})["stage"] == "advance"
    )
    action = language.advance_language(work, analysis, "dubbing", {})
    request = json.loads(Path(action["input"]).read_text())
    units = [
        dict(
            source_unit_ids=[u["unit_id"]],
            text_en=u["text"],
            text_zh="我们可以把这句话说完整。",
        )
        for u in request["source_units"]
    ]
    Path(action["output"]).write_text(
        json.dumps(dict(input_id=request["input_id"], units=units))
    )
    assert (
        language.advance_language(work, analysis, "dubbing", {})["stage"] == "advance"
    )
    assert language.advance_language(work, analysis, "dubbing", {}) is None


def test_full_translation_and_export_use_server_owned_ids(skill, tmp_path):
    analysis = source()
    translate_all(skill, tmp_path, analysis)
    artifacts = skill[1].export_work(tmp_path, analysis, "dubbing", (1280, 720), 40)
    manifest = json.loads(Path(artifacts["manifest"]).read_text())
    units = validate_manifest(manifest, analysis)
    assert [u["speaker"] for u in units] == ["A", "B"]
    assert [i for u in units for i in u["source_word_ids"]] == list(range(10))
    assert all(u["text_zh"] == "我们可以把这句话说完整。" for u in units)


def test_confirmation_rebinds_source_ids_without_retranslation(skill, tmp_path):
    analysis = source()
    # A real unknown region remains independently refinable but cannot be dubbed.
    asr = {
        "segments": [
            {
                "words": [
                    dict(word="Hello", start=0, end=1, speaker="A"),
                    dict(word="there", start=1, end=2, speaker="A"),
                    dict(word="friend.", start=2, end=3, speaker="A"),
                ]
            },
            {"words": [dict(word="Yes.", start=4, end=5)]},
        ]
    }
    analysis = build_analysis(asr, "media")
    translate_all(skill, tmp_path, analysis)
    before = (tmp_path / "language/units.json").read_bytes()
    artifacts = skill[1].export_work(tmp_path, analysis, "dubbing", (1280, 720), 40)
    assert "manifest" not in artifacts
    revised = confirm_speakers(analysis, [dict(word_ids=[3], speaker_id="A")])
    artifacts = skill[1].export_work(tmp_path, revised, "dubbing", (1280, 720), 40)
    validate_manifest(json.loads(Path(artifacts["manifest"]).read_text()), revised)
    assert (tmp_path / "language/units.json").read_bytes() == before


def test_standalone_subtitle_workflow_uses_no_sdk_or_site_packages(tmp_path):
    video = tmp_path / "视频.mp4"
    video.write_bytes(b"local media")
    captions = tmp_path / "source.srt"
    captions.write_text(
        "1\n00:00:00,995 --> 00:00:04,000\nWe can finish this complete sentence.\n",
        encoding="utf-8",
    )
    work = tmp_path / "project"

    def run(*args):
        out = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPTS / "workflow.py"),
                "run",
                "--workdir",
                str(work),
                *map(str, args),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert out.returncode == 0, out.stdout + out.stderr
        return json.loads(out.stdout)

    action = run(
        "--input",
        video,
        "--mode",
        "subtitles",
        "--source-subtitles",
        captions,
        "--width",
        "1280",
        "--height",
        "720",
    )
    request = json.loads(Path(action["input"]).read_text())
    Path(action["output"]).write_text(
        json.dumps(
            dict(
                input_id=request["input_id"],
                sentences=[{"text": "We can finish this complete sentence."}],
            )
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
                        source_unit_ids=[u["unit_id"] for u in request["source_units"]],
                        text_en="We can finish this complete sentence.",
                        text_zh="我们可以把这一整句话完整、自然地说完，不必在说到一半时突然停下来。",
                    )
                ],
            )
        )
    )
    action = run()
    assert action["stage"] == "completed"
    report = action["artifacts"]["subtitle_layout"]
    assert report["max_lines"] <= 4 and report["max_estimated_height_ratio"] <= 0.25
    assert not (work / "output/dub_manifest.json").exists()
    assert "0:00:01.00" in (work / "output/refined.ass").read_text()
