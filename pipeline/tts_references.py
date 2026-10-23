"""Choose auditable, same-speaker TTS references without changing speech units."""
import math
import os
import wave
from array import array
import sys
import subprocess

from pipeline.dubbing_plan import canonical_hash, file_sha256, known_speaker
from pipeline.speaker_analysis import _extract_audio_clip


REFERENCE_MODES = ("speaker", "segment")
REFERENCE_POLICY_VERSION = 1
MIN_REFERENCE_SECONDS = 2.0
MAX_REFERENCE_SECONDS = 10.0


def validate_reference_mode(value):
    if not isinstance(value, str) or value not in REFERENCE_MODES:
        raise ValueError("tts_reference_mode must be speaker or segment")
    return value


def _segment_bounds(segment, analysis, catalog, positions):
    """Only server-owned, contiguous words can authorize a source interval."""
    ids = segment.get("source_word_ids")
    if not isinstance(ids, list) or not ids or any(i not in catalog for i in ids):
        return None, "missing_source_words"
    order = [positions[i] for i in ids]
    if order != list(range(order[0], order[0] + len(order))):
        return None, "noncontiguous_source_words"
    words = [catalog[i] for i in ids]
    speaker = segment.get("speaker")
    if not known_speaker(speaker) or any(w.get("speaker") != speaker for w in words):
        return None, "mixed_or_unknown_speaker"
    if any(not w.get("reference_eligible", False) or w.get("overlap_speakers")
           or (analysis.get("verification_complete") and not w.get("speaker_verified")) for w in words):
        return None, "unverified_or_overlapping_source"
    if any(not isinstance(w.get(k), (int, float)) or not math.isfinite(w[k])
           for w in words for k in ("start", "end")):
        return None, "invalid_source_timing"
    if any(w["start"] < 0 or w["end"] <= w["start"] for w in words):
        return None, "invalid_source_timing"
    if any(b["start"] < a["start"] or b["end"] < a["end"] for a, b in zip(words, words[1:])):
        return None, "invalid_source_timing"
    start, full_end = words[0]["start"], words[-1]["end"]
    if any(t.get("speaker") != speaker and t["start"] < full_end and t["end"] > start
           for t in analysis.get("speaker_turns", [])):
        return None, "other_speaker_in_source_interval"
    # Bound reference conditioning cost, retaining word edges. The Chinese
    # synthesis unit itself is never split or shortened here.
    bounded = [w for w in words if w["end"] - start <= MAX_REFERENCE_SECONDS]
    if not bounded or bounded[-1]["end"] - start < MIN_REFERENCE_SECONDS:
        return None, "reference_too_short"
    return {"start": start, "end": bounded[-1]["end"],
            "source_word_ids": [w["word_id"] for w in bounded],
            "bounded": bounded[-1]["end"] < full_end}, None


def _audio_issue(path):
    """Reject silent or severely clipped PCM references; not a noise classifier."""
    with wave.open(path, "rb") as stream:
        if stream.getsampwidth() != 2 or stream.getcomptype() != "NONE":
            raise ValueError("Reference must be uncompressed PCM16 WAV")
        samples = array("h", stream.readframes(stream.getnframes()))
        rate, channels = stream.getframerate(), stream.getnchannels()
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) < MIN_REFERENCE_SECONDS * rate * channels:
        return "reference_too_short"
    rms = math.sqrt(sum(float(s) * s for s in samples) / len(samples)) / 32768
    if rms < 10 ** (-45 / 20):
        return "reference_silent_or_too_quiet"
    if sum(abs(s) >= 32760 for s in samples) / len(samples) > .02:
        return "reference_severely_clipped"
    return None


def build_reference_map(audio_path, analysis, segments, indices, pool, output_dir, mode="speaker"):
    """Return paths and a per-unit report; unsafe segments use their own speaker."""
    validate_reference_mode(mode)
    source_hash = file_sha256(audio_path) if mode == "segment" else None
    catalog = {word["word_id"]: word for word in analysis["words"]}
    positions = {word_id: i for i, word_id in enumerate(catalog)}
    paths, units = {}, []
    os.makedirs(output_dir, exist_ok=True)
    for index in indices:
        segment = segments[index]
        speaker = segment.get("speaker")
        if not known_speaker(speaker) or speaker not in pool:
            raise ValueError(f"Missing clean same-speaker reference for unit {index}")
        selected = dict(pool[speaker])
        actual_mode, reason = "speaker", None
        if mode == "segment":
            bounds, reason = _segment_bounds(segment, analysis, catalog, positions)
            if bounds is not None:
                identity = dict(policy=REFERENCE_POLICY_VERSION, source=source_hash,
                                analysis=analysis["analysis_id"], speaker=speaker, bounds=bounds)
                path = os.path.join(output_dir, "segment_" + canonical_hash(identity)[:24] + ".wav")
                try:
                    if not os.path.isfile(path):
                        _extract_audio_clip(audio_path, round(bounds["start"] * 1000),
                                            round(bounds["end"] * 1000), path)
                    reason = _audio_issue(path)
                except (OSError, EOFError, ValueError, wave.Error, subprocess.SubprocessError):
                    reason = "reference_extraction_failed"
                if reason is None:
                    selected = {**bounds, "speaker": speaker, "path": path, "sha256": file_sha256(path)}
                    actual_mode = "segment"
        paths[index] = selected["path"]
        units.append({**selected, "segment_index": index, "unit_id": segment.get("unit_id"),
                      "requested_mode": mode, "actual_mode": actual_mode, "fallback_reason": reason})
    return paths, {"schema_version": 1, "policy_version": REFERENCE_POLICY_VERSION,
                   "mode": mode, "units": units,
                   "fallback_count": sum(bool(u["fallback_reason"]) for u in units)}
