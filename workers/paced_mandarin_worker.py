"""Batch worker for Mandarin with bounded phoneme durations and a fixed timbre."""
import json
import os
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from core.files import atomic_json
from pipeline.dubbing_plan import file_sha256
from pipeline.tts_narration import (
    NarrationQualityError, cached_synthesis_valid, report_path,
    synthesize_narration, write_synthesis,
)


def synthesize_job(engine, job, set_seed):
    reference = job['ref_audio']
    output = job['output_path']
    if job.get('lang', 'zh') != 'zh':
        raise ValueError('Paced Mandarin requires a Chinese utterance')
    if cached_synthesis_valid(output):
        return
    timing = []

    def generate(chunk, seed, _limit):
        set_seed(seed)
        audio = engine.generate(chunk, reference)
        timing.append(dict(engine.last_timing))
        return audio

    try:
        audio, report = synthesize_narration(
            job['text'], 'zh', file_sha256(reference), generate, engine.sample_rate,
            seed_context='paced_mandarin', max_attempts=1,
        )
    except NarrationQualityError as error:
        atomic_json(report_path(output), {
            **error.report, 'engine': 'paced_mandarin', 'timing': timing, 'error': str(error),
        })
        raise
    write_synthesis(output, audio, engine.sample_rate, {
        **report, 'engine': 'paced_mandarin', 'timing': timing,
    })


def main(task_file):
    # Pre-staged model assets only. Missing weights must fail explicitly.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    root = Path(os.environ['BILIMIX_PACED_ZH_ROOT']).resolve()
    os.environ['HF_HOME'] = str(root / 'frontend' / 'hf')
    os.environ['NLTK_DATA'] = str(root / 'frontend' / 'nltk')
    import torch
    from workers.paced_mandarin import PacedMandarin

    task = json.loads(Path(task_file).read_text(encoding='utf-8'))
    started = time.perf_counter()
    engine = PacedMandarin(os.environ['BILIMIX_PACED_ZH_ROOT'], task['device'])
    print(f"[PacedMandarin] Model loading {time.perf_counter() - started:.2f}s",
          file=sys.stderr, flush=True)

    def set_seed(seed):
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)

    results = []
    for job in task['jobs']:
        synthesize_job(engine, job, set_seed)
        results.append({'output_path': job['output_path'], 'error': None})
    print(json.dumps({'success': len(results), 'failed': 0, 'results': results}))


if __name__ == '__main__':
    main(sys.argv[1])
