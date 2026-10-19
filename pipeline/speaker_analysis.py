"""Independent, reusable source analysis for externally refined subtitles."""
import json
import math
import os
from pipeline.dubbing_plan import file_sha256, known_speaker, canonical_hash


def build_analysis(transcription, media_hash, speaker_turns=None):
    words = []
    for segment in transcription.get("segments", []):
        for item in segment.get("words", []):
            start, end = item.get("start"), item.get("end")
            speaker = item.get("speaker") or segment.get("speaker")
            if speaker_turns is not None and start is not None and end is not None:
                overlaps = {}
                for turn in speaker_turns:
                    overlap = max(0, min(end, turn["end"]) - max(start, turn["start"]))
                    overlaps[turn["speaker"]] = overlaps.get(turn["speaker"], 0) + overlap
                overlaps = {s: amount for s, amount in overlaps.items() if amount > 0}
                if not overlaps:
                    raise ValueError(f"No speaker at source word {start:.2f}s")
                speaker = max(overlaps, key=overlaps.get)
            if start is None or end is None:
                raise ValueError("Source contains unaligned words; resolve alignment before refining")
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)) or end <= start:
                raise ValueError("Invalid source word timing")
            if not known_speaker(speaker):
                raise ValueError("Speaker diarization required; source contains unresolved words")
            words.append({"word_id": len(words), "text": item.get("word", "").strip(),
                          "start": start, "end": end, "speaker": speaker})
    if not words or any(words[i]["start"] < words[i-1]["start"] for i in range(1, len(words))):
        raise ValueError("Missing/unordered word-level source analysis")
    turns = []
    for word in words:
        if not turns or turns[-1]["speaker"] != word["speaker"] or word["start"] - turns[-1]["end"] > 3:
            turns.append({"turn_id": f"turn_{len(turns)+1:05d}", "speaker": word["speaker"],
                          "start": word["start"], "end": word["end"], "word_ids": [], "text": ""})
        turn = turns[-1]
        turn["end"] = max(turn["end"], word["end"])
        turn["word_ids"].append(word["word_id"])
        turn["text"] = (turn["text"] + " " + word["text"]).strip()
    # A candidate must contain a single identified voice throughout its source
    # words and must not intersect another speaker's aligned speech.
    references = {}
    for turn in turns:
        # Search contiguous speech runs, rather than reject an entire long turn
        # because it contains one natural pause.
        runs, current = [], []
        for word_id in turn["word_ids"]:
            word = words[word_id]
            native = speaker_turns if speaker_turns is not None else words
            contaminated = any(t["speaker"] != turn["speaker"] and t["start"] < word["end"] and t["end"] > word["start"] for t in native)
            interrupted = current and any(t["speaker"] != turn["speaker"] and t["start"] < word["start"] and t["end"] > current[-1]["end"] for t in native)
            if contaminated or interrupted or (current and word["start"]-current[-1]["end"] > .8):
                if current:
                    runs.append(current)
                current = []
            if not contaminated:
                current.append(word)
        if current:
            runs.append(current)
        for run in runs:
            start = run[0]["start"]
            owned = [w for w in run if w["end"] <= start + 10]
            if not owned:
                continue
            end = owned[-1]["end"]
            if end - start < 2:
                continue
            if any(w["speaker"] != turn["speaker"] and w["start"] < end and w["end"] > start
                   for w in (speaker_turns if speaker_turns is not None else words)):
                continue
            candidate = {"speaker": turn["speaker"], "start": start, "end": end,
                         "text": " ".join(w["text"] for w in owned), "analysis_verified": True}
            previous = references.get(turn["speaker"])
            if previous is None or end-start > previous["end"]-previous["start"]:
                references[turn["speaker"]] = candidate
    sentences = []
    for word in words:
        if not sentences or sentences[-1]["speaker"] != word["speaker"] or sentences[-1]["text"].endswith((".", "!", "?")):
            sentences.append({"speaker": word["speaker"], "start": word["start"], "end": word["end"],
                              "text": "", "unit_id": f"source_{len(sentences)+1}", "boundary_after": "sentence_end"})
        sentence = sentences[-1]
        sentence["text"] = (sentence["text"]+" "+word["text"]).strip()
        sentence["end"] = max(sentence["end"], word["end"])
    value = {"schema_version": 1, "analysis_revision": 3, "source_media_sha256": media_hash,
             "words": words, "turns": turns, "references": references,
             "speaker_turns": speaker_turns or [], "sentences": sentences}
    value["analysis_id"] = canonical_hash(value)
    return value


def analyze_source(audio_path, media_path, output_dir, task_id=None):
    from pipeline.step1_transcribe import transcribe
    from core import config
    import subprocess
    import sys
    os.makedirs(output_dir, exist_ok=True)
    media_hash = file_sha256(media_path)
    audio_hash = file_sha256(audio_path)
    cache = os.path.join(output_dir, "speaker_analysis.json")
    if os.path.isfile(cache):
        with open(cache, encoding="utf-8") as stream:
            old = json.load(stream)
        if old.get("source_media_sha256") == media_hash and old.get("source_audio_sha256") == audio_hash and old.get("speaker_turns") and old.get("analysis_revision") == 3:
            return old
    # One ASR/alignment pass, one native diarization pass; keep overlap evidence.
    transcription = transcribe(audio_path, output_dir=output_dir, diarize=False)
    turns_path = os.path.join(output_dir, "speaker_turns.json")
    env = os.environ.copy()
    if getattr(config, "WHISPERX_HF_TOKEN", ""):
        env["HF_TOKEN"] = config.WHISPERX_HF_TOKEN
    worker = os.path.join(config.BASE_DIR, "workers", "analyze_speakers.py")
    python = getattr(config, "WHISPERX_PYTHON", "") or sys.executable
    proc = subprocess.Popen([python, worker, audio_path, turns_path], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    if task_id:
        from core.task_manager import task_subprocesses
        task_subprocesses[task_id] = proc
    try:
        _, err = proc.communicate(timeout=7200)
        if proc.returncode:
            raise ValueError("Speaker analysis failed: " + err[-1500:])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        if task_id:
            task_subprocesses.pop(task_id, None)
    with open(turns_path) as stream:
        speaker_turns = json.load(stream)
    result = build_analysis(transcription, media_hash, speaker_turns)
    result["source_audio_sha256"] = audio_hash
    with open(cache, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return result


def extract_reference_pool(audio_path, analysis, output_dir):
    from pipeline.ref_audio_utils import _extract_audio_clip
    os.makedirs(output_dir, exist_ok=True)
    pool = {}
    for speaker, ref in analysis["references"].items():
        # Speaker labels originate in the analyzer, not filenames supplied by a client.
        name = canonical_hash({"speaker": speaker, "ref": ref, "analysis": analysis["analysis_id"]})[:24]
        path = os.path.join(output_dir, f"reference_{name}.wav")
        if not os.path.isfile(path):
            _extract_audio_clip(audio_path, int(ref["start"]*1000), int(ref["end"]*1000), path)
        pool[speaker] = {**ref, "path": path, "sha256": file_sha256(path)}
    return pool
