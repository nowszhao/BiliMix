#!/usr/bin/env python3
"""Pre-apply drift check for refined_NN.txt (2026-08 lesson: FULL-mode agents
delete filler words -> word-stream match fails -> timeline collapse).

Checks every refine_part_NN.txt / refined_NN.txt pair in <workdir>/refine_parts:
  1. WORD SET  — Counter equality (case/punct ignored). Any missing/extra word
                 means the chunk must be re-dispatched (NEVER fix by mechanical
                 insertion — it corrupts word order and still collapses).
  2. SEQUENCE  — greedy LCS order-match ratio of refined words vs source words.
                 Must be >= 0.995. Lower means the agent reordered words
                 (common when it "cleans" uh/um by moving them).

Usage: python3 check_refined.py --workdir <dir> [--mode full|b]
Exit 0 = all pass, 1 = drift found (fix before running refine_srt.py apply).
"""
import argparse
import collections
import re
import sys


def toks(s: str) -> list[str]:
    return [m.group(0).lower() for m in re.finditer(r"[a-z0-9']+", s.lower())]


def word_counter(tokens: list[str]) -> collections.Counter:
    return collections.Counter(tokens)


def lcs_len(a: list[str], b: list[str]) -> int:
    it = iter(b)
    matched = 0
    try:
        for w in a:
            while True:
                nxt = next(it)
                if nxt == w:
                    matched += 1
                    break
    except StopIteration:
        pass
    return matched


def parse_source_words(path: str) -> list[str]:
    words: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or "\t" not in line:
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                words.extend(toks("\t".join(parts[3:])))
    return words


def parse_refined_words(path: str, mode: str) -> list[str]:
    words: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "\t" not in line:
                continue
            key, text = line.split("\t", 1)
            if mode == "full" and not key.strip().startswith("S"):
                continue
            if mode == "b" and not key.strip().isdigit():
                continue
            words.extend(toks(text))
    return words


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", required=True, help="working dir containing refine_parts/")
    ap.add_argument("--mode", default="full", choices=["full", "b"])
    args = ap.parse_args()

    import os
    parts_dir = os.path.join(args.workdir, "refine_parts")
    set_fail, seq_fail = [], []
    n_pairs = 0
    import json
    plan_path = os.path.join(args.workdir, "refine_plan.json")
    if os.path.isfile(plan_path):
        with open(plan_path) as stream:
            names = [part["file"] for part in json.load(stream)["parts"]]
    else:
        names = sorted(os.path.basename(p) for p in __import__("glob").glob(os.path.join(parts_dir, "refine_part_*.txt")))
    if not names:
        print("[check_refined] No refinement source blocks")
        return 1
    for name in names:
        n = int(re.search(r"(\d+)\.txt$", name)[1])
        src_path = os.path.join(parts_dir, name)
        ref_path = os.path.join(parts_dir, name.replace("refine_part_", "refined_"))
        if not os.path.isfile(src_path) or not os.path.isfile(ref_path):
            print(f"[check_refined] Missing block {name}")
            return 1
        n_pairs += 1
        sw = word_counter(parse_source_words(src_path))
        rw = word_counter(parse_refined_words(ref_path, args.mode))
        if sw != rw:
            set_fail.append(
                (n, dict(sw - rw), dict(rw - sw))
            )
            continue  # skip seq check for this chunk (word set already wrong)
        r = lcs_len(parse_refined_words(ref_path, args.mode),
                    parse_source_words(src_path)) / max(len(parse_refined_words(ref_path, args.mode)), 1)
        if r < 0.995:
            seq_fail.append((n, round(r, 4)))

    print(f"[check_refined] {n_pairs} chunk pair(s) scanned (mode={args.mode})")
    for n, missing, extra in set_fail:
        print(f"  [WORD-SET FAIL] part_{n:02d}: missing={missing} extra={extra}")
    for n, r in seq_fail:
        print(f"  [SEQUENCE FAIL] part_{n:02d}: order-match={r} (<0.995)")

    if set_fail or seq_fail:
        print("[check_refined] DRIFT FOUND — re-dispatch the listed chunks "
              "(prompt must state: '禁止删任何词，包括 uh/um/重复词；词序与源完全一致'), "
              "then re-run this check. Do NOT fix by mechanical word insertion.")
        return 1
    analysis_path = os.path.join(args.workdir, "analysis.json")
    if os.path.isfile(analysis_path) and args.mode == "full":
        from audio_first import attach_sources, load_analysis
        texts = []
        for name in names:
            with open(os.path.join(parts_dir, name.replace("refine_part_", "refined_"))) as stream:
                for line in stream:
                    if line.startswith("S") and "\t" in line:
                        texts.append(line.rstrip("\n").split("\t",1)[1])
        attach_sources(texts, load_analysis(analysis_path))
    print("[check_refined] ALL PASS (word set 100% + sequence >= 0.995)")
    return 0


if __name__ == "__main__":
    from portable_io import configure_utf8
    configure_utf8()
    sys.exit(main())
