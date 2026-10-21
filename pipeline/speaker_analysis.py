"""Independent, reusable source analysis for externally refined subtitles."""
import json
import math
import os
from pipeline.dubbing_plan import file_sha256, known_speaker, canonical_hash


def build_analysis(transcription, media_hash, speaker_turns=None, verification=None):
    words = []
    for segment in transcription.get("segments", []):
        for item in segment.get("words", []):
            start, end = item.get("start"), item.get("end")
            speaker = item.get("speaker") or segment.get("speaker")
            assignment = "overlap" if speaker_turns is not None else "upstream"
            if speaker_turns is not None and start is not None and end is not None:
                overlaps = {}
                for turn in speaker_turns:
                    overlap = max(0, min(end, turn["end"]) - max(start, turn["start"]))
                    overlaps[turn["speaker"]] = overlaps.get(turn["speaker"], 0) + overlap
                overlaps = {s: amount for s, amount in overlaps.items() if amount > 0}
                if not overlaps:
                    # ASR and native VAD clocks differ slightly at word edges.
                    # This is a 20ms boundary tolerance, never a gap-based voice guess.
                    nearby = {turn['speaker'] for turn in speaker_turns
                              if max(0,turn['start']-end,start-turn['end']) <= .02}
                    if len(nearby) != 1:
                        raise ValueError(f"No unique speaker at source word {start:.2f}s")
                    speaker = next(iter(nearby))
                    assignment = "boundary_tolerance"
                else:
                    speaker = max(overlaps, key=overlaps.get)
            if start is None or end is None:
                raise ValueError("Source contains unaligned words; resolve alignment before refining")
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)) or end <= start:
                raise ValueError("Invalid source word timing")
            if not known_speaker(speaker):
                raise ValueError("Speaker diarization required; source contains unresolved words")
            words.append({"word_id": len(words), "text": item.get("word", "").strip(),
                          "start": start, "end": end, "speaker": speaker,
                          "speaker_assignment": assignment})
    if not words or any(words[i]["start"] < words[i-1]["start"] for i in range(1, len(words))):
        raise ValueError("Missing/unordered word-level source analysis")
    if verification is not None:
        from pipeline.speaker_verification import verified_word_assignments
        decisions = verified_word_assignments({"words":words},verification)
        for word in words:
            word.update(decisions.get(word['word_id'],{"speaker_verified":False}))
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
            contaminated = (verification is not None and not word.get("speaker_verified")) or any(t["speaker"] != turn["speaker"] and t["start"] < word["end"] and t["end"] > word["start"] for t in native)
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
    value = {"schema_version": 1, "analysis_revision": 5, "source_media_sha256": media_hash,
             "words": words, "turns": turns, "references": references,
             "speaker_turns": speaker_turns or [], "sentences": sentences}
    value["analysis_id"] = canonical_hash(value)
    return value


def native_analysis_signature(audio_hash, worker):
    from pathlib import Path
    from importlib.metadata import version, PackageNotFoundError
    try:
        runtime = version("pyannote.audio")
    except PackageNotFoundError:
        runtime = "unavailable"
    from pipeline.model_runtime import model_snapshots
    revisions = [str(p) for p in model_snapshots()]
    return canonical_hash({"schema":1,"audio_sha256":audio_hash,
        "worker_sha256":file_sha256(worker),"runtime":runtime,
        "runtime_helper":file_sha256(os.path.join(os.path.dirname(worker),"..","pipeline","model_runtime.py")),"cached_revisions":revisions,
        "model":os.environ.get("BILIMIX_DIARIZATION_MODEL","pyannote/speaker-diarization-community-1")})


def _native_speaker_turns(audio_path, output_dir, signature, task_id=None):
    from core import config
    import subprocess
    import sys
    turns_path = os.path.join(output_dir, "speaker_turns.json")
    meta_path = os.path.join(output_dir, "speaker_turns.meta.json")
    if os.path.isfile(turns_path) and os.path.isfile(meta_path):
        try:
            with open(meta_path) as stream:
                meta = json.load(stream)
            if meta.get("signature") == signature and meta.get("turns_sha256") == file_sha256(turns_path):
                with open(turns_path) as stream:
                    return json.load(stream)
        except (OSError,ValueError,TypeError):
            pass
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
        turns = json.load(stream)
    # Save before word binding: an alignment/binding error must not rerun the model.
    with open(meta_path,"w") as stream:
        json.dump({"signature":signature,"turns_sha256":file_sha256(turns_path)},stream)
    return turns


