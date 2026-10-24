"""Run native diarization in the model environment, preserving overlap turns."""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pipeline.model_runtime import configure_diarization_environment


def _load_audio(path):
    """Load decoded mono audio without relying on torchcodec/FFmpeg dylibs."""
    import soundfile as sf
    import torch

    samples, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    if samples.size == 0:
        raise ValueError("Empty source audio")
    if samples.shape[1] > 1:
        samples = samples.mean(axis=1, keepdims=True)
    waveform = torch.from_numpy(samples.T.copy())
    return {"waveform": waveform, "sample_rate": int(sample_rate)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('audio')
    parser.add_argument('output')
    args = parser.parse_args()
    model = configure_diarization_environment()
    import torch
    from pyannote.audio import Pipeline
    # Cached models work without a token; an unavailable gated model fails clearly.
    pipeline = Pipeline.from_pretrained(model, token=os.environ.get('HF_TOKEN') or None)
    pipeline.to(torch.device('cpu'))
    # pyannote 4 can accept an in-memory waveform.  This avoids a fragile
    # torchcodec/FFmpeg dynamic-library dependency for the worker.
    output = pipeline(_load_audio(args.audio))
    annotation = getattr(output, 'speaker_diarization', output)
    turns = [{'start': turn.start, 'end': turn.end, 'speaker': speaker}
             for turn, _, speaker in annotation.itertracks(yield_label=True)]
    Path(args.output).write_text(json.dumps(turns), encoding='utf-8')


if __name__ == '__main__': main()
