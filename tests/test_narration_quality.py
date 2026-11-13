"""Narration regressions using synthetic audio; never load a speech model."""
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest
from pydub import AudioSegment
from pydub.generators import Sine


def test_engine_source_change_invalidates_synthesis_cache(tmp_path):
    from pipeline.tts_cache import model_fingerprint
    engine = tmp_path / 'confuciustts'
    engine.mkdir()
    implementation = engine / 'inference.py'
    implementation.write_text('version = 1')
    worker, config = tmp_path / 'worker.py', tmp_path / 'config.yaml'
    worker.write_text('# worker')
    config.write_text('model: test')
    before = model_fingerprint(tmp_path, worker, config)
    implementation.write_text('version = 2 with changed pacing')
    assert before != model_fingerprint(tmp_path, worker, config)


def test_mixer_bounds_edge_silence_without_changing_speech_duration(tmp_path):
    from pipeline.step4b_sentence_mixer import mix_sentence_audio
    from pydub.silence import detect_silence
    tone = Sine(440, sample_rate=24000).to_audio_segment(duration=1000).apply_gain(-15)
    paths = {}
    for i, tail in enumerate((900, 0)):
        path = tmp_path / f'{i}.wav'
        (tone + AudioSegment.silent(duration=tail, frame_rate=24000)).export(path, format='wav')
        paths[i] = str(path)
    out = tmp_path / 'out.wav'
    result = mix_sentence_audio('', [dict(start=i,end=i+1,unit_id=f'u{i}',speaker='A')
        for i in range(2)], [0,1], {0:'第一句。',1:'第二句。'}, paths, str(out))
    gaps = detect_silence(AudioSegment.from_wav(out), min_silence_len=80, silence_thresh=-45)
    assert max(end-start for start,end in gaps) < 400
    assert 1 <= result['time_mapping'][0]['mixed_end'] <= 1.1


@pytest.mark.parametrize('resume', [False, True])
def test_bgm_disabled_ignores_cached_track(tmp_path, monkeypatch, resume):
    from application import dubbing
    from core import config, task_manager as tm
    task_id = f'narration-no-bgm-{resume}'
    audio = tmp_path / 'source.wav'; audio.write_bytes(b'fixture')
    bgm = tmp_path / 'bgm.wav'; bgm.write_bytes(b'cached accompaniment')
    segments = [dict(text='hello',start=0,end=1,unit_id='u1',speaker='A')]
    with tm.tasks_lock:
        tm.tasks[task_id] = dict(task_id=task_id,type='audio',process_mode='sentence_translate',
            status='processing',_basename=task_id,_audio_path=str(audio),keep_bgm=False,
            _bgm_path=str(bgm),segments=segments,translations={0:'你好。'},translated_indices=[0])
    (Path(config.RESULT_DIR) / task_id).mkdir(exist_ok=True)
    monkeypatch.setattr(config, 'SKIP_TTS', False, raising=False)
    monkeypatch.setattr(config, 'SENTENCE_TTS_VOICE_CLONE', True)
    monkeypatch.setattr(dubbing, '_ensure_vocals_for_tts', lambda _: str(audio))
    monkeypatch.setattr(dubbing, '_build_confucius_ref_map', lambda *a: {0:str(audio)})
    monkeypatch.setattr(dubbing, 'synthesize_sentences_with_confucius_tts', lambda *a,**kw: {0:str(audio)})
    seen = []
    def mix(*args, **kwargs):
        seen.append(kwargs.get('bgm_path'))
        raise RuntimeError('stop after observing actual mix call')
    monkeypatch.setattr(dubbing, 'mix_sentence_audio', mix)
    if resume:
        with pytest.raises(RuntimeError, match='stop after'):
            dubbing._synthesis_resume(task_id)
    else:
        dubbing.continue_after_sentence_confirmation(task_id)
    assert seen == [None]


