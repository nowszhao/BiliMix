"""Run native diarization in the model environment, preserving overlap turns."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('audio')
    parser.add_argument('output')
    args = parser.parse_args()
    cache = Path(os.environ.get("HF_HUB_CACHE", str(Path.home()/".cache/huggingface/hub")))
    snapshots = cache/'models--pyannote--speaker-diarization-community-1'/'snapshots'
    required = ('config.yaml', 'embedding/pytorch_model.bin', 'segmentation/pytorch_model.bin', 'plda/plda.npz')
    if snapshots.is_dir() and any(all((snapshot/item).is_file() for item in required) for snapshot in snapshots.iterdir()):
        os.environ.setdefault('HF_HUB_OFFLINE', '1')
    import torch
    from pyannote.audio import Pipeline
    # Cached models work without a token; an unavailable gated model fails clearly.
    model = os.environ.get('BILIMIX_DIARIZATION_MODEL', 'pyannote/speaker-diarization-community-1')
    pipeline = Pipeline.from_pretrained(model, token=os.environ.get('HF_TOKEN') or None)
    pipeline.to(torch.device('cpu'))
    output = pipeline(args.audio)
    annotation = getattr(output, 'speaker_diarization', output)
    turns = [{'start': turn.start, 'end': turn.end, 'speaker': speaker}
             for turn, _, speaker in annotation.itertracks(yield_label=True)]
    Path(args.output).write_text(json.dumps(turns), encoding='utf-8')


if __name__ == '__main__': main()
