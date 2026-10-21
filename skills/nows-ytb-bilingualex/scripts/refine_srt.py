#!/usr/bin/env python3
"""
refine_srt.py — Semantic re-splitting of ASR subtitles using the Agent's own
model for every splitting DECISION and this script for every timestamp
(script-anchored, never model-authored).

Two operation modes (controlled by which prepare subcommand you run):

  MODE B — "over-long only" (default, see `prepare`)
      Only lines longer than --max-words or --max-dur-ms are re-split; other
      lines keep aggregate_srt.py's mechanical boundaries. Fast, low-cost,
      fixes the "one line is 30+ words" problem.

  MODE FULL — "full re-split" (see `prepare-full`)
      EVERY aggregated line is handed to the Agent, which may keep, split, or
      MERGE adjacent lines, so sentence boundaries are semantic end-to-end
      (mechanical splits that cut a sentence in half get healed). Higher cost
      (a full pass of the model before translation), better boundaries.

The Agent outputs sentence text only — never timestamps. `apply` maps every
output sentence back onto the SOURCE FRAGMENT timeline with a greedy global
word-stream match: sentence START = the START of the fragment holding the
sentence's first word (inherited verbatim; refined by word-count ratio inside
the fragment when the first word sits mid-fragment), END = next sentence's
START (de-overlap).

Pipeline position:
    raw ASR fragments -> aggregate_srt.py -> [THIS STEP] -> split_translation.py
                                             (LLM refine, optional)

Usage:
    # mode B
    python3 refine_srt.py prepare <sentence_level.srt> <fragments.srt> \
        [--workdir .] [--max-words 16] [--max-dur-ms 6000] \
        [--lines-per-part 30] [--context-lines 2]
    # mode FULL
    python3 refine_srt.py prepare-full <sentence_level.srt> <fragments.srt> \
        [--workdir .] [--max-words 16] [--lines-per-part 40] [--context-lines 2]
    # apply (works for both modes)
    python3 refine_srt.py apply [--workdir .] [--out transcript_refined.srt]

Exit codes: 0 ok, 1 fatal, 2 validation problems in apply (nothing written).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


def log(msg: str) -> None:
    print(f"[refine] {msg}", flush=True)


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


def read_captions(in_path: str) -> list[tuple[int, int, str]]:
    """Parse an SRT into (start_ms, end_ms, text) tuples."""
    raw = open(in_path, encoding="utf-8").read()
    blocks = re.split(r"\n\s*\n", raw.strip())
    captions = []
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


def assign_fragments(lines: list[tuple[int, int, str]],
                     frags: list[tuple[int, int, str]]) -> list[list[int]]:
    """Each aggregated line consumes the fragments whose START falls inside its
    window (END is the de-overlapped next-line START, so this is exact)."""
    out: list[list[int]] = []
    fi = 0
    n_frags = len(frags)
    for ls, le, _ in lines:
        idxs = []
        while fi < n_frags and frags[fi][0] < le:
            idxs.append(fi)
            fi += 1
        out.append(idxs)
    return out


def build_word_seq(frag_idxs: list[int],
                   frags: list[tuple[int, int, str]]) -> list[tuple[str, int]]:
    """Word stream of one line with its source fragment index:
    [(word, frag_idx), ...]. Speaker markers are stripped (matches aggregate)."""
    words = []
    for fi in frag_idxs:
        for w in frags[fi][2].split():
            w2 = re.sub(r">>\s*", "", w)
            if w2:
                words.append((w2, fi))
    return words


def load_global_context(workdir: str) -> dict:
    ctx = {"title": None, "url": None, "duration_sec": None}
    top_manifest = os.path.join(workdir, "manifest.json")
    if os.path.exists(top_manifest):
        try:
            with open(top_manifest, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in ctx:
                ctx[key] = data.get(key)
        except Exception as e:
            log(f"WARN: could not read manifest for context ({e})")
    return ctx


def make_chunk_header(ctx: dict, prev_lines: list[str], mode: str) -> str:
    h = ["# === GLOBAL CONTEXT (reference only — do NOT translate) ==="]
    if ctx.get("title"):
        h.append(f"# Video: {ctx['title']}")
    if ctx.get("url"):
        h.append(f"# URL: {ctx['url']}")
    if ctx.get("duration_sec"):
        d = ctx["duration_sec"]
        h.append(f"# Duration: {int(d // 60)}m {int(d % 60):02d}s")
    h.append("# Subtitle source: auto-generated ASR (en), aggregated sentence-level")
    if mode == "full":
        h += [
            "# Task: rewrite the WHOLE block below into semantically complete",
            "#       sentences. For each line you may KEEP it, SPLIT it into",
            "#       several clauses, or MERGE it with the NEXT line.",
            "# Output ONE line per final sentence: S<seq>\\t<sentence text>",
            "#       (seq = 1,2,3... in video order, one output line per sentence)",
            "# Keep every word in order across the block (only punctuation may",
            "#       be added); Complete speech first; 5-16 words is a soft guide, not a limit. NEVER output timestamps.",
        ]
    else:
        h += [
            "# Task: re-split each over-long line below into semantically complete",
            "#       clauses. Output ONE line per clause: <idx>\\t<clause text>",
            "#       Keep idx as-is; keep every word in order (only punctuation",
            "#       may be added); 2-4 clauses per line; 5-16 words per clause;",
            "#       NEVER output timestamps.",
        ]
    h.append("# === END GLOBAL CONTEXT ===")
    if prev_lines:
        h.append("# === PREVIOUS CHUNK (reference only — do NOT translate) ===")
        for ln in prev_lines:
            h.append(f"# {ln.rstrip(chr(10))}")
        h.append("# === END PREVIOUS CHUNK ===")
    if mode == "full":
        h.append("# === LINES TO REWRITE (idx\\tstart\\tend\\ttext) ===")
    else:
        h.append("# === LINES TO RE-SPLIT (idx\\tstart\\tend\\ttext) ===")
    return "\n".join(h) + "\n"


# ---------------------------------------------------------------------------
# prepare (mode B) & prepare-full (mode FULL)
# ---------------------------------------------------------------------------

def _write_plan(plan: dict, workdir: str) -> None:
    with open(os.path.join(workdir, "refine_plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    log(f"Plan written: {os.path.join(workdir, 'refine_plan.json')}")


def _base_plan(args) -> dict:
    return {
        "mode": getattr(args, "mode", "b"),
        "sentence_srt": os.path.abspath(args.sentence),
        "fragments_srt": os.path.abspath(args.fragments),
        "manifest": os.path.abspath(args.manifest) if args.manifest else None,
        "max_words": args.max_words,
        "max_dur_ms": getattr(args, "max_dur_ms", 6000),
        "overlong": [],
        "parts": [],
        "output": args.out,
    }


def cmd_prepare(args) -> int:
    if not os.path.exists(args.sentence) or not os.path.exists(args.fragments):
        log("sentence-level SRT and fragments SRT are both required.")
        return 1

    lines = read_captions(args.sentence)
    frags = read_captions(args.fragments)
    if not lines or not frags:
        log("No captions parsed from one of the inputs.")
        return 1

    assignment = assign_fragments(lines, frags)
    wordseqs = [build_word_seq(a, frags) for a in assignment]

    overlong: list[int] = []
    for i, ((ls, le, _), wseq) in enumerate(zip(lines, wordseqs), start=1):
        n_words = len(wseq)
        dur = le - ls
        if n_words > args.max_words or dur > args.max_dur_ms:
            overlong.append(i)

    log(f"{len(lines)} aggregated lines, {len(overlong)} over-long "
        f"(words>{args.max_words} or dur>{args.max_dur_ms}ms)")

    plan = _base_plan(args)
    if not overlong:
        _write_plan(plan, args.workdir)
        log("Nothing to refine — write a no-op message and exit 0.")
        return 0

    parts_dir = os.path.join(args.workdir, "refine_parts")
    os.makedirs(parts_dir, exist_ok=True)
    ctx = load_global_context(args.workdir)

    src_lines = []
    for i in range(len(lines)):
        s, e, t = lines[i]
        src_lines.append(f"{i + 1}\t{fmt_srt_time(s)}\t{fmt_srt_time(e)}\t{t}")

    chunks: list[list[int]] = []
    cur: list[int] = []
    for idx in overlong:
        if cur and len(cur) >= args.lines_per_part:
            chunks.append(cur)
            cur = []
        cur.append(idx)
    if cur:
        chunks.append(cur)

    parts = []
    for p, idxs in enumerate(chunks, start=1):
        first = idxs[0]
        prev_start = max(0, first - 1 - args.context_lines)
        prev_lines = src_lines[prev_start:first - 1]
        header = make_chunk_header(ctx, prev_lines, "b")
        body = "\n".join(src_lines[i - 1] for i in idxs)
        fname = f"refine_part_{p:02d}.txt"
        with open(os.path.join(parts_dir, fname), "w", encoding="utf-8") as f:
            f.write(header + body + "\n")
        parts.append({"file": fname, "idxs": idxs})
        log(f"  {fname}: lines {idxs}")

    plan["overlong"] = overlong
    plan["parts"] = parts
    _write_plan(plan, args.workdir)
    log("Next: Agent translates each refine_part_NN.txt into refined_NN.txt "
        "(see references/refine_prompt.md), then run `apply`.")
    return 0


def cmd_prepare_full(args) -> int:
    if not os.path.exists(args.sentence) or not os.path.exists(args.fragments):
        log("sentence-level SRT and fragments SRT are both required.")
        return 1

    lines = read_captions(args.sentence)
    frags = read_captions(args.fragments)
    if not lines or not frags:
        log("No captions parsed from one of the inputs.")
        return 1

    parts_dir = os.path.join(args.workdir, "refine_parts")
    os.makedirs(parts_dir, exist_ok=True)
    ctx = load_global_context(args.workdir)

    src_lines = []
    for i in range(len(lines)):
        s, e, t = lines[i]
        src_lines.append(f"{i + 1}\t{fmt_srt_time(s)}\t{fmt_srt_time(e)}\t{t}")

    n = args.lines_per_part
    total = len(lines)
    n_parts = (total + n - 1) // n
    parts = []
    for p in range(1, n_parts + 1):
        start_line = (p - 1) * n + 1
        end_line = min(p * n, total)
        idxs = list(range(start_line, end_line + 1))
        prev_start = max(0, start_line - 1 - args.context_lines)
        prev_lines = src_lines[prev_start:start_line - 1]
        header = make_chunk_header(ctx, prev_lines, "full")
        analysis_path = os.path.join(args.workdir, "analysis.json")
        if os.path.exists(analysis_path):
            from audio_first import load_analysis, canonical_rows
            analysis = load_analysis(analysis_path)
            source_rows = canonical_rows(analysis)
            if [row["text"] for row in source_rows] != [line[2] for line in lines]:
                raise ValueError("Use import_analysis.py canonical source before FULL refinement")
            header += "# Same-speaker complete speech may merge/split; NEVER merge different speakers.\n"
            for idx in idxs:
                turn = source_rows[idx-1]
                header += f"# source row {idx}: speaker={turn['speaker']} turn={turn['turn_id']}\n"
        body = "\n".join(src_lines[i - 1] for i in idxs)
        fname = f"refine_part_{p:02d}.txt"
        with open(os.path.join(parts_dir, fname), "w", encoding="utf-8") as f:
            f.write(header + body + "\n")
        parts.append({"file": fname, "idxs": idxs})
        log(f"  {fname}: lines {start_line}-{end_line}")

    plan = _base_plan(args)
    plan["mode"] = "full"
    plan["parts"] = parts
    _write_plan(plan, args.workdir)
    log(f"Mode FULL: {n_parts} chunks covering ALL {total} lines.")
    log("Next: Agent rewrites each refine_part_NN.txt into refined_NN.txt "
        "(see references/refine_prompt_full.md), then run `apply`.")
    return 0


# ---------------------------------------------------------------------------
# apply (works for both modes)
# ---------------------------------------------------------------------------

def parse_refined(path: str) -> list[tuple[str, str]]:
    """Read one refined_NN.txt into [(seq_or_idx, text), ...] preserving order.
    Mode b: seq_or_idx is the source idx; mode full: it is the 'S<seq>' token."""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            parts = ln.rstrip("\n").split("\t", 1)
            if len(parts) != 2:
                log(f"WARN: bad refined line in {os.path.basename(path)}: {ln[:60]!r}")
                continue
            out.append((parts[0].strip(), parts[1].strip()))
    return out


def norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def _frag_word_starts(stream: list[tuple[str, int, int]]) -> dict[int, int]:
    """Map each fragment index to its first position in the GLOBAL word stream."""
    starts: dict[int, int] = {}
    for i, (_, fi, _) in enumerate(stream):
        starts.setdefault(fi, i)
    return starts


def _refined_start(stream: list[tuple[str, int, int]],
                   frags: list[tuple[int, int, str]],
                   frag_starts: dict[int, int], pos: int) -> int:
    """START for the word at `pos`: the source fragment's START when the word
    begins that fragment (strict inheritance), otherwise an intra-fragment
    refinement by word-count ratio (only used when a sentence boundary falls
    INSIDE a fragment, which would otherwise produce overlap / 0-length lines).
    """
    _, fi, _ = stream[pos]
    f_start, f_end, ftext = frags[fi]
    offset = pos - frag_starts.get(fi, 0)
    if offset <= 0:
        return f_start
    frag_words = [w for w in (re.sub(r">>\s*", "", x) for x in ftext.split()) if w]
    total = len(frag_words)
    if total <= 1:
        return f_start
    ratio = min(offset / total, 1.0)
    return int(f_start + ratio * (f_end - f_start))


def build_global_stream(lines: list[tuple[int, int, str]],
                        assignments: list[list[int]],
                        frags: list[tuple[int, int, str]]):
    """Concatenate every line's words into one global stream.
    Returns (stream, line_ranges) where each element is (word, frag_idx, line_idx)
    and line_ranges maps line_idx -> (start_pos, end_pos)."""
    stream: list[tuple[str, int, int]] = []
    line_ranges: dict[int, tuple[int, int]] = {}
    for i, a in enumerate(assignments, start=1):
        ws = build_word_seq(a, frags)
        line_ranges[i] = (len(stream), len(stream) + len(ws))
        stream.extend((w, fi, i) for w, fi in ws)
    return stream, line_ranges


def match_sentence(sentence_words: list[str], stream: list[tuple[str, int, int]],
                   pos: int) -> tuple[int, int]:
    """Greedy match a sentence's words against the global stream from `pos`.
    Source words that don't match are skipped (treated as deleted by the model).
    Returns (end_pos, unmatched) where unmatched counts sentence words absent
    from the remaining stream (model reworded / added content).

    Hyphenated words match their source components. Unmatched tokens are
    counted without consuming the rest of the source stream.
    """
    end = pos
    unmatched = 0
    n_stream = len(stream)
    for sw in sentence_words:
        nsw = norm_word(sw)
        if not nsw:
            continue
        # expand hyphenated tokens into components for matching
        subs = nsw.split("-") if "-" in nsw else [nsw]
        saved = end
        ok = True
        for sub in subs:
            scan = saved
            found = False
            while scan < n_stream:
                if norm_word(stream[scan][0]) == sub:
                    found = True
                    scan += 1
                    break
                scan += 1
            if not found:
                ok = False
                break
            saved = scan
        if ok:
            end = saved
        else:
            unmatched += 1
            # do NOT advance `end` — a missing token is skipped in place so the
            # next word resumes from the current position instead of n_stream.
    return end, unmatched


def build_sentence_texts(mode: str, lines: list[tuple[int, int, str]],
                         overlong_set: set[int],
                         refined: dict[int, list[str]] | None) -> tuple[list[str], list[int]]:
    """Ordered list of sentence texts to emit, plus the source line each
    sentence's first word belongs to (for B mode sanity checks).

    mode 'full': every sentence comes from the Agent's rewrite (may merge lines).
    mode 'b':    non-over-long lines verbatim + over-long lines' clauses.
    """
    if mode == "full":
        if not refined:
            return [], []
        texts, lines_ = [], []
        for seq in sorted(refined.keys(), key=lambda k: _seq_key(k)):
            for t in refined[seq]:
                texts.append(t)
                lines_.append(None)
        return texts, lines_

    texts, lines_ = [], []
    for i, (ls, le, text) in enumerate(lines, start=1):
        if i in overlong_set:
            clauses = (refined or {}).get(i) or [text]
            for c in clauses:
                texts.append(c)
                lines_.append(i)
        else:
            texts.append(text)
            lines_.append(i)
    return texts, lines_


def _seq_key(k) -> int:
    if isinstance(k, int):
        return k
    try:
        return int(str(k).lstrip("S"))
    except ValueError:
        return 10 ** 9


def cmd_apply(args) -> int:
    workdir = args.workdir
    plan_path = os.path.join(workdir, "refine_plan.json")
    if not os.path.exists(plan_path):
        log(f"Plan not found: {plan_path}. Run `prepare` or `prepare-full` first.")
        return 1
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    mode = plan.get("mode", "b")
    lines = read_captions(plan["sentence_srt"])
    frags = read_captions(plan["fragments_srt"])
    assignments = assign_fragments(lines, frags)
    stream, _ = build_global_stream(lines, assignments, frags)

    parts_dir = os.path.join(workdir, "refine_parts")
    missing_parts = []
    for part in plan.get("parts", []):
        rname = part["file"].replace("refine_part_", "refined_")
        if not os.path.exists(os.path.join(parts_dir, rname)):
            missing_parts.append(rname)

    problems: list[str] = []
    if missing_parts:
        problems.append(f"missing refined files: {missing_parts}")

    overlong_set = set(plan.get("overlong", []))

    if mode == "full":
        # full mode: every refined_NN.txt numbers its sentences S<seq> from 1,
        # so they MUST be read per part (in part order), never merged into one
        # dict (seq keys would collide and later parts would overwrite earlier).
        texts: list[str] = []
        for part in plan.get("parts", []):
            rname = part["file"].replace("refine_part_", "refined_")
            rpath = os.path.join(parts_dir, rname)
            if not os.path.exists(rpath):
                continue
            seq_map: dict[int, list[str]] = {}
            for key, text in parse_refined(rpath):
                seq = _seq_key(key)
                if seq >= 1:
                    seq_map.setdefault(seq, []).append(text)
            if not seq_map:
                problems.append(f"full mode: no sentences in {rname}")
                continue
            n_seq = max(seq_map)
            missing_seq = [k for k in range(1, n_seq + 1) if k not in seq_map]
            if missing_seq:
                problems.append(f"full mode {rname}: missing seq {missing_seq}")
            for seq in sorted(seq_map):
                texts.extend(seq_map[seq])
    else:
        refined_raw: list[tuple[str, str]] = []
        for part in plan.get("parts", []):
            rname = part["file"].replace("refine_part_", "refined_")
            rpath = os.path.join(parts_dir, rname)
            if os.path.exists(rpath):
                refined_raw.extend(parse_refined(rpath))
        refined = {}
        for key, text in refined_raw:
            try:
                idx = int(key)
            except ValueError:
                continue
            refined.setdefault(idx, []).append(text)
        got = set(refined.keys())
        not_refined = sorted(overlong_set - got)
        extra = sorted(got - overlong_set)
        if not_refined:
            problems.append(f"over-long lines with no refined output: {not_refined}")
        if extra:
            problems.append(f"refined output for non-over-long lines: {extra}")
        texts, _ = build_sentence_texts("b", lines, overlong_set, refined)

    if not texts:
        problems.append("no sentences to emit")

    analysis_path = os.path.join(workdir, "analysis.json")
    if os.path.exists(analysis_path):
        if problems:
            raise ValueError("; ".join(problems))
        from audio_first import load_analysis, attach_sources
        sources = attach_sources(texts, load_analysis(analysis_path))
        output = "\n".join(f"{i}\n{fmt_srt_time(src['start_ms'])} --> {fmt_srt_time(src['end_ms'])}\n{text}\n"
                           for i, (text, src) in enumerate(zip(texts, sources), 1))
        out_path = os.path.join(workdir, args.out)
        with open(out_path, "w", encoding="utf-8") as stream:
            stream.write(output)
        import hashlib
        with open(os.path.join(workdir, "refined_sources.json"), "w", encoding="utf-8") as stream:
            json.dump({"source_sha256": hashlib.sha256(output.encode()).hexdigest(), "events": sources}, stream, indent=2)
        log(f"Wrote {len(texts)} speaker-owned utterances -> {out_path}")
        return 0

    # global greedy match + anchor
    n_stream = len(stream)
    frag_starts = _frag_word_starts(stream)
    anchored: list[tuple[int, int, str]] = []
    pos = 0
    total_unmatched = 0
    for text in texts:
        words = text.split()  # SAME tokenization as the word stream (whitespace)
        if not words:
            continue
        if pos >= n_stream:
            # nothing left to anchor to — append with a degenerate window
            # (END is assigned in the pass below, so use the previous START)
            prev_start = anchored[-1][0] if anchored else 0
            anchored.append((prev_start, None, text.strip()))
            total_unmatched += len(words)
            continue
        start = _refined_start(stream, frags, frag_starts, pos)
        end_pos, unmatched = match_sentence(words, stream, pos)
        total_unmatched += unmatched
        if end_pos <= pos:  # nothing consumed (all reworded) — take word-count fallback
            end_pos = min(pos + len(words), n_stream)
            if end_pos <= pos:
                end_pos = pos + 1
        anchored.append((start, None, text.strip()))
        pos = end_pos

    if pos < n_stream and pos > 0:
        consumed = pos / n_stream
        if consumed < 0.9:
            problems.append(
                f"only {consumed:.0%} of source words consumed by sentences "
                f"({pos}/{n_stream}); sentences may be missing")

    if total_unmatched:
        problems.append(f"{total_unmatched} unmatched words: source word sequence must be preserved")
    if any(anchored[i][0] <= anchored[i-1][0] for i in range(1, len(anchored))):
        problems.append("Repeated/decreasing start timestamps: use aligned source analysis")
    if problems:
        log("VALIDATION PROBLEMS:")
        for p in problems[:40]:
            log(f"  - {p}")
        if len(problems) > 40:
            log(f"  ... and {len(problems) - 40} more")
        return 2

    # END = next START (de-overlap); last = stream end (or +2s for final line)
    if anchored:
        if n_stream:
            last_frag = stream[-1][1]
            stream_end = frags[last_frag][1] + 2000
        else:
            stream_end = lines[-1][1] if lines else 0
        for i in range(len(anchored) - 1):
            s, _, t = anchored[i]
            nxt_s = anchored[i + 1][0]
            if nxt_s <= s:
                nxt_s = s + 500
            anchored[i] = (s, nxt_s, t)
        s, _, t = anchored[-1]
        anchored[-1] = (s, max(stream_end, s + 500), t)

    # de-overlap global pass (defensive; also re-clip any line past its next START)
    for i in range(len(anchored) - 1):
        s, e, t = anchored[i]
        nxt_s = anchored[i + 1][0]
        if e > nxt_s:
            if nxt_s > s:
                anchored[i] = (s, nxt_s, t)
            else:
                anchored[i] = (s, s + 500, t)

    out_path = os.path.join(workdir, args.out)
    out = []
    for k, (s, e, t) in enumerate(anchored, start=1):
        out.append(f"{k}\n{fmt_srt_time(s)} --> {fmt_srt_time(e)}\n{t}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    wc = [len(t.split()) for _, _, t in anchored]
    log(f"Wrote {len(anchored)} lines -> {out_path}")
    log(f"Stats: source {len(lines)} lines, {n_stream} words -> {len(anchored)} "
        f"lines; max words {max(wc) if wc else 0}; "
        f"unmatched sentence words {total_unmatched}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare")
    p_prep.add_argument("sentence", help="sentence-level SRT from aggregate_srt.py")
    p_prep.add_argument("fragments", help="raw rolling-window ASR SRT")
    p_prep.add_argument("--workdir", default=".")
    p_prep.add_argument("--max-words", type=int, default=16,
                        help="re-split lines above this many words (default 16)")
    p_prep.add_argument("--max-dur-ms", type=int, default=6000,
                        help="re-split lines above this duration in ms (default 6000)")
    p_prep.add_argument("--lines-per-part", type=int, default=30,
                        help="max over-long lines per instruction chunk (default 30)")
    p_prep.add_argument("--context-lines", type=int, default=2,
                        help="tail lines of source shown as PREVIOUS CHUNK (default 2)")
    p_prep.add_argument("--manifest", default=None)
    p_prep.add_argument("--out", default="transcript_refined.srt")
    p_prep.set_defaults(func=cmd_prepare, mode="b")

    p_full = sub.add_parser("prepare-full")
    p_full.add_argument("sentence", help="sentence-level SRT from aggregate_srt.py")
    p_full.add_argument("fragments", help="raw rolling-window ASR SRT")
    p_full.add_argument("--workdir", default=".")
    p_full.add_argument("--max-words", type=int, default=16,
                        help="target cap words per sentence (default 16)")
    p_full.add_argument("--lines-per-part", type=int, default=40,
                        help="max source lines per instruction chunk (default 40)")
    p_full.add_argument("--context-lines", type=int, default=2,
                        help="tail lines of source shown as PREVIOUS CHUNK (default 2)")
    p_full.add_argument("--manifest", default=None)
    p_full.add_argument("--out", default="transcript_refined.srt")
    p_full.set_defaults(func=cmd_prepare_full, mode="full")

    p_ap = sub.add_parser("apply")
    p_ap.add_argument("--workdir", default=".")
    p_ap.add_argument("--out", default="transcript_refined.srt")
    p_ap.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    if getattr(args, "manifest", None) is None:
        args.manifest = os.path.join(args.workdir, "manifest.json")
    return args.func(args)


if __name__ == "__main__":
    from portable_io import configure_utf8
    configure_utf8()
    raise SystemExit(main())