def test_failed_separation_never_uses_original_mix_as_cloning_prompt(tmp_path, monkeypatch):
    from application import dubbing
    audio = tmp_path / 'source.wav'; audio.write_bytes(b'speech and music')
    monkeypatch.setattr(dubbing, 'get_task', lambda _: dict(_audio_path=str(audio),_basename='test'))
    monkeypatch.setattr(dubbing, 'separate_vocals', lambda *a: dict(ok=False,error='unavailable'))
    with pytest.raises(RuntimeError, match='纯人声'):
        dubbing._ensure_vocals_for_tts('test')


def waveform(seconds, rate=24000):
    return (.15 * np.sin(2*np.pi*440*np.arange(round(seconds*rate))/rate)).astype('float32')


def test_narration_retries_fast_chunk_and_preserves_words_and_reference():
    from pipeline.tts_narration import synthesize_narration
    text = '我们需要让整集中文听起来清楚自然并且保持稳定的节奏。'
    calls = []
    def generate(chunk, seed, limit):
        calls.append((chunk,seed,limit))
        return waveform(1 if len(calls)==1 else 5)
    audio, report = synthesize_narration(text, 'zh', 'same-reference', generate, 24000)
    assert len(calls) == 2
    assert all(call[0] == text for call in calls)
    assert calls[0][1] != calls[1][1]
    assert len(audio) == 5*24000
    assert report['chunks'][0]['selected_attempt'] == 1
    assert len(report['chunks'][0]['attempts']) == 2


def test_narration_seed_independent_of_job_order_and_failed_neighbour():
    from pipeline.tts_narration import synthesize_narration
    captured = []
    def generate(chunk, seed, limit):
        captured.append((chunk, seed))
        return waveform(2)
    first, _ = synthesize_narration('你好。', 'zh', 'speaker-A', generate, 24000)
    synthesize_narration('另一位人物。', 'zh', 'speaker-B', generate, 24000)
    second, _ = synthesize_narration('你好。', 'zh', 'speaker-A', generate, 24000)
    assert captured[0] == captured[-1]
    np.testing.assert_array_equal(first, second)


def test_narration_chunks_only_at_punctuation_and_bounds_silence():
    from pipeline.tts_narration import synthesize_narration
    clause = '我们需要让整集中文听起来清楚自然并且保持稳定的节奏，'
    text = clause*4 + '最后完整地说完。'
    chunks = []
    def generate(chunk, seed, limit):
        chunks.append(chunk)
        assert len(chunk) < limit
        return np.concatenate([np.zeros(24000), waveform(len(chunk)/5), np.zeros(24000)])
    audio, report = synthesize_narration(text,'zh','A',generate,24000)
    assert ''.join(chunks) == text
    assert len(chunks) > 1
    assert all(chunk.endswith('，') for chunk in chunks[:-1])
    assert len(audio)/24000 < len(text)/5+1
    assert report['chunk_count'] == len(chunks)


def test_unpunctuated_long_text_is_not_cut_through_words():
    from pipeline.tts_narration import plan_narration
    with pytest.raises(ValueError, match='标点'):
        plan_narration('需要保持完整语义'*20, 'zh')


def test_narration_keeps_quotes_and_numeric_tokens_with_their_clause():
    from pipeline.tts_narration import plan_narration
    first = '他向我们解释说：“' + '这段内容需要连贯清楚地说完'*4 + '。”'
    second = '随后继续讨论数值3.14以及时间12:30,最后再把整个过程说明白。'
    chunks = plan_narration(first+second, 'zh')
    assert ''.join(chunks) == first+second
    assert chunks[0].endswith('。”')
    assert any('3.14' in chunk and '12:30' in chunk for chunk in chunks)


@pytest.mark.parametrize('bad_audio', [np.zeros(24000), np.full(24000,np.nan), waveform(.2)])
def test_unusable_synthesis_cannot_be_published(bad_audio):
    from pipeline.tts_narration import synthesize_narration, NarrationQualityError
    with pytest.raises(NarrationQualityError) as error:
        synthesize_narration('这是一个需要完整朗读并且保持清楚节奏的中文句子。',
                            'zh','A',lambda *a:bad_audio,24000)
    assert len(error.value.report['chunks'][0]['attempts']) == 3


