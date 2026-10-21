#!/usr/bin/env python3
"""Install the current skills into an explicit Agent directory."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

SKILLS = ('nows-ytb-dubbing',)
ROOT = Path(__file__).resolve().parents[1] / 'skills'


def install(destination, update=False):
    destination = Path(destination).expanduser().resolve()
    if destination == ROOT.resolve() or ROOT.resolve() in destination.parents:
        raise ValueError('Installation destination must differ from the source skills directory')
    for name in SKILLS:
        if not (ROOT / name / 'SKILL.md').is_file():
            raise ValueError('Missing source skill: ' + name)
        target = destination / name
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError('Installation target must be a directory: ' + str(target))
        if target.exists() and not update:
            raise ValueError('Already installed: ' + name + '; use --update to replace')

    destination.mkdir(parents=True, exist_ok=True)
    replaced, installed = [], []
    with tempfile.TemporaryDirectory(prefix='.bilimix-install-', dir=destination) as temporary:
        stage = Path(temporary)
        for name in SKILLS:
            shutil.copytree(ROOT / name, stage / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
        try:
            for name in SKILLS:
                target = destination / name
                if target.exists():
                    os.replace(target, stage / (name + '.previous'))
                    replaced.append(name)
                os.replace(stage / name, target)
                installed.append(name)
        except OSError:
            for name in reversed(installed):
                shutil.rmtree(destination / name)
            for name in reversed(replaced):
                os.replace(stage / (name + '.previous'), destination / name)
            raise
    return {'installed': [str(destination / name) for name in SKILLS]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--update', action='store_true', help='Replace installed skills with current sources')
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.destination, args.update), ensure_ascii=False))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
