"""Use the official SDK in one selected runtime; never replay a failed POST."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit


def canonical_server(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Use an HTTP(S) server URL without credentials, query or fragment')
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), '', ''))


def tools_python(work):
    return Path(work) / '.tools' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def resolve_cli(work):
    explicit = os.environ.get('BILIMIX_CLI')
    if explicit:
        return [explicit]
    isolated = tools_python(work)
    if isolated.is_file():
        return [str(isolated), '-m', 'bilimix_cli']
    if importlib.util.find_spec('bilimix_cli'):
        return [sys.executable, '-m', 'bilimix_cli']
    executable = Path(sys.executable).parent / ('bmx.exe' if os.name == 'nt' else 'bmx')
    found = str(executable) if executable.is_file() else shutil.which('bmx')
    if not found:
        raise ValueError('SDK missing: run workflow.py setup --workdir <project>')
    return [found]


class Client:
    def __init__(self, work, server=None):
        self.command = resolve_cli(work)
        if server:
            self.command += ['--server', canonical_server(server)]
        auth = self.call('auth', 'status')
        if not auth.get('authenticated'):
            raise ValueError('BiliMix login required; see references/operations.md')
        self.server = canonical_server(auth.get('server') or server or '')
        if not server:
            self.command += ['--server', self.server]
        capabilities = self.call('api')
        if capabilities.get('dubbing_protocol') != 1:
            raise ValueError('BiliMix dubbing protocol 1 required')

    def call(self, *arguments):
        result = subprocess.run([*self.command, *map(str, arguments)], capture_output=True,
                                text=True, encoding='utf-8', timeout=7200,
                                env={**os.environ, 'PYTHONUTF8': '1'})
        if result.returncode:
            raise ValueError((result.stderr or result.stdout)[-1600:] or 'BiliMix command failed')
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise ValueError('SDK returned invalid JSON; update the client') from exc
        if not isinstance(value, dict):
            raise ValueError('SDK returned a non-object result')
        return value

    def upload(self, path):
        result = self.call('audio', 'upload', path)
        if not result.get('local_path'):
            raise ValueError('Upload returned no local_path')
        return result['local_path']

    def download(self, command, task_id, target):
        """Commit only a complete transfer; partial files are never deliverables."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name('.' + target.name + '.download')
        try:
            if command == 'audio':
                self.call('audio', 'download', '--task-id', task_id, '--type', 'mixed', '-o', temporary)
            else:
                self.call('video', command, '--task-id', task_id, '-o', temporary)
            if not temporary.is_file() or not temporary.stat().st_size:
                raise ValueError('Empty download')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
