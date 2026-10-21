#!/usr/bin/env python3
"""
deoverlap.py — Minimal fix for subtitle lines that have NO visible window.

Why this exists (and what it does NOT do):

YouTube ASR subtitles are a "rolling window": each line is a 2-5s window that
starts while the previous line is still on screen. That overlap is NORMAL — the
start timestamps are aligned to the audio (verified: audio speech onset 16.318s
vs. first caption start 16.32s). Therefore this script NEVER pushes a line's
start forward to de-overlap it: doing so would break lip-sync (the subtitle
would appear 1-2s after the words are spoken).

The only real defect is a line whose ENTIRE window is inside the previous
line's (end <= previous end): it has no time of its own, so a player shows it
for 0 seconds. This script gives those rare lines a minimum visible window by
EXTENDING their end forward (start is never touched). The audio-alignment
of every other line is preserved exactly.

Only the timestamps change; text/style/layer are untouched; line count is
unchanged.

Usage:
    python3 deoverlap.py <input.ass> [--out <output.ass>] [--min-duration 0.5]

If --out is omitted, the input file is modified in place.
"""

import argparse
import re


def log(msg: str) -> None:
    print(f"[deoverlap] {msg}", flush=True)


DIALOGUE_RE = re.compile(
    r"^(Dialogue:\s*\d+,)([^,]+),([^,]+),"
)

TS_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$")


def to_centiseconds(ts: str) -> int | None:
    """'H:MM:SS.cc' -> centiseconds, or None if malformed."""
    m = TS_RE.match(ts.strip())
    if not m:
        return None
    h, mi, s, cc = (int(m.group(1)), int(m.group(2)),
                    int(m.group(3)), int(m.group(4)))
    return ((h * 3600 + mi * 60 + s) * 100) + cc


def to_ts(cs: int) -> str:
    """centiseconds -> 'H:MM:SS.cc' (hour without leading zero, like the source)."""
    cs = max(cs, 0)
    cc = cs % 100
    total_s = cs // 100
    s = total_s % 60
    total_m = total_s // 60
    mi = total_m % 60
    h = total_m // 60
    return f"{h}:{mi:02d}:{s:02d}.{cc:02d}"


def process(lines: list[str], min_duration_cs: int) -> tuple[list[str], int]:
    """Return (new_lines, n_fixed). start timestamps are NEVER modified."""
    out = []
    prev_end_cs: int | None = None
    n_fixed = 0
    for ln in lines:
        m = DIALOGUE_RE.match(ln)
        if not m:
            out.append(ln)
            continue
        start_cs = to_centiseconds(m.group(2))
        end_cs = to_centiseconds(m.group(3))
        if start_cs is None or end_cs is None:
            out.append(ln)
            continue

        # NEVER touch start (it is aligned to the audio). Only fix a line that
        # has no window at all: extend its END forward to a visible minimum.
        if prev_end_cs is not None and end_cs <= prev_end_cs:
            end_cs = max(start_cs + min_duration_cs, prev_end_cs + min_duration_cs)
            n_fixed += 1

        new_line = f"{m.group(1)}{to_ts(start_cs)},{to_ts(end_cs)}," + ln[m.end():]
        out.append(new_line)
        prev_end_cs = end_cs
    return out, n_fixed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="path to the .ass file")
    ap.add_argument("--out", default=None,
                    help="output path (default: modify the input in place)")
    ap.add_argument("--min-duration", type=float, default=0.5,
                    help="minimum visible window in seconds for lines that "
                         "would otherwise have none (default 0.5)")
    args = ap.parse_args()

    min_cs = int(round(args.min_duration * 100))
    if min_cs < 1:
        log("--min-duration must be > 0")
        return 1

    try:
        with open(args.input, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        log(f"File not found: {args.input}")
        return 1

    new_lines, n_fixed = process(lines, min_cs)

    out_path = args.out or args.input
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    total_dialogue = sum(1 for ln in lines if ln.startswith("Dialogue:"))
    log(f"Dialogue lines: {total_dialogue}")
    log(f"Lines with no window, end extended by min {args.min_duration}s: {n_fixed}")
    log(f"NOTE: start timestamps were NOT modified (audio alignment preserved).")
    log(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    from portable_io import configure_utf8
    configure_utf8()
    raise SystemExit(main())
