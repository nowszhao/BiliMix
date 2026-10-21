#!/usr/bin/env python3
"""Maintain the standalone skill's vendored, dependency-free layout module.

The source of truth is pipeline/subtitle_layout.py. --check is used by CI and
release packaging; no installed Agent directory is touched by this command.
"""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'pipeline/subtitle_layout.py'
DESTINATION = ROOT/'skills/nows-ytb-dubbing/scripts/subtitle_layout.py'


def sync(check=False):
    equal = DESTINATION.is_file() and SOURCE.read_bytes() == DESTINATION.read_bytes()
    if check:
        return equal
    if not equal:
        shutil.copy2(SOURCE,DESTINATION)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args = parser.parse_args()
    if not sync(args.check):
        raise SystemExit('Layout copy differs; run python scripts/sync_skill_layout.py')
