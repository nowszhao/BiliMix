"""Independent, reusable source analysis for externally refined subtitles."""
import json
from core.files import atomic_json
import math
import os
from pipeline.dubbing_plan import file_sha256, known_speaker, canonical_hash


ANALYSIS_REVISION = 6


def _project_word(start, end, turns):
    """Return evidence and an optional owner; a missing owner is reviewable data."""
    overlaps = {}
    for turn in turns:
        overlap = max(0, min(end, turn["end"]) - max(start, turn["start"]))
        if overlap > 0 and known_speaker(turn["speaker"]):
            overlaps[turn["speaker"]] = overlaps.get(turn["speaker"], 0) + overlap
    candidates = sorted(overlaps, key=lambda speaker: (-overlaps[speaker], speaker))
    if candidates:
        winner = candidates[0]
        dominant = overlaps[winner] / sum(overlaps.values())
        owner = winner if len(candidates) == 1 or dominant >= .6 else None
        return dict(speaker=owner, speaker_candidates=candidates,
                    speaker_assignment="overlap" if owner else "ambiguous_overlap",
                    overlap_speakers=candidates if len(candidates) > 1 else [],
                    reference_eligible=len(candidates) == 1 and overlaps[winner] >= .8 * (end-start))
    nearby = sorted({turn["speaker"] for turn in turns
                     if known_speaker(turn["speaker"]) and
                     max(0, turn["start"]-end, start-turn["end"]) <= .02})
    if len(nearby) == 1:
        return dict(speaker=nearby[0], speaker_candidates=nearby,
                    speaker_assignment="boundary_tolerance", overlap_speakers=[], reference_eligible=False)
    previous = max((turn for turn in turns if turn["end"] <= start),
                   key=lambda turn: turn["end"], default=None)
    following = min((turn for turn in turns if turn["start"] >= end),
                    key=lambda turn: turn["start"], default=None)
    candidates = sorted({turn["speaker"] for turn in (previous, following)
                         if turn and known_speaker(turn["speaker"])})
    # Only bridge a small word-edge discrepancy inside the same identified voice.
    bridge = (not nearby and previous and following and len(candidates) == 1
              and following["start"]-previous["end"] <= 2
              and min(start-previous["end"], following["start"]-end) <= .05)
    return dict(speaker=candidates[0] if bridge else None, speaker_candidates=nearby or candidates,
                speaker_assignment="gap_bridge_unanimous" if bridge else "unresolved_gap",
                overlap_speakers=[], reference_eligible=False)


def _group_words(words):
    turns, sentences = [], []
    for word in words:
        if (not turns or turns[-1]["speaker"] != word["speaker"]
                or word["start"]-turns[-1]["end"] > 3):
            turns.append(dict(turn_id=f"turn_{len(turns)+1:05d}", speaker=word["speaker"],
                              start=word["start"], end=word["end"], word_ids=[], text=""))
        turn = turns[-1]
        turn["end"] = max(turn["end"], word["end"])
        turn["word_ids"].append(word["word_id"])
        turn["text"] = (turn["text"] + " " + word["text"]).strip()
        if (not sentences or sentences[-1]["speaker"] != word["speaker"]
                or sentences[-1]["text"].endswith((".", "!", "?"))):
            sentences.append(dict(speaker=word["speaker"], start=word["start"], end=word["end"],
                                  text="", unit_id=f"source_{len(sentences)+1}", boundary_after="sentence_end"))
        sentence = sentences[-1]
        sentence["text"] = (sentence["text"] + " " + word["text"]).strip()
        sentence["end"] = max(sentence["end"], word["end"])
    return turns, sentences