@pytest.mark.parametrize('text,seconds', [
    ('好。', .8), ('这个 API returns a JSON response with status OK。', 4),
    ('版本 2。', 4), ('这是カタカナ。', 4),
])
def test_short_and_mixed_language_texts_do_not_use_naive_han_rate(text, seconds):
    from pipeline.tts_narration import synthesize_narration
    _, report = synthesize_narration(text, 'zh', 'A', lambda *a: waveform(seconds), 24000)
    assert len(report['chunks'][0]['attempts']) == 1
    assert report['chunks'][0]['attempts'][0]['chars_per_second'] is None


def test_drawn_out_short_reply_retries_without_speeding_up_audio():
    from pipeline.tts_narration import synthesize_narration
    candidates = iter([waveform(3), waveform(.5)])
    audio, report = synthesize_narration('好的。', 'zh', 'same-reference',
                                         lambda *args: next(candidates), 24000)
    chunk = report['chunks'][0]
    assert chunk['selected_attempt'] == 1
    assert 'short_utterance_too_slow' in chunk['attempts'][0]['reasons']
    assert len(audio) == 12000


def test_short_reply_duration_check_excludes_silence():
    from pipeline.tts_narration import synthesize_narration
    samples = np.concatenate([np.zeros(24000), waveform(.6), np.zeros(24000),
                              waveform(.6), np.zeros(24000)])
    _, report = synthesize_narration('好，的。', 'zh', 'A', lambda *args: samples, 24000)
    assert len(report['chunks'][0]['attempts']) == 1


def test_persistently_drawn_out_short_reply_is_rejected():
    from pipeline.tts_narration import synthesize_narration, NarrationQualityError
    with pytest.raises(NarrationQualityError) as error:
        synthesize_narration('嗯。', 'zh', 'A', lambda *args: waveform(3), 24000)
    attempts = error.value.report['chunks'][0]['attempts']
    assert len(attempts) == 3
    assert all('short_utterance_too_slow' in attempt['reasons'] for attempt in attempts)


def test_atomic_synthesis_requires_matching_report_and_audio(tmp_path):
    from pipeline.tts_narration import write_synthesis, cached_synthesis_valid
    path = tmp_path/'speech.wav'
    write_synthesis(path, waveform(1), 24000, {'schema_version':1,'chunks':[]})
    assert cached_synthesis_valid(path)
    path.write_bytes(b'truncated')
    with pytest.raises(ValueError, match='cache'):
        cached_synthesis_valid(path)


@pytest.mark.parametrize('width,channels,duration', [(2,1,120), (4,1,1000), (4,2,1000)])
def test_normalization_preserves_short_speech_duration_and_channel_count(width, channels, duration):
    from pipeline.step4b_sentence_mixer import _normalize_tts_audio
    clip = Sine(440, sample_rate=24000).to_audio_segment(duration=duration).apply_gain(-15)
    clip = clip.set_sample_width(width).set_channels(channels)
    normalized = _normalize_tts_audio(clip)
    assert len(normalized) == len(clip)
    assert normalized.channels == channels
    assert normalized.frame_count() == clip.frame_count()
    assert normalized.sample_width == 2


@pytest.fixture
def fake_engine(tmp_path):
    """A subprocess-compatible model double: real worker/PCM/JSON, no torch/model."""
    root = tmp_path / 'engine'
    package = root / 'confuciustts' / 'cli'
    package.mkdir(parents=True)
    (root/'confuciustts'/'__init__.py').write_text('')
    (package/'__init__.py').write_text('')
    (root/'torch.py').write_text('''
seed = 0
def manual_seed(value):
    global seed
    seed = value
class cuda:
    @staticmethod
    def is_available(): return False
''')
    (package/'inference.py').write_text('''
import json, os
import numpy as np
import torch
class Tensor:
    def __init__(self, samples): self.samples = samples
    def cpu(self): return self
    def squeeze(self, dim): return self
    def numpy(self): return self.samples
class Normalizer:
    def normalize(self, text, language): return text
class ConfuciusTTS:
    def __init__(self, config_path, device):
        self.sample_rate = 24000
        self.normalizer = Normalizer()
    def generate(self, text, lang, prompt_wav, max_text_tokens_per_segment, **kwargs):
        with open(os.environ['TEST_GENERATION_LOG'], 'a') as stream:
            stream.write(json.dumps(dict(text=text, seed=torch.seed, reference=prompt_wav,
                limit=max_text_tokens_per_segment)) + '\\n')
        if text == '无效': return Tensor(np.full(24000, np.nan))
        wave = .15*np.sin(2*np.pi*440*np.arange(48000)/24000)
        return Tensor((wave + np.random.uniform(-.00001,.00001,48000)).astype('float32'))
''')
    (root/'config').mkdir()
    (root/'config'/'inference_config.yaml').write_text('fixture: true')
    return root


