#!/usr/bin/env python3
"""verify_translation.py — post-translation CONTENT-ALIGNMENT check.

Catches "off-by-one block shift" failures where a translation agent merges /
drops / shifts lines inside a chunk (observed 2026-08: one agent merged line N
into line N-1, then shifted every following line up by one and duplicated the
last line; assemble_final.py only caught it line-by-line over 3 iterations).

This script compares each trans line's corrected EN against the SOURCE line's
EN (transcript.txt) by token overlap, so a shifted block is reported in ONE
pass, with the exact idx range of the corruption.

Usage:
    python3 verify_translation.py --workdir . [--min-ratio 0.25]

Exit codes: 0 ok, 1 mismatches found.
"""
import argparse
import glob
import os
import re
import sys


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def overlap(src: str, tgt: str) -> float:
    a, b = tokens(src), tokens(tgt)
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def load_source(transcript_txt: str) -> dict[int, str]:
    src = {}
    for line in open(transcript_txt, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 4 and p[0].isdigit():
            src[int(p[0])] = p[3]
    return src


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--min-ratio", type=float, default=0.25,
                    help="min token-overlap ratio to consider a line aligned")
    ap.add_argument("--transcript", default=None,
                    help="transcript.txt path (default <workdir>/transcript.txt)")
    args = ap.parse_args()

    src = load_source(args.transcript or os.path.join(args.workdir, "transcript.txt"))
    if not src:
        print("[verify] transcript.txt not found or empty — run split_translation first.")
        return 1

    def best_match(idx: int, t: str) -> tuple[int, float]:
        """Nearest source idx (±3) by token overlap — exposes clean shifts."""
        best, br = 0, 0.0
        for j in range(max(1, idx - 3), min(max(src), idx + 3) + 1):
            r = overlap(src[j], t)
            if r > br:
                best, br = j, r
        return best, br

    bad = 0
    for tf in sorted(glob.glob(os.path.join(args.workdir, "parts", "trans_*.txt"))):
        for line in open(tf, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) < 5 or not p[0].isdigit():
                continue
            idx, en = int(p[0]), p[3]
            # pure-music placeholder lines are exempt
            if en.strip() in ("", "...", "♪"):
                continue
            if idx not in src:
                print(f"[{os.path.basename(tf)}] idx {idx}: NOT in source (extra line)")
                bad += 1
                continue
            r = overlap(src[idx], en)
            if r < args.min_ratio:
                bi, br = best_match(idx, en)
                hint = f" — 疑似整体偏移: 内容更接近 src[{bi}] (overlap {br:.2f})"
                if bi != idx:
                    hint += f"，即本行内容对应源的第 {bi} 行"
                print(f"[{os.path.basename(tf)}] idx {idx}: content MISMATCH (overlap {r:.2f}){hint}")
                print(f"    src : {src[idx][:90]}")
                print(f"    trans: {en[:90]}")
                bad += 1

    if bad:
        print(f"\n[verify] {bad} 处内容不对齐。若为整块偏移：对照 transcript.txt 重建对应 idx 区间"
              "（时间戳用源值，内容下移/上移回位），再重新 assemble。")
        return 1
    print(f"[verify] 全部 {len(src)} 行内容对齐通过。")
    return 0


if __name__ == "__main__":
    from portable_io import configure_utf8
    configure_utf8()
    raise SystemExit(main())
