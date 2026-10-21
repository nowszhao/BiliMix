#!/usr/bin/env python3
"""Build a complete current release without machine state or development artifacts."""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

from package_skills import package

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ('core', 'pipeline', 'services', 'workers', 'web', 'sdk', 'skills', 'scripts', 'tests', '.github', 'bin', 'examples')
SOURCE_ROOT_FILES = ('README.md', 'README.en.md', 'setup.sh', 'requirements.txt', 'requirements-tts.txt', 'constraints-speech.txt', '.gitignore', 'index.html', 'en.html', 'docs/installation.md', 'docs/audio-first-dubbing.md')
SUFFIXES = {'.py', '.md', '.txt', '.toml', '.yml', '.html', '.css', '.js', '.svg', '.json', '.wav', '.jpg', '.png', '.mp4', '.sh', '.whl'}
EXCLUDED_PARTS = {'__pycache__', 'build', 'dist', '.pytest_cache', '.venv', '.bilimix-tools'}


def release_version():
    tree = ast.parse((ROOT / 'sdk/bilimix_cli/__init__.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('SDK version is required')


def source_files():
    selected = [ROOT / name for name in SOURCE_ROOT_FILES]
    for folder in SOURCE_DIRS:
        for path in (ROOT / folder).rglob('*'):
            relative = path.relative_to(ROOT)
            if (not path.is_file() or path.is_symlink()
                    or any(part in EXCLUDED_PARTS or part.endswith('.egg-info') for part in relative.parts)
                    or path.name in {'config_local.py', 'settings.json', '.DS_Store'}):
                continue
            if path.suffix in SUFFIXES or relative.as_posix() == 'bin/bilimix':
                selected.append(path)
    return sorted(set(selected))


def write_sources(output, files):
    # Stable metadata keeps a source release independent of the builder's account.
    with output.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for path in files:
                member = tarfile.TarInfo('BiliMix/' + path.relative_to(ROOT).as_posix())
                data = path.read_bytes()
                member.size = len(data)
                member.mode = 0o755 if path.name == 'setup.sh' or path.parent.name == 'bin' else 0o644
                archive.addfile(member, io.BytesIO(data))


def build(output):
    requested = Path(output).expanduser()
    if requested.is_symlink():
        raise ValueError('Release output cannot be a symbolic link')
    output = requested.resolve()
    if (output == ROOT or output in ROOT.parents or any(output == ROOT / p or ROOT / p in output.parents for p in SOURCE_DIRS)):
        raise ValueError('Release output must be outside maintained source directories')
    if output.is_symlink() or (output.exists() and not (output / '.bilimix-release').is_file()):
        raise ValueError('Existing output is not a BiliMix release directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/sync_skill_layout.py'), '--check'], check=True)
    version = release_version()
    with tempfile.TemporaryDirectory(prefix='.bilimix-release-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'current'
        stage.mkdir()
        package(stage)
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                        '--wheel-dir', str(stage), str(ROOT / 'sdk')], check=True,
                       env={**os.environ, 'SOURCE_DATE_EPOCH': '315532800'})
        files = source_files()
        write_sources(stage / 'BiliMix-source.tar.gz', files)
        artifacts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(stage.iterdir()) if p.is_file()}
        (stage / 'release.json').write_text(json.dumps({'version': version, 'artifacts': artifacts}, indent=2) + '\n', encoding='utf-8')
        (stage / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in artifacts.items()), encoding='utf-8')
        (stage / '.bilimix-release').write_text(version + '\n', encoding='utf-8')
        previous = Path(temporary) / 'previous'
        if output.exists():
            os.replace(output, previous)
        try:
            os.replace(stage, output)
        except OSError:
            if previous.exists():
                os.replace(previous, output)
            raise
    return {'version': version, 'output': str(output), 'artifacts': list(artifacts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT / 'dist'))
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