def test_worker_from_foreign_cwd_reproducible_across_order_and_cache(tmp_path, fake_engine):
    from pipeline.tts_narration import cached_synthesis_valid, report_path
    worker = Path(__file__).resolve().parents[1]/'workers/confucius_tts_worker.py'
    ref = tmp_path/'ref.wav'
    AudioSegment.silent(duration=2000).export(ref, format='wav')
    log = tmp_path/'calls.jsonl'
    env = {**os.environ, 'CONFUCIUS4_TTS_ROOT':str(fake_engine), 'PYTHONPATH':str(fake_engine),
           'TEST_GENERATION_LOG':str(log), 'BILIMIX_DATA_DIR':str(tmp_path/'data')}
    outputs = [tmp_path/f'{i}.wav' for i in range(4)]
    jobs = [dict(text=text, ref_audio=str(ref), output_path=str(path))
            for text,path in zip(['你好。','再见。','你好。','无效'],outputs)]
    task = tmp_path/'task.json'
    task.write_text(json.dumps(dict(jobs=jobs)))
    result = subprocess.run([sys.executable,str(worker),str(task)],cwd=tmp_path,env=env,
                            text=True,capture_output=True,timeout=30)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.splitlines()[-1])
    assert (summary['success'], summary['failed']) == (3,1)
    assert outputs[0].read_bytes() == outputs[2].read_bytes()
    assert outputs[0].read_bytes() != outputs[1].read_bytes()
    assert cached_synthesis_valid(outputs[0])
    assert not outputs[3].exists()
    assert 'error' in json.loads(report_path(outputs[3]).read_text())
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0]['seed'] == calls[2]['seed']
    assert all(call['reference']==str(ref) for call in calls)
    task.write_text(json.dumps(dict(jobs=jobs[:3])))
    result = subprocess.run([sys.executable,str(worker),str(task)],cwd=tmp_path,env=env,
                            text=True,capture_output=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(log.read_text().splitlines()) == len(calls)


@pytest.mark.parametrize('workers', [1,2])
def test_dispatch_never_returns_partial_narration(tmp_path, monkeypatch, fake_engine, workers):
    from core import config
    from pipeline.step3_tts_confucius import synthesize_sentences_with_confucius_tts
    monkeypatch.setattr(config, 'CONFUCIUS4_TTS_ROOT', str(fake_engine))
    monkeypatch.setattr(config, 'CONFUCIUS4_TTS_PYTHON', sys.executable)
    monkeypatch.setattr(config, 'CONFUCIUS4_TTS_NUM_WORKERS', workers)
    monkeypatch.setenv('PYTHONPATH',str(fake_engine))
    monkeypatch.setenv('TEST_GENERATION_LOG',str(tmp_path/'calls.jsonl'))
    ref = tmp_path/'ref.wav'
    AudioSegment.silent(duration=2000).export(ref,format='wav')
    cache = tmp_path/'cache'; cache.mkdir()
    with pytest.raises(RuntimeError, match='禁止交付部分配音'):
        synthesize_sentences_with_confucius_tts([], [0,1], {0:'你好。',1:'无效'}, '', str(cache),
                                               ref_audio_map={0:str(ref),1:str(ref)})
    assert len(list(cache.glob('*.wav'))) == 1