def _select_references(words, turns, native, require_verified):
    references = {}
    for turn in turns:
        if not known_speaker(turn["speaker"]):
            continue
        runs, current = [], []
        for word_id in turn["word_ids"]:
            word = words[word_id]
            contaminated = (not word["reference_eligible"]
                            or (require_verified and not word["speaker_verified"])
                            or any(t["speaker"] != turn["speaker"] and t["start"] < word["end"]
                                   and t["end"] > word["start"] for t in native))
            interrupted = current and any(t["speaker"] != turn["speaker"] and t["start"] < word["start"]
                                          and t["end"] > current[-1]["end"] for t in native)
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
            owned = [word for word in run if word["end"] <= start+10]
            if not owned or owned[-1]["end"]-start < 2:
                continue
            end = owned[-1]["end"]
            if any(t["speaker"] != turn["speaker"] and t["start"] < end and t["end"] > start for t in native):
                continue
            candidate = dict(speaker=turn["speaker"], start=start, end=end,
                             text=" ".join(w["text"] for w in owned), analysis_verified=require_verified)
            previous = references.get(turn["speaker"])
            if previous is None or end-start > previous["end"]-previous["start"]:
                references[turn["speaker"]] = candidate
    return references


def _assemble_analysis(words, media_hash, native, require_verified):
    turns, sentences = _group_words(words)
    references = _select_references(words, turns, native if native is not None else words, require_verified)
    unresolved = []
    for word in words:
        word["speaker_status"] = "assigned" if known_speaker(word["speaker"]) else "unresolved"
        if word["speaker_status"] != "unresolved":
            continue
        if (not unresolved or word["word_id"] != unresolved[-1]["word_ids"][-1]+1
                or word["start"]-unresolved[-1]["end"] > .8):
            unresolved.append(dict(start=word["start"], end=word["end"], text="", word_ids=[],
                                   speaker_candidates=[], reason=word["speaker_assignment"]))
        issue = unresolved[-1]
        issue["end"] = max(issue["end"], word["end"])
        issue["text"] = (issue["text"] + " " + word["text"]).strip()
        issue["word_ids"].append(word["word_id"])
        issue["speaker_candidates"] = sorted(set(issue["speaker_candidates"]) | set(word["speaker_candidates"]))
    expected = {word["speaker"] for word in words if known_speaker(word["speaker"])}
    expected.update(s for word in words for s in word["speaker_candidates"] if known_speaker(s))
    missing = sorted(expected - references.keys())
    value = dict(schema_version=1, analysis_revision=ANALYSIS_REVISION, source_media_sha256=media_hash,
                 words=words, turns=turns, references=references, speaker_turns=native or [], sentences=sentences,
                 verification_complete=require_verified, unresolved_segments=unresolved,
                 missing_reference_speakers=missing, dubbing_ready=not unresolved and not missing,
                 speaker_verification_summary=dict(
                     verified_words=sum(bool(w["speaker_verified"]) for w in words),
                     unresolved_words=sum(not known_speaker(w["speaker"]) for w in words),
                     unresolved_segments=len(unresolved), missing_reference_speakers=missing))
    value["analysis_id"] = canonical_hash(value)
    return value


