#!/usr/bin/env python3
"""Discover download tools without assuming an Agent, OS, or Python location.

By default only check. --install-missing may install yt-dlp/imageio-ffmpeg in
an isolated --venv (default: .bilimix-tools beside --env-out).
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def log(message):
    print('[check_env] '+message, file=sys.stderr, flush=True)


def venv_executable(directory, name, windows=None):
    windows = os.name == 'nt' if windows is None else windows
    return str(Path(directory)/('Scripts' if windows else 'bin')/(name+('.exe' if windows else '')))


def runtime_executable(name):
    suffix = '.exe' if os.name == 'nt' else ''
    candidate = Path(sys.executable).parent/(name+suffix)
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def install_package(directory, package):
    python = venv_executable(directory, 'python')
    if not Path(python).is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(directory)], check=True)
    subprocess.run([python, '-m', 'pip', 'install', package], check=True)
    return python


def probe_yt_dlp(venv_dir, install=False):
    explicit = os.environ.get('BILIMIX_YTDLP')
    if explicit:
        return shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
    found = runtime_executable('yt-dlp')
    if found:
        return found
    if importlib.util.find_spec('yt_dlp'):
        return [sys.executable, '-m', 'yt_dlp']
    found = shutil.which('yt-dlp')
    if found or not install:
        return found
    python = install_package(venv_dir, 'yt-dlp')
    return [python, '-m', 'yt_dlp']


def probe_ffmpeg(venv_dir, install=False):
    explicit = os.environ.get('BILIMIX_FFMPEG')
    if explicit:
        return shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
    found = runtime_executable('ffmpeg') or shutil.which('ffmpeg')
    if found:
        return found
    python = sys.executable
    if not importlib.util.find_spec('imageio_ffmpeg'):
        if not install:
            return None
        python = install_package(venv_dir, 'imageio-ffmpeg')
    result = subprocess.run([python, '-c', 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'],
                            capture_output=True, text=True, timeout=30)
    value = result.stdout.strip()
    return value if result.returncode == 0 and Path(value).is_file() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-out', default='yt_env.json')
    parser.add_argument('--venv', help='Isolated tools environment; never an Agent runtime')
    parser.add_argument('--install-missing', action='store_true')
    args = parser.parse_args()
    destination = Path(args.env_out).expanduser().resolve()
    directory = Path(args.venv).expanduser() if args.venv else destination.parent/'.bilimix-tools'
    try:
        yt = probe_yt_dlp(directory, args.install_missing)
        ffmpeg = probe_ffmpeg(directory, args.install_missing)
    except (OSError, subprocess.SubprocessError) as error:
        log(str(error)); return 1
    env = {'schema_version':1, 'python':sys.executable, 'machine_local':True,
           'yt_dlp':yt if isinstance(yt,str) else None,
           'yt_dlp_command':([yt] if isinstance(yt,str) else yt), 'ffmpeg':ffmpeg}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(env, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(env, ensure_ascii=False))
    missing = [name for name,value in [('yt-dlp',yt),('ffmpeg',ffmpeg)] if not value]
    if missing:
        log('Missing '+', '.join(missing)+'. Install them or rerun with --install-missing.')
        return 1
    return 0


if __name__ == '__main__':
    from portable_io import configure_utf8
    configure_utf8()
    raise SystemExit(main())
