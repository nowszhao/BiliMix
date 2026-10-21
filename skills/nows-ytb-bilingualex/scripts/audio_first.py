"""Source-word ownership helpers shared by refinement and manifest assembly."""
import json
from pathlib import Path
import re


def tokens(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def load_analysis(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    value = value.get("result", {}).get("analysis", value.get("analysis", value))
    if value.get("schema_version") != 1 or not value.get("words") or not value.get("analysis_id"):
        raise ValueError("Valid protocol-v1 source analysis required")
    return value


def attach_sources(texts, analysis):
    expected, owners = [], []
    for word in analysis["words"]:
        ts = tokens(word["text"])
        if not ts:
            raise ValueError(f"Nonlexical source word {word['word_id']}: inspect source alignment")
        expected.extend(ts)
        owners.extend([word] * len(ts))
    actual = [w for text in texts for w in tokens(text)]
    if actual != expected:
        raise ValueError("Refined word sequence differs from source analysis")
    pos, used, result = 0, [], []
    for text in texts:
        n = len(tokens(text))
        if n == 0:
            raise ValueError("Empty refined utterance")
        words = owners[pos:pos+n]
        word_ids = list(dict.fromkeys(w["word_id"] for w in words))
        speakers = {w["speaker"] for w in words}
        if len(speakers) != 1:
            raise ValueError("Refined utterance crosses a real speaker change; keep each person's complete speech separate")
        result.append({"speaker_id": next(iter(speakers)), "source_word_ids": word_ids,
                       "start_ms": round(words[0]["start"]*1000),
                       "end_ms": round(words[-1]["end"]*1000)})
        used.extend(word_ids)
        pos += n
    if used != [w["word_id"] for w in analysis["words"]]:
        raise ValueError("A source word was split across utterances or coverage changed")
    return result


def canonical_rows(analysis, max_words=80):
    """Bound translator context rows, without creating synthesis boundaries."""
    catalog = {w['word_id']:w for w in analysis['words']}
    rows = []
    for turn in analysis['turns']:
        ids = turn['word_ids']
        for offset in range(0,len(ids),max_words):
            owned = [catalog[i] for i in ids[offset:offset+max_words]]
            rows.append({'start':owned[0]['start'],'end':owned[-1]['end'],
                         'text':' '.join(w['text'] for w in owned),
                         'speaker':turn['speaker'],'turn_id':turn['turn_id']})
    return rows
