#!/usr/bin/env python3
"""Fallback apply: map refined sentences onto transcript_official timestamps by
exact 1:1 word-stream consumption.

refine_srt.py apply collapsed the timeline (859 lines pinned to one START) even
though every chunk's word stream matches the source EXACTLY. Since the mapping
is provably 1:1, we can derive timestamps directly instead of greedy-matching
against raw ASR fragments.

Rule: a sentence's START = interpolated time of its first word inside the source
line that owns it; END = next sentence's START (last sentence ends with the last
source line's END).
"""
import json
import os
import re
import sys

WORKDIR = os.getcwd()
WORD = re.compile(r"[a-zA-Z0-9']+")


def ts_to_ms(t):
    h, m, rest = t.strip().split(":")
    s, ms = rest.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def ms_to_ts(v):
    v = int(round(v))
    h, v = divmod(v, 3600000)
    m, v = divmod(v, 60000)
    s, ms = divmod(v, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def read_srt(path):
    out = []
    for blk in open(path, encoding="utf-8").read().strip().split("\n\n"):
        lines = [l for l in blk.split("\n") if l.strip()]
        if len(lines) >= 2 and "-->" in lines[1]:
            a, b = lines[1].split(" --> ")
            out.append((ts_to_ms(a.strip()), ts_to_ms(b.strip()), " ".join(lines[2:])))
    return out


def main():
    import argparse
    global WORKDIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=WORKDIR)
    WORKDIR = parser.parse_args().workdir
    official = read_srt(os.path.join(WORKDIR, "transcript_official.srt"))

    # Build per-word ownership: (line_start_ms, line_end_ms, idx_in_line, n_words_in_line)
    owning = []
    for st, en, text in official:
        ws = WORD.findall(text)
        n = len(ws)
        for k in range(n):
            owning.append((st, en, k, n))

    # Collect refined sentences in part order
    plan = json.load(open(os.path.join(WORKDIR, "refine_plan.json"), encoding="utf-8"))
    texts = []
    for part in plan.get("parts", []):
        rname = part["file"].replace("refine_part_", "refined_")
        rpath = os.path.join(os.path.join(WORKDIR, "refine_parts"), rname)
        if not os.path.exists(rpath):
            sys.exit(f"missing {rpath}")
        seq_map = {}
        for line in open(rpath, encoding="utf-8"):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            f = line.split("\t")
            if len(f) < 2:
                continue
            m = re.match(r"^S(\d+)$", f[0].strip())
            if not m:
                continue
            seq_map[int(m.group(1))] = f[1]
        for seq in sorted(seq_map):
            texts.append(seq_map[seq])

    source_words = [w.lower() for _,_,t in official for w in WORD.findall(t)]
    refined_words = [w.lower() for t in texts for w in WORD.findall(t)]
    if source_words != refined_words:
        sys.exit("Exact apply requires identical word sequence, not just word counts")
    # 1:1 consume
    pos = 0
    starts = []
    end_of_last = None
    for t in texts:
        n = len(WORD.findall(t))
        if pos >= len(owning):
            sys.exit(f"word stream exhausted early at sentence {len(starts)+1}")
        st, en, k, ln = owning[pos]
        # interpolate inside the owning line
        frac = (k / ln) if ln else 0.0
        starts.append(st + (en - st) * frac)
        pos += n
        end_of_last = owning[pos-1][1]
    if pos != len(owning):
        sys.exit(f"word mismatch: consumed {pos}, source has {len(owning)}")

    ends = starts[1:] + [end_of_last]

    out = []
    for i, (t, s, e) in enumerate(zip(texts, starts, ends), 1):
        if e <= s:
            e = s + 500
        out.append(f"{i}\n{ms_to_ts(s)} --> {ms_to_ts(e)}\n{t}\n")
    dst = os.path.join(WORKDIR, "transcript_refined.srt")
    open(dst, "w", encoding="utf-8").write("\n".join(out))
    print(f"[exact-apply] wrote {len(out)} lines -> {dst}")
    print(f"[exact-apply] first START={ms_to_ts(starts[0])} last END={ms_to_ts(ends[-1])}")


if __name__ == "__main__":
    from portable_io import configure_utf8
    configure_utf8()
    main()
