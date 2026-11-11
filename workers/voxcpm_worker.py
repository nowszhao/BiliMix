#!/usr/bin/env python3
"""VoxCPM2 worker — zero-shot Chinese voice cloning.

Same contract as confucius_tts_worker: one task JSON with a jobs list, one WAV
per complete Chinese utterance, plus an atomic .quality.json report.
Only Linux CPU is supported; Apple Silicon MPS produces corrupted audio.
"""
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.files import atomic_json
from pipeline.tts_narration import (
    NarrationQualityError, cached_synthesis_valid, report_path,
    synthesize_narration, write_synthesis,
)

HAN = re.compile(r"[\u3400-\u9fff]")


def _han_count(text):
    return len(HAN.findall(text))


def synthesize_job(model, job, set_seed, sample_rate, cfg):
    """Synthesize one complete utterance; short lines get multi-candidate screening.

    VoxCPM2 occasionally emits training-data leakage for 1-3 character lines
    (e.g. a subscription slogan) which shows up as a far too long waveform.
    For short lines we draw three candidates and keep the one whose duration
    is closest to a natural short reply. This is an acoustic heuristic, not
    pronunciation verification; ASR spot checks are still required.
    """
    reference = job["ref_audio"]
    output = job["output_path"]
    text = job["text"]
    lang = job.get("lang", "zh")
    if lang != "zh":
        raise ValueError("VoxCPM2 worker handles Chinese utterances only")
    if cached_synthesis_valid(output):
        return
    with open(reference, "rb") as stream:
        reference_hash = hashlib.sha256(stream.read()).hexdigest()

    common = dict(
        reference_wav_path=reference,
        cfg_value=cfg.get("cfg_value", 2.0),
        inference_timesteps=cfg.get("inference_timesteps", 10),
        retry_badcase=True,
    )

    def generate(chunk, seed, limit):
        set_seed(seed)
        kwargs = dict(common, text=chunk, max_len=max(256, len(chunk) * 4))
        han = _han_count(chunk)
        if han <= 3:
            target = 0.55 * han + 0.5
            candidates = []
            for k in range(3):
                set_seed(seed + k * 7919)
                wav = np.asarray(model.generate(**kwargs), dtype=np.float32).reshape(-1)
                candidates.append((abs(len(wav) / sample_rate - target), wav))
            return min(candidates, key=lambda item: item[0])[1]
        return np.asarray(model.generate(**kwargs), dtype=np.float32).reshape(-1)

    try:
        audio, report = synthesize_narration(
            text, lang, reference_hash, generate, sample_rate,
            seed_context="voxcpm2",
            rate_profile=(cfg.get("target_cps", 5.8),
                          tuple(cfg.get("soft_cps", (4.2, 7.0)))),
        )
    except NarrationQualityError as error:
        atomic_json(report_path(output), {
            **error.report, "engine": "voxcpm2", "error": str(error),
        })
        raise
    write_synthesis(output, audio, sample_rate, {
        **report, "engine": "voxcpm2", "model": cfg.get("model", ""),
    })


def main():
    if len(sys.argv) < 2:
        print("usage: voxcpm_worker.py <task_file.json>", file=sys.stderr)
        sys.exit(1)
    task = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    jobs = task.get("jobs", [])
    if not jobs:
        print(json.dumps({"success": 0, "failed": 0, "results": []}))
        return

    from voxcpm import VoxCPM
    import torch

    cfg = {
        "model": task.get("model", os.environ.get("BILIMIX_VOXCPM_MODEL", "openbmb/VoxCPM2")),
        "cfg_value": task.get("cfg_value", 2.0),
        "inference_timesteps": task.get("inference_timesteps", 10),
        "target_cps": task.get("target_cps", 5.8),
        "soft_cps": task.get("soft_cps", [4.2, 7.0]),
    }
    t0 = time.time()
    model = VoxCPM.from_pretrained(
        cfg["model"], load_denoiser=False, optimize=False,
        device=task.get("device", "cpu"))
    sample_rate = model.tts_model.sample_rate
    print(f"[VoxCPM2] loaded in {time.time() - t0:.1f}s sr={sample_rate}",
          file=sys.stderr, flush=True)

    def set_seed(seed):
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)

    results, failed = [], 0
    for i, job in enumerate(jobs, 1):
        try:
            synthesize_job(model, job, set_seed, sample_rate, cfg)
            results.append({"output_path": job["output_path"], "error": None})
        except Exception as exc:
            failed += 1
            print(f"[VoxCPM2] [{i}/{len(jobs)}] failed: {exc}",
                  file=sys.stderr, flush=True)
            results.append({"output_path": job.get("output_path", ""),
                            "error": str(exc)})
    print(json.dumps({"success": len(results) - failed, "failed": failed,
                      "results": results}))


if __name__ == "__main__":
    main()
