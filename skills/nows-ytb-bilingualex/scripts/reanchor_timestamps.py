#!/usr/bin/env python3
"""
reanchor_timestamps.py — pre-assemble safety net for the YouTube MP4 + Bilingual
ASS pipeline (nows-ytb-bilingualex skill).

WHY THIS EXISTS
---------------
`verify_translation.py` only checks EN-content alignment per idx. It does NOT
check timestamps. Translation subagents occasionally SHIFT or DROP timestamps
(this happened on 2 consecutive videos). The drift is invisible to verify but
`assemble_final.py` rejects it ("timestamp changed!"). Symptoms: a whole block's
start/end is offset by +1 (trans[idx] == canonical[idx+1]) while the translated
text stays correct per idx.

FIX
---
Re-anchor every trans line's <start>/<end> from the canonical timebase in
`subtitle_meta.json` (the same source split_translation.py uses), keeping the
translated EN/ZH text by idx. This is safe because verify already proved the
idx->text mapping is correct; only the timestamps were corrupted.

USAGE
-----
  python3 reanchor_timestamps.py --workdir <dir> [--backup]

Backs up parts/trans_*.txt to parts/trans_backup/ (unless --no-backup) then
rewrites each trans line so <start>/<end> == subtitle_meta.json[idx].
"""
import argparse
import glob
import json
import os
import shutil


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()
    wd = args.workdir

    meta_path = os.path.join(wd, "subtitle_meta.json")
    if not os.path.exists(meta_path):
        raise SystemExit(f"ERROR: {meta_path} not found")
    meta = json.load(open(meta_path, encoding="utf-8"))
    # canonical timebase: idx (1-based) -> (start, end)
    canon = {i + 1: (e.get("start"), e.get("end")) for i, e in enumerate(meta["events"])}
    print(f"[reanchor] canonical events: {len(canon)}")

    trans_files = sorted(glob.glob(os.path.join(wd, "parts", "trans_*.txt")))
    if not trans_files:
        raise SystemExit("ERROR: no parts/trans_*.txt found")

    if not args.no_backup:
        bak = os.path.join(wd, "parts", "trans_backup")
        os.makedirs(bak, exist_ok=True)
        for tf in trans_files:
            shutil.copy2(tf, os.path.join(bak, os.path.basename(tf)))
        print(f"[reanchor] backed up {len(trans_files)} file(s) to parts/trans_backup/")

    fixed = 0
    missing = 0
    for tf in trans_files:
        out_lines = []
        for raw in open(tf, encoding="utf-8"):
            raw = raw.rstrip("\n")
            if not raw:
                out_lines.append(raw)
                continue
            f = raw.split("\t")
            if len(f) < 5 or not f[0].isdigit():
                out_lines.append(raw)
                continue
            idx = int(f[0])
            if idx in canon:
                f[1], f[2] = canon[idx]
                fixed += 1
            else:
                missing += 1
            out_lines.append("\t".join(f))
        open(tf, "w", encoding="utf-8").write("\n".join(out_lines) + "\n")

    print(f"[reanchor] re-anchored {fixed} line(s); idx missing from meta: {missing}")
    if missing:
        print("[reanchor] WARNING: some idx not in subtitle_meta.json — check for line-count gaps")


if __name__ == "__main__":
    from portable_io import configure_utf8
    configure_utf8()
    main()
