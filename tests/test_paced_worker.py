"""Worker contract and atomic quality output, using a bounded-engine double."""
import json

import numpy as np
import pytest

from pipeline.tts_narration import NarrationQualityError, report_path
from workers.paced_mandarin_worker import synthesize_job


class Engine:
    sample_rate = 8000

    def __init__(self, seconds=.7):
        self.seconds = seconds
        self.calls = []
        self.last_timing = {}

    def generate(self, text, reference):
        self.calls.append((text, reference))
        self.last_timing = {'max_phoneme_seconds': .25}
        return np.full(round(self.seconds * self.sample_rate), .1, dtype=np.float32)


def test_worker_uses_timing_control_once_and_commits_report(tmp_path):
    reference = tmp_path / 'reference.wav'
    reference.write_bytes(b'reference')
    output = tmp_path / 'speech.wav'
    job = dict(text='好的。', lang='zh', ref_audio=str(reference), output_path=str(output))
    engine = Engine()
    synthesize_job(engine, job, lambda seed: None)
    synthesize_job(engine, job, lambda seed: None)
    assert engine.calls == [('好的。', str(reference))]
    report = json.loads(report_path(output).read_text())
    assert report['engine'] == 'paced_mandarin'
    assert report['timing'] == [{'max_phoneme_seconds': .25}]
    assert len(report['chunks'][0]['attempts']) == 1
    assert output.is_file()


def test_paced_worker_reports_bad_output_without_retrying(tmp_path):
    reference = tmp_path / 'reference.wav'
    reference.write_bytes(b'reference')
    output = tmp_path / 'speech.wav'
    job = dict(text='好的。', lang='zh', ref_audio=str(reference), output_path=str(output))
    engine = Engine(seconds=4)
    with pytest.raises(NarrationQualityError):
        synthesize_job(engine, job, lambda seed: None)
    assert len(engine.calls) == 1
    assert not output.exists()
    assert 'error' in json.loads(report_path(output).read_text())
