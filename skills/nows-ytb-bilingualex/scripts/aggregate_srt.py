#!/usr/bin/env python3
"""Aggregate rolling-window source SRT into bounded sentence-level SRT.

Preserve source starts, merge nearby fragments, split at punctuation or speaker
markers, and end each sentence at the next start to prevent overlapping display.

Usage: python aggregate_srt.py input.srt output.srt [--max-gap 700]
"""
import argparse
import re

SENT_END_RE = re.compile(r"[.?!][\"')\]]?$")


def parse_srt_time(t: str) -> int:
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def fmt_srt_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    mss = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{mss:03d}"


def read_captions(in_path: str):
    raw = open(in_path, encoding="utf-8").read()
    blocks = re.split(r"\n\s*\n", raw.strip())
    captions = []  # (start_ms, end_ms, text)
    for b in blocks:
        lines = b.strip().split("\n")
        if len(lines) < 3:
            continue
        try:
            start_s, end_s = lines[1].split(" --> ")
            start_ms = parse_srt_time(start_s.strip())
            end_ms = parse_srt_time(end_s.strip())
        except Exception:
            continue
        text = " ".join(lines[2:]).strip()
        if not text:
            continue
        captions.append((start_ms, end_ms, text))
    return captions


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="fragmented/rolling-window SRT")
    ap.add_argument("output", help="sentence-level SRT")
    ap.add_argument("--max-gap", type=int, default=700, help="max silence ms between merged fragments (default 700)")
    ap.add_argument("--max-group", type=int, default=7500, help="hard cap ms per sentence (default 7500)")
    ap.add_argument("--min-term", type=int, default=1200, help="min group ms before a terminator can split (default 1200)")
    ap.add_argument("--max-words", type=int, default=26, help="hard cap words per aggregated line (default 26)")
    args = ap.parse_args()

    captions = read_captions(args.input)
    if not captions:
        print("No captions parsed from input.", file=__import__("sys").stderr)
        return 1

    sentences = []
    cur = None  # (start_ms, end_ms, [text_parts], word_count)

    def flush():
        nonlocal cur
        if cur is None:
            return
        s, e, parts, _ = cur
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        text = re.sub(r">>\s*", "", text)  # collapse speaker markers
        if text:
            sentences.append((s, e, text))
        cur = None

    for start_ms, end_ms, text in captions:
        is_speaker_change = text.lstrip().startswith(">>")
        ends_sentence = bool(SENT_END_RE.search(text.rstrip()))
        gap = (start_ms - cur[1]) if cur is not None else 0
        group_dur = (cur[1] - cur[0]) if cur is not None else 0
        group_words = cur[3] if cur is not None else 0
        split = (
            cur is None
            or is_speaker_change
            or gap > args.max_gap
            or group_dur > args.max_group
            or group_words > args.max_words
        )
        if not split and ends_sentence and group_dur >= args.min_term:
            split = True

        if split:
            flush()
            cur = [start_ms, end_ms, [text.lstrip()], len(text.split())]
        else:
            cur[1] = end_ms
            cur[2].append(text)
            cur[3] += len(text.split())

    flush()

    # De-overlap: YouTube ASR is a rolling window, so each fragment's end can
    # extend past the next fragment's start. The aggregated sentence inherited
    # end = last fragment's end, which OVERLAPS the next sentence — that leaked
    # into the final ASS. Prevent this by
    # setting each sentence's end = next sentence's start (the same convention
    # the transcript panel uses). Starts are never touched (audio alignment).
    for i in range(len(sentences) - 1):
        s, e, t = sentences[i]
        nxt = sentences[i + 1][0]
        if nxt < e and nxt > s:
            sentences[i] = (s, nxt, t)

    if sentences:
        s, e, t = sentences[-1]
        sentences[-1] = (s, e + 2000, t)

    out = []
    for i, (s, e, t) in enumerate(sentences, 1):
        out.append(f"{i}\n{fmt_srt_time(s)} --> {fmt_srt_time(e)}\n{t}\n")
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"Aggregated {len(captions)} fragments -> {len(sentences)} sentences")
    if sentences:
        print(f"First: {sentences[0][2][:80]}")
        print(f"Last : {sentences[-1][2][:80]}")
    return 0


if __name__ == "__main__":
    from portable_io import configure_utf8
    configure_utf8()
    raise SystemExit(main())