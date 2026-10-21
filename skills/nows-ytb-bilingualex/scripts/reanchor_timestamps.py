#!/usr/bin/env python3
"""Restore work translation timestamps from subtitle_meta.json by source index.

Run verify_translation.py first. Text and row identity are preserved. This
command edits work translations only; final dubbed subtitles use audio timing.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile


def reanchor(workdir):
    workdir = Path(workdir)
    meta = json.loads((workdir / 'subtitle_meta.json').read_text(encoding='utf-8'))
    canonical = {i + 1: (event['start'], event['end']) for i, event in enumerate(meta['events'])}
    prepared, seen = [], set()
    files = sorted((workdir / 'parts').glob('trans_*.txt'))
    if not files:
        raise ValueError('No parts/trans_*.txt found')
    for path in files:
        rows = []
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            fields = line.split('\t')
            if len(fields) != 5 or not fields[0].isdigit():
                raise ValueError('Invalid translation row in ' + path.name)
            index = int(fields[0])
            if index not in canonical or index in seen:
                raise ValueError('Unknown or duplicate source index: ' + str(index))
            seen.add(index)
            fields[1:3] = canonical[index]
            rows.append('\t'.join(fields))
        prepared.append((path, '\n'.join(rows) + '\n'))
    if seen != set(canonical):
        raise ValueError('Translation rows must cover every source index')
    for path, text in prepared:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.reanchor-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(text)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return len(seen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True)
    args = parser.parse_args()
    print(json.dumps({'reanchored': reanchor(args.workdir)}))


if __name__ == '__main__':
    from portable_io import configure_utf8
    configure_utf8()
    main()
