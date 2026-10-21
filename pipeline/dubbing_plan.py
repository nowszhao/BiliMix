"""Versioned audio-first input contract. Text decisions belong to the translator.

Source IDs and speaker ownership are checked against server-owned analysis;
display captions never create additional synthesis jobs.
"""
import hashlib
import json
import math


PROTOCOL_VERSION = 1


def file_sha256(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def known_speaker(value):
    return isinstance(value, str) and bool(value) and not value.lower().startswith(
        ("spk_auto_", "spk_unknown", "unknown"))


def validate_manifest(manifest, analysis):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != PROTOCOL_VERSION:
        raise ValueError("Unsupported dubbing manifest schema_version")
    if manifest.get("source_media_sha256") != analysis.get("source_media_sha256"):
        raise ValueError("Dubbing manifest belongs to a different source media")
    if manifest.get("analysis_id") != analysis.get("analysis_id"):
        raise ValueError("Dubbing manifest analysis_id mismatch")
    from pipeline.speaker_analysis import ensure_dubbing_ready
    ensure_dubbing_ready(analysis)
    catalog = {w["word_id"]: w for w in analysis["words"]}
    expected = list(catalog)
    used, output, ids = [], [], set()
    units = manifest.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError("No complete dubbing units")
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError("Each unit must be an object")
        uid = unit.get("unit_id")
        if not isinstance(uid, str) or not uid or uid in ids:
            raise ValueError("Missing/duplicate unit_id")
        ids.add(uid)
        word_ids = unit.get("source_word_ids")
        if not isinstance(word_ids, list) or not word_ids:
            raise ValueError(f"{uid}: source_word_ids required")
        if any(w not in catalog for w in word_ids):
            raise ValueError(f"{uid}: unknown source word")
        words = [catalog[w] for w in word_ids]
        speaker = unit.get("speaker_id")
        if not known_speaker(speaker) or any(w["speaker"] != speaker for w in words):
            raise ValueError(f"{uid}: unresolved/mixed speaker; split at real speaker change")
        text = unit.get("text_zh")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{uid}: complete Chinese text required")
        # A bounded engine request, not an arbitrary 16-English-word boundary.
        if len(text) > 1500:
            raise ValueError(f"{uid}: split at a natural clause boundary (max 1500 chars)")
        captions = unit.get("captions") or []
        if captions:
            if not isinstance(captions, list) or any(not isinstance(c, dict) for c in captions):
                raise ValueError(f"{uid}: invalid captions")
            if "".join(c.get("text_zh", "") for c in captions) != text:
                raise ValueError(f"{uid}: display captions must exactly cover Chinese text")
        boundary = unit.get("boundary_after", "sentence_end")
        if boundary not in ("continuation", "sentence_end", "turn_end"):
            raise ValueError(f"{uid}: unknown boundary_after")
        output.append({"unit_id": uid, "speaker": speaker,
                       "start": words[0]["start"], "end": words[-1]["end"],
                       "text": unit.get("text_en", " ".join(w["text"] for w in words)),
                       "text_zh": text.strip(), "source_word_ids": word_ids,
                       "boundary_after": boundary, "captions": captions})
        used.extend(word_ids)
    if used != expected:
        raise ValueError("Source word coverage/order changed: missing, duplicated or reordered dialogue")
    return output


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()
