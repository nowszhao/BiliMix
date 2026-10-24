"""Enforce dependency direction and side-effect-free HTTP construction."""

import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_dependencies_flow_from_transport_to_application_to_pipeline_and_core():
    for folder in ("core", "pipeline", "application"):
        for path in (ROOT / folder).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    assert name.split(".")[0] not in {"flask", "services"}, (
                        f"{path}: reverse import {name}"
                    )
                    if folder in ("core", "pipeline"):
                        assert not name.startswith("application"), (
                            f"{path}: reverse import {name}"
                        )


def test_importing_http_adapter_starts_no_threads_or_network(tmp_path):
    code = """
import threading,socket
from unittest.mock import patch
def forbidden(*a,**k):raise AssertionError('Import must not start background work')
with patch.object(threading.Thread,'start',forbidden),patch.object(socket.socket,'connect',forbidden):
    from services.web_app import create_app
    app=create_app()
    assert '/api/submit' in {str(rule) for rule in app.url_map.iter_rules()}
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "BILIMIX_DATA_DIR": str(tmp_path / "data")},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_source_release_contains_application_and_only_published_skill():
    import runpy

    sys.path.insert(0, str(ROOT / "scripts"))
    release = runpy.run_path(str(ROOT / "scripts/release.py"))
    paths = [p.relative_to(ROOT).as_posix() for p in release["source_files"]()]
    assert "application/task_execution.py" in paths
    skills = {p.split("/")[1] for p in paths if p.startswith("skills/")}
    assert skills == {"nows-ytb-dubbing"}
