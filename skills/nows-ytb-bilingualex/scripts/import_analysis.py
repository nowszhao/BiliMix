#!/usr/bin/env python3
"""Import server analysis before FULL refinement; no translation is performed."""
import argparse
import json
from pathlib import Path
from audio_first import load_analysis, canonical_rows


def timestamp(ms):
    h, rem = divmod(round(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis")
    parser.add_argument("--workdir", default=".")
    args = parser.parse_args()
    analysis = load_analysis(args.analysis)
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    (work/'analysis.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2),encoding='utf-8')
    for filename, rows in [('transcript_official.srt',canonical_rows(analysis)),('raw_aligned.srt',analysis['words'])]:
        body = [f"{i}\n{timestamp(row['start']*1000)} --> {timestamp(row['end']*1000)}\n{row['text']}\n"
                for i,row in enumerate(rows,1)]
        (work/filename).write_text('\n'.join(body),encoding='utf-8')
    print(f"Imported {len(analysis['words'])} aligned words / {len(analysis['turns'])} turns")


if __name__ == '__main__': main()