def build_analysis(transcription, media_hash, speaker_turns=None, verification=None):
    words = []
    for segment in transcription.get("segments", []):
        for item in segment.get("words", []):
            start, end = item.get("start"), item.get("end")
            if start is None or end is None:
                raise ValueError("Source contains unaligned words; resolve alignment before refining")
            if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                        for v in (start, end)) or start < 0 or end <= start):
                raise ValueError("Invalid source word timing")
            if speaker_turns is not None:
                projection = _project_word(start, end, speaker_turns)
            else:
                speaker = item.get("speaker") or segment.get("speaker")
                speaker = speaker if known_speaker(speaker) else None
                projection = dict(speaker=speaker, speaker_candidates=[speaker] if speaker else [],
                                  speaker_assignment="upstream" if speaker else "unresolved_upstream",
                                  overlap_speakers=[], reference_eligible=bool(speaker))
            words.append(dict(word_id=len(words), text=item.get("word", "").strip(), start=start, end=end,
                              speaker_verified=False, **projection))
    if not words or any(words[i]["start"] < words[i-1]["start"] for i in range(1, len(words))):
        raise ValueError("Missing/unordered word-level source analysis")
    if verification is not None:
        from pipeline.speaker_verification import verified_word_assignments
        decisions = verified_word_assignments({"words": words}, verification)
        for word in words:
            word.update(decisions.get(word["word_id"], {}))
    return _assemble_analysis(words, media_hash, speaker_turns, verification is not None)


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
    import subprocess,sys,tempfile,threading
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
    errors = tempfile.TemporaryFile(mode="w+t")
    proc=subprocess.Popen([python,worker,audio_path,asr_path,initial_path,evidence_path],
                          stdout=subprocess.PIPE,stderr=errors,text=True,env=env)
    if task_id:
        from core.task_manager import task_subprocesses,get_task,update_task
        task_subprocesses[task_id]=proc
        if (get_task(task_id) or {}).get("type")=="analysis":
            update_task(task_id,step="verify_speakers",progress=80,message="正在按原声声纹复核完整话语和参考声线...")
    expired = threading.Event()
    def expire():
        expired.set()
        if proc.poll() is None:
            proc.kill()
    timer = threading.Timer(7200, expire)
    timer.daemon = True
    timer.start()
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
                completed, total = event.get("completed"), event.get("total")
                if (task_id and event.get("event") == "speaker_verification"
                        and isinstance(total, int) and total > 0 and isinstance(completed, int)
                        and 0 <= completed <= total and (get_task(task_id) or {}).get("type") == "analysis"):
                    update_task(task_id, step="verify_speakers", progress=80+int(15*completed/total),
                                message=f"正在声纹复核：{completed}/{total} 个话语窗口")
            except (ValueError, AttributeError):
                continue
        proc.wait()
        errors.seek(0)
        err = errors.read()[-1500:]
        if expired.is_set():
            raise TimeoutError("Acoustic speaker verification timed out")
        if proc.returncode:
            raise ValueError("Acoustic speaker verification failed: " + err)
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        errors.close()
        if task_id:
            task_subprocesses.pop(task_id,None)
    with open(evidence_path) as stream:evidence=json.load(stream)
    with open(meta_path,"w") as stream:json.dump({"signature":signature,"sha256":file_sha256(evidence_path)},stream)
    return evidence


def analyze_source(audio_path, media_path, output_dir, task_id=None, media_sha256=None):
    from pipeline.step1_transcribe import transcribe
    from core import config
    os.makedirs(output_dir, exist_ok=True)
    media_hash = media_sha256 or file_sha256(media_path)
    audio_hash = file_sha256(audio_path)
    worker = os.path.join(config.BASE_DIR, "workers", "analyze_speakers.py")
    signature = native_analysis_signature(audio_hash,worker)
    acoustic_signature = verification_signature(signature)
    cache = os.path.join(output_dir, "speaker_analysis.json")
    if os.path.isfile(cache):
        with open(cache, encoding="utf-8") as stream:
            old = json.load(stream)
        if old.get("source_media_sha256") == media_hash and old.get("source_audio_sha256") == audio_hash and old.get("speaker_turns") and old.get("verification_complete") is True and old.get("analysis_revision") == ANALYSIS_REVISION and old.get("native_analysis_signature") == signature and old.get("verification_signature") == acoustic_signature:
            return old
    def stage(step, progress, message):
        if task_id:
            from core.task_manager import get_task, update_task
            if (get_task(task_id) or {}).get("type") == "analysis":
                update_task(task_id, step=step, progress=progress, message=message)
    stage("transcribe", 20, "正在转录并对齐原声词流...")
    transcription = transcribe(audio_path, output_dir=output_dir, diarize=False)
    stage("diarize", 55, "正在分析原声人物时间段...")
    speaker_turns = _native_speaker_turns(audio_path,output_dir,signature,task_id)
    stage("bind_speakers", 70, "正在绑定词级人物，保留待确认片段...")
    initial = build_analysis(transcription, media_hash, speaker_turns)
    atomic_json(cache, initial)
    evidence = _verify_source_speakers(audio_path,transcription,initial,output_dir,acoustic_signature,task_id)
    stage("finalize_analysis", 98, "正在汇总声纹复核和待确认片段...")
    result = build_analysis(transcription, media_hash, speaker_turns,verification=evidence)
    result["verification_signature"] = acoustic_signature
    result["speaker_verification_summary"].update({
        "windows":len(evidence["units"]),
        "verified_words":sum(bool(w.get("speaker_verified")) for w in result["words"]),
        "corrected_words":sum(a["speaker"]!=b["speaker"] for a,b in zip(initial["words"],result["words"]))})
    result["source_audio_sha256"] = audio_hash
    result["native_analysis_signature"] = signature
    atomic_json(cache, result)
    return result


