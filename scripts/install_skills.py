#!/usr/bin/env python3
"""Install both portable skills into an explicitly chosen Agent skill directory.

--update backs up existing skill directories before replacing them. No Agent
home, user account, Python environment, or server URL is assumed.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile

SKILLS = ('nows-ytb-bilingualex','nows-ytb-bmxtask')
ROOT = Path(__file__).resolve().parents[1]/'skills'


def install(destination, update=False):
    destination = Path(destination).expanduser().resolve()
    if destination == ROOT.resolve() or ROOT.resolve() in destination.parents:
        raise ValueError('Installation destination must differ from the source skills directory')
    for name in SKILLS:
        if not (ROOT/name/'SKILL.md').is_file():
            raise ValueError('Missing source skill: '+name)
        if (destination/name).exists() and not update:
            raise ValueError('Already installed: '+name+'; use --update to retain a backup and replace')
    destination.mkdir(parents=True,exist_ok=True)
    backup = destination/'.bilimix-skill-backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    installed = []
    for name in SKILLS:
        target = destination/name
        with tempfile.TemporaryDirectory(prefix='.bilimix-install-',dir=destination) as stage:
            prepared = Path(stage)/name
            shutil.copytree(ROOT/name,prepared,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.DS_Store'))
            if target.exists():
                backup.mkdir(parents=True,exist_ok=True)
                os.replace(target,backup/name)
            try:
                os.replace(prepared,target)
            except OSError:
                if (backup/name).exists():
                    os.replace(backup/name,target)
                raise
        installed.append(str(target))
    return {'installed':installed,'backup':str(backup) if backup.exists() else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--update',action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.destination,args.update),ensure_ascii=False));return 0
    except (OSError,ValueError) as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False));return 1


if __name__ == '__main__':
    raise SystemExit(main())