def verification_signature(native_signature):
    from core import config
    return canonical_hash({"native":native_signature,
        "worker":file_sha256(os.path.join(config.BASE_DIR,"workers","verify_speakers.py")),
        "analyzer":file_sha256(__file__),
        "policy":file_sha256(os.path.join(config.BASE_DIR,"pipeline","speaker_verification.py"))})


def _verify_source_speakers(audio_path, transcription, initial, output_dir, signature, task_id=None):
    from core import config
    import subprocess,sys
    evidence_path=os.path.join(output_dir,"speaker_verification.json")
    meta_path=evidence_path+".meta.json"
    if os.path.isfile(evidence_path) and os.path.isfile(meta_path):
        try:
            with open(meta_path) as stream:meta=json.load(stream)
            if meta.get("signature")==signature and meta.get("sha256")==file_sha256(evidence_path):
                with open(evidence_path) as stream:return json.load(stream)
        except (OSError,ValueError,TypeError):pass
    asr_path=os.path.join(output_dir,"verification_asr.json")
    initial_path=os.path.join(output_dir,"speaker_analysis_initial.json")
    for path,value in [(asr_path,transcription),(initial_path,initial)]:
        with open(path,"w") as stream:json.dump(value,stream,ensure_ascii=False)
    env=os.environ.copy()
    if getattr(config,"WHISPERX_HF_TOKEN",""):env["HF_TOKEN"]=config.WHISPERX_HF_TOKEN
    worker=os.path.join(config.BASE_DIR,"workers","verify_speakers.py")
    python=getattr(config,"WHISPERX_PYTHON","") or sys.executable
    proc=subprocess.Popen([python,worker,audio_path,asr_path,initial_path,evidence_path],
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
    if task_id:
        from core.task_manager import task_subprocesses,get_task,update_task
        task_subprocesses[task_id]=proc
        if (get_task(task_id) or {}).get("type")=="analysis":
            update_task(task_id,step="verify_speakers",progress=80,message="正在按原声声纹复核完整话语和参考声线...")
    try:
        _,err=proc.communicate(timeout=7200)
        if proc.returncode:raise ValueError("Acoustic speaker verification failed: "+err[-1500:])
    finally:
        if proc.poll() is None:proc.kill();proc.communicate()
        if task_id:task_subprocesses.pop(task_id,None)
    with open(evidence_path) as stream:evidence=json.load(stream)
    with open(meta_path,"w") as stream:json.dump({"signature":signature,"sha256":file_sha256(evidence_path)},stream)
    return evidence


def analyze_source(audio_path, media_path, output_dir, task_id=None):
    from pipeline.step1_transcribe import transcribe
    from core import config
    os.makedirs(output_dir, exist_ok=True)
    media_hash = file_sha256(media_path)
    audio_hash = file_sha256(audio_path)
    worker = os.path.join(config.BASE_DIR, "workers", "analyze_speakers.py")
    signature = native_analysis_signature(audio_hash,worker)
    acoustic_signature = verification_signature(signature)
    cache = os.path.join(output_dir, "speaker_analysis.json")
    if os.path.isfile(cache):
        with open(cache, encoding="utf-8") as stream:
            old = json.load(stream)
        if old.get("source_media_sha256") == media_hash and old.get("source_audio_sha256") == audio_hash and old.get("speaker_turns") and old.get("analysis_revision") == 5 and old.get("native_analysis_signature") == signature and old.get("verification_signature") == acoustic_signature:
            return old
    transcription = transcribe(audio_path, output_dir=output_dir, diarize=False)
    speaker_turns = _native_speaker_turns(audio_path,output_dir,signature,task_id)
    initial = build_analysis(transcription, media_hash, speaker_turns)
    evidence = _verify_source_speakers(audio_path,transcription,initial,output_dir,acoustic_signature,task_id)
    result = build_analysis(transcription, media_hash, speaker_turns,verification=evidence)
    result["verification_signature"] = acoustic_signature
    result["speaker_verification_summary"] = {
        "windows":len(evidence["units"]),
        "verified_words":sum(bool(w.get("speaker_verified")) for w in result["words"]),
        "corrected_words":sum(a["speaker"]!=b["speaker"] for a,b in zip(initial["words"],result["words"]))}
    result["source_audio_sha256"] = audio_hash
    result["native_analysis_signature"] = signature
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