def extract_reference_pool(audio_path, analysis, output_dir):
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


def _extract_audio_clip(audio_path, start_ms, end_ms, output_path):
    """Extract a bounded reference; failed FFmpeg output is never cached."""
    from core import config
    import subprocess
    import tempfile
    from pathlib import Path
    if end_ms <= start_ms:
        raise ValueError('Reference audio interval must be positive')
    target = Path(output_path)
    with tempfile.TemporaryDirectory(prefix='.reference-', dir=target.parent) as temporary:
        output = Path(temporary) / 'reference.wav'
        subprocess.run([
            'ffmpeg', '-y', '-loglevel', 'error', '-ss', str(start_ms / 1000),
            '-i', str(audio_path), '-t', str((end_ms - start_ms) / 1000),
            '-acodec', 'pcm_s16le', str(output),
        ], check=True, capture_output=True, timeout=config.REF_EXTRACT_TIMEOUT)
        os.replace(output, target)



def ensure_dubbing_ready(analysis):
    unknown = [w for w in analysis.get("words", []) if not known_speaker(w.get("speaker"))]
    if unknown:
        raise ValueError(f"Source analysis has unresolved speaker at {unknown[0]['start']:.3f}s; confirm before dubbing")
    missing = analysis.get("missing_reference_speakers", [])
    if missing:
        raise ValueError("Insufficient clean same-speaker reference: " + ", ".join(missing))


def confirm_speakers(analysis, resolutions):
    """Apply explicit human decisions only to unresolved words; preserve source IDs."""
    import copy
    if not isinstance(resolutions, list) or not resolutions:
        raise ValueError("Nonempty resolutions list required")
    result = copy.deepcopy(analysis)
    catalog = {w["word_id"]:w for w in result["words"]}
    used = set()
    for resolution in resolutions:
        if not isinstance(resolution, dict):
            raise ValueError("Each resolution must be an object")
        speaker, ids = resolution.get("speaker_id"), resolution.get("word_ids")
        if not known_speaker(speaker) or speaker not in result["references"]:
            raise ValueError("Choose an existing speaker with a clean reference")
        if not isinstance(ids, list) or not ids:
            raise ValueError("word_ids required")
        for word_id in ids:
            if (type(word_id) is not int or word_id not in catalog or word_id in used
                    or known_speaker(catalog[word_id].get("speaker"))):
                raise ValueError("Only unresolved, unique source word IDs may be confirmed")
            used.add(word_id)
            catalog[word_id].update(speaker=speaker, speaker_assignment="user_confirmed",
                                    human_confirmed=True, speaker_verified=False, reference_eligible=False)
    rebuilt = _assemble_analysis(result["words"], result["source_media_sha256"],
                                 result.get("speaker_turns") or None, result.get("verification_complete", False))
    summary = result.get("speaker_verification_summary", {})
    result.update(rebuilt)
    result["speaker_verification_summary"] = {**summary, **rebuilt["speaker_verification_summary"]}
    result["analysis_id"] = analysis["analysis_id"]
    result["review_revision"] = analysis.get("review_revision", 0) + 1
    return result
