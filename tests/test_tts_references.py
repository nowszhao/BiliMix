"""Local reference selection, PCM extraction, persistence and request contracts."""
import copy
import math
from pathlib import Path
import shutil
import struct
import wave

import pytest

from core import config, database, task_manager as tm
from pipeline import tts_references as refs
from pipeline.dubbing_plan import validate_manifest
from pipeline.tts_cache import synthesis_key
from tests.test_audio_first import manifest_fixture


def wav(path, seconds=12, amplitude=5000):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(struct.pack('<h', round(amplitude * math.sin(i * .1)))
                                    for i in range(round(seconds * 16000))))
    return str(path)


@pytest.fixture
def inputs(tmp_path):
    manifest, analysis = manifest_fixture()
    segments = validate_manifest(manifest, analysis)
    audio = wav(tmp_path/'source.wav')
    pool = {s: dict(speaker=s, start=r['start'], end=r['end'],
                    path=wav(tmp_path/f'{s}.wav', seconds=r['end']-r['start']), sha256=s)
            for s, r in analysis['references'].items()}
    return audio, analysis, segments, list(range(len(segments))), pool, str(tmp_path/'references')


def test_default_keeps_one_reference_per_speaker_without_recropping(inputs, monkeypatch):
    def forbidden(*args):
        raise AssertionError('Fixed speaker mode must not extract per-unit audio')
    monkeypatch.setattr(refs, '_extract_audio_clip', forbidden)
    paths, report = refs.build_reference_map(*inputs)
    assert all(path == inputs[4][inputs[2][i]['speaker']]['path'] for i, path in paths.items())
    assert paths[0] == paths[2] != paths[1]
    assert report['fallback_count'] == 0
    assert all(u['actual_mode'] == 'speaker' for u in report['units'])


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_segment_uses_real_pcm_from_each_complete_unit_and_reuses_it(inputs, monkeypatch):
    original = copy.deepcopy(inputs[2])
    paths, report = refs.build_reference_map(*inputs, mode='segment')
    assert len(set(paths.values())) == len(original)
    assert report['fallback_count'] == 0
    for unit in report['units']:
        segment = original[unit['segment_index']]
        assert unit['start'] == segment['start'] and unit['end'] == segment['end']
        assert unit['actual_mode'] == 'segment'
        with wave.open(unit['path']) as stream:
            assert stream.getnframes()/stream.getframerate() == pytest.approx(unit['end']-unit['start'], abs=.002)
    monkeypatch.setattr(refs, '_extract_audio_clip', lambda *a: pytest.fail('Valid reference must be cached'))
    again, _ = refs.build_reference_map(*inputs, mode='segment')
    assert again == paths
    assert inputs[2] == original  # Display pagination/translation units stay untouched.


@pytest.mark.parametrize('case,reason', [
    ('short', 'reference_too_short'), ('unknown', 'mixed_or_unknown_speaker'),
    ('unverified', 'unverified_or_overlapping_source'), ('missing_ids', 'missing_source_words'),
    ('gap', 'noncontiguous_source_words'), ('overlap', 'other_speaker_in_source_interval')])
def test_unsafe_units_fall_back_to_the_same_speaker(inputs, case, reason):
    audio, analysis, segments, _, pool, folder = inputs
    if case == 'short':
        segments[0]['source_word_ids'] = [0]
    elif case == 'unknown':
        analysis['words'][0]['speaker'] = 'B'
    elif case == 'unverified':
        analysis['words'][0]['reference_eligible'] = False
    elif case == 'missing_ids':
        segments[0].pop('source_word_ids')
    elif case == 'gap':
        segments[0]['source_word_ids'] = [0, 2, 3, 4, 5]
    else:
        analysis['speaker_turns'].append(dict(speaker='B', start=1, end=1.2))
    paths, report = refs.build_reference_map(audio, analysis, segments, [0], pool, folder, 'segment')
    assert paths[0] == pool['A']['path']
    assert report['units'][0]['speaker'] == 'A'
    assert report['units'][0]['fallback_reason'] == reason


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_silent_reference_is_reported_and_never_used(inputs):
    wav(Path(inputs[0]), amplitude=0)
    paths, report = refs.build_reference_map(*inputs, mode='segment')
    assert report['fallback_count'] == len(inputs[2])
    assert all(u['fallback_reason'] == 'reference_silent_or_too_quiet' for u in report['units'])
    assert all(path == inputs[4][inputs[2][i]['speaker']]['path'] for i, path in paths.items())


