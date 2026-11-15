"""Word/character timings for a synthesized utterance.

Synthesis starts from text, so there is no source timeline to reuse. This
module reads the generated WAV back with the project ASR stack and maps the
recognized characters onto the text the caller sent, so every timing indexes
that text. Recognized text is returned alongside the mapping: a mismatch is
reported as reduced coverage, never silently patched into a plausible timing.
"""
import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CONTENT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaffA-Za-z0-9]")
_WORD = re.compile(r"[^\s]+")

LOW_COVERAGE = 0.6


def _probe_duration(path):
    from pipeline.step1_transcribe import _probe_audio_duration
    return _probe_audio_duration(path)


def _tokens(transcription):
    """Flatten ASR words in time order; segments without words are ignored."""
    tokens = []
    for segment in transcription.get("segments", []):
        for item in segment.get("words", []):
            text = (item.get("word") or "").strip()
            start, end = item.get("start"), item.get("end")
            if not text or start is None or end is None or end < start:
                continue
            tokens.append({"text": text, "start": float(start), "end": float(end),
                           "score": item.get("score")})
    tokens.sort(key=lambda token: token["start"])
    return tokens


def _char_timeline(tokens):
    """Expand tokens into characters; characters of one token share its span.

    Chinese clarification models emit one to three characters per token. Even
    split is an estimate inside the token and always resolves to real audio
    boundaries at token edges.
    """
    timeline = []
    for token in tokens:
        chars = [ch for ch in token["text"] if _CONTENT.match(ch)]
        if not chars:
            continue
        span = token["end"] - token["start"]
        step = span / len(chars)
        for position, ch in enumerate(chars):
            timeline.append({
                "text": ch,
                "start": round(token["start"] + step * position, 3),
                "end": round(token["start"] + step * (position + 1), 3),
                "score": token.get("score"),
            })
    return timeline


def _input_positions(text, unit):
    """Content spans of the input text: characters, or whitespace words."""
    if unit == "char":
        return [(index, text[index]) for index, ch in enumerate(text)
                if _CONTENT.match(ch)]
    spans = []
    for match in _WORD.finditer(text):
        word = "".join(ch for ch in match.group(0) if _CONTENT.match(ch))
        if word:
            spans.append((match.start(), match.end(), word))
    return spans


def _align_chars(text, tokens):
    import difflib

    timeline = _char_timeline(tokens)
    asr_chars = "".join(item["text"] for item in timeline).lower()
    positions = _input_positions(text, "char")
    input_chars = "".join(ch for _, ch in positions).lower()

    matched = {}
    if asr_chars and input_chars:
        matcher = difflib.SequenceMatcher(a=asr_chars, b=input_chars, autojunk=False)
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                matched[block.b + offset] = timeline[block.a + offset]

    items = []
    for index, (position, ch) in enumerate(positions):
        timing = matched.get(index)
        items.append({
            "index": index,
            "text": text[position],
            "char_start": position,
            "char_end": position + 1,
            "start": timing["start"] if timing else None,
            "end": timing["end"] if timing else None,
            "score": timing.get("score") if timing else None,
        })
    return items, asr_chars, len(positions), len(matched)


def _align_words(text, tokens):
    import difflib

    asr_words = []
    for token in tokens:
        word = "".join(ch for ch in token["text"] if _CONTENT.match(ch))
        if word:
            asr_words.append({**token, "compare": word.lower()})

    spans = _input_positions(text, "word")
    input_words = [word.lower() for _, _, word in spans]

    matched = {}
    if asr_words and input_words:
        matcher = difflib.SequenceMatcher(
            a=[item["compare"] for item in asr_words], b=input_words, autojunk=False)
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                matched[block.b + offset] = asr_words[block.a + offset]

    items = []
    for index, (start, end, word) in enumerate(spans):
        timing = matched.get(index)
        items.append({
            "index": index,
            "text": text[start:end],
            "char_start": start,
            "char_end": end,
            "start": round(timing["start"], 3) if timing else None,
            "end": round(timing["end"], 3) if timing else None,
            "score": timing.get("score") if timing else None,
        })
    return items, " ".join(item["text"] for item in asr_words), len(spans), len(matched)


def align_to_text(text, tokens, language="zh"):
    """Return timings that index `text` plus an honest coverage report."""
    unit = "char" if language in ("zh", "ja") else "word"
    if unit == "char":
        items, asr_text, total, matched = _align_chars(text, tokens)
    else:
        items, asr_text, total, matched = _align_words(text, tokens)

    coverage = round(matched / total, 4) if total else 0.0
    warnings = []
    if not tokens:
        warnings.append("asr_empty")
    if total and matched < total:
        warnings.append(f"unmatched_units:{total - matched}")
    if total and coverage < LOW_COVERAGE:
        warnings.append(f"low_coverage:{coverage}")
    return {
        "version": 1,
        "unit": unit,
        "source": "asr_align",
        "language": language,
        "text": text,
        "asr_text": asr_text,
        "coverage": coverage,
        "items": items,
        "warnings": warnings,
    }


def analyze_words(audio_path, text, language="zh", work_dir=None):
    """Transcribe one synthesized WAV and align it to the requested text.

    WhisperX artifacts stay in `work_dir` so a retry reuses its own cache; the
    returned report is independent of that cache.
    """
    import os

    from pipeline.step1_transcribe import transcribe

    work_dir = work_dir or os.path.join(os.path.dirname(audio_path), "words")
    os.makedirs(work_dir, exist_ok=True)
    transcription = transcribe(audio_path, output_dir=work_dir,
                               diarize=False, language=language)
    report = align_to_text(text, _tokens(transcription), language)
    report["duration"] = round(_probe_duration(audio_path), 3)
    return report