def test_failed_extraction_uses_own_speaker_reference(inputs, monkeypatch):
    def fail(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(refs, '_extract_audio_clip', fail)
    paths, report = refs.build_reference_map(*inputs, mode='segment')
    assert report['fallback_count'] == len(inputs[2])
    assert all(u['fallback_reason'] == 'reference_extraction_failed' for u in report['units'])


def test_long_reference_is_bounded_at_word_edges_without_splitting_synthesis_text(inputs):
    audio, analysis, segments, _, pool, folder = inputs
    words = [dict(analysis['words'][0], word_id=i, start=i*.5, end=(i+1)*.5) for i in range(30)]
    analysis['words'] = words
    analysis['speaker_turns'] = [dict(speaker='A', start=0, end=15)]
    segment = dict(segments[0], source_word_ids=list(range(30)), start=0, end=15)
    catalog = {w['word_id']: w for w in words}
    bounds, reason = refs._segment_bounds(segment, analysis, catalog, {i:i for i in catalog})
    assert reason is None and bounds['end'] == 10 and bounds['bounded']
    assert segment['end'] == 15 and segment['text_zh'] == segments[0]['text_zh']


def test_mode_partitions_synthesis_cache_even_when_fallback_reference_matches(inputs):
    args = ('你好。', 'zh', inputs[0], 'model', {}, 'A')
    assert synthesis_key(*args, reference_mode='segment') != synthesis_key(*args, reference_mode='speaker')


@pytest.mark.parametrize('checkpoint', [False, True])
def test_mode_survives_partial_index_writes_and_restart(tmp_path, checkpoint):
    task_id = 'reference-mode-restart-'+str(checkpoint)
    database.save_task_to_index(task_id, dict(type='video', process_mode='video',
                                             tts_reference_mode='segment', status='error', basename=task_id))
    database.save_task_to_index(task_id, dict(message='partial status update'))
    assert database.get_task_from_index(task_id)['tts_reference_mode'] == 'segment'
    if checkpoint:
        folder = Path(config.RESULT_DIR)/task_id
        folder.mkdir()
        tm.tasks[task_id] = dict(task_id=task_id, type='video', tts_reference_mode='segment',
                                 _tts_reference_manifest={'mode':'segment', 'units':[]})
        tm.save_task_result_to_disk(str(folder), dict(task_id=task_id, status='error'))
        tm.tasks.pop(task_id)
    restored = tm.restore_task_from_disk(task_id)
    assert restored['tts_reference_mode'] == 'segment'
    if checkpoint:
        assert restored['_tts_reference_manifest']['mode'] == 'segment'


def test_redo_preserves_mode_and_clears_previous_reference_report(tmp_path, monkeypatch):
    from services import web_app as wa
    monkeypatch.setattr(config, 'AUTH_ENABLED', False)
    task_id = 'segment-reference-redo'
    tm.tasks[task_id] = dict(task_id=task_id, type='video', process_mode='video', status='error',
                             url='file://'+str(tmp_path/'source.mp4'), tts_reference_mode='segment',
                             _tts_reference_manifest={'mode':'segment', 'units':[{}]})
    class NoStart:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass
    monkeypatch.setattr(wa.threading, 'Thread', NoStart)
    response = wa.app.test_client().post(f'/api/task/{task_id}/redo')
    assert response.status_code == 200
    assert tm.get_task(task_id)['tts_reference_mode'] == 'segment'
    assert '_tts_reference_manifest' not in tm.get_task(task_id)
    assert database.get_task_from_index(task_id)['tts_reference_mode'] == 'segment'


@pytest.mark.parametrize('mode', ['wrong', True, None, ['segment']])
def test_invalid_mode_is_rejected_before_creating_a_task(mode, monkeypatch):
    from services import web_app as wa
    monkeypatch.setattr(config, 'AUTH_ENABLED', False)
    before = set(tm.tasks)
    response = wa.app.test_client().post('/api/submit', json=dict(tts_reference_mode=mode))
    assert response.status_code == 400
    assert 'tts_reference_mode' in response.get_json()['error']
    assert set(tm.tasks) == before


@pytest.mark.parametrize('mode', ['speaker', 'segment'])
def test_cli_sends_task_mode_and_requires_server_capability(mode, monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'sdk'))
    from bilimix_cli import cli
    calls = []
    class Client:
        def get(self, path):
            assert path == '/api'
            return {'tts_reference_modes':['speaker','segment']}
        def post_json(self, path, body):
            calls.append((path, body))
            return {'task_id':'offline-test'}
    monkeypatch.setattr(cli, 'emit', lambda *a, **k:None)
    args = cli.build_parser().parse_args(['task','submit','--url','https://example.com/source.mp3',
                                         '--tts-reference-mode',mode])
    args.pretty, args.field = False, None
    cli.cmd_task_submit(args, Client())
    assert calls[0][1]['tts_reference_mode'] == mode
    if mode == 'segment':
        class Unsupported(Client):
            def get(self, path):
                return {'dubbing_protocol':1}
        with pytest.raises(cli.CLIError, match='不支持分段复刻'):
            cli.cmd_task_submit(args, Unsupported())
        assert len(calls) == 1
