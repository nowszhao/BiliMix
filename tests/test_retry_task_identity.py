"""Retry identity/persistence regressions; model and video encoding calls are isolated."""
import json
from pathlib import Path

import pytest

from core import config, database, task_manager as tm


def test_partial_index_update_preserves_video_identity_and_source():
    task_id = 'retry-partial-index'
    original = dict(task_id=task_id, type='video', process_mode='video', url='file:///source.mp4',
                    title='Original video title', basename='video-result', status='error', created_at='2026-10-23')
    database.save_task_to_index(task_id, original)
    database.save_task_to_index(task_id, dict(status='completed', progress=100, message='retry done'))
    restored = database.get_task_from_index(task_id)
    for key in ('type', 'process_mode', 'url', 'title', 'basename', 'created_at'):
        assert restored[key] == original[key]
    assert restored['status'] == 'completed'


@pytest.mark.parametrize('basename', ['', 'retry-no-checkpoint'])
@pytest.mark.parametrize('kind', ['video', 'analysis'])
def test_restore_without_checkpoint_preserves_task_kind(tmp_path, basename, kind):
    task_id = 'restore-'+kind+'-'+str(bool(basename))
    source = tmp_path/'source.mp4'; source.write_bytes(b'video')
    database.save_task_to_index(task_id, dict(type=kind, process_mode=kind, url='file://'+str(source),
                                            basename=basename, status='error'))
    restored = tm.restore_task_from_disk(task_id)
    assert restored['type'] == restored['process_mode'] == kind
    assert restored['_video_path'] == str(source)


def test_saved_video_checkpoint_cannot_restore_as_audio(tmp_path):
    task_id, basename = 'saved-video-identity', 'saved-video-identity'
    database.save_task_to_index(task_id, dict(type='video', process_mode='video', basename=basename, status='error'))
    folder = Path(config.RESULT_DIR)/basename; folder.mkdir()
    (folder/'task_result.json').write_text(json.dumps(dict(task_id=task_id, type='video',
        process_mode='sentence_translate', status='error', _video_path=str(tmp_path/'source.mp4'))))
    restored = tm.restore_task_from_disk(task_id)
    assert restored['type'] == restored['process_mode'] == 'video'


def test_interrupted_video_can_be_retried_after_restart():
    task_id = basename = 'interrupted-video-checkpoint'
    database.save_task_to_index(task_id, dict(type='video', process_mode='video', basename=basename,
                                            status='processing', step='synthesize', progress=68))
    folder = Path(config.RESULT_DIR)/basename; folder.mkdir()
    (folder/'task_result.json').write_text(json.dumps(dict(task_id=task_id, type='video',
                                                         status='processing', process_mode='sentence_translate')))
    restored = tm.restore_task_from_disk(task_id)
    assert restored['status'] == 'error'
    assert restored['type'] == restored['process_mode'] == 'video'
    assert restored['step'] == 'synthesize' and restored['progress'] == 68


def test_corrupted_index_recovers_only_exact_task_checkpoint(tmp_path):
    task_id = 'recover-lost-basename'
    basename = 'source_'+task_id[:8]
    source = tmp_path/'original.mp4';source.write_bytes(b'video')
    database.save_task_to_index(task_id, dict(status='error'))
    folder = Path(config.RESULT_DIR)/basename;folder.mkdir()
    (folder/'task_result.json').write_text(json.dumps(dict(task_id=task_id, type='video',
        status='error', _video_path=str(source), process_mode='sentence_translate')))
    restored = tm.restore_task_from_disk(task_id)
    assert restored['type'] == restored['process_mode'] == 'video'
    assert restored['_basename'] == basename
    assert restored['_video_path'] == str(source)


def test_missing_video_never_falls_back_to_an_audio_resume(tmp_path, monkeypatch):
    from application import dubbing as wa
    from application import task_execution as execution
    task_id = 'missing-video-retry'
    audio = tmp_path/'source.wav'; audio.write_bytes(b'audio')
    with tm.tasks_lock:
        tm.tasks[task_id] = dict(task_id=task_id, type='video', process_mode='video', status='error',
                                 url='file://'+str(tmp_path/'missing.mp4'), _audio_path=str(audio))
    def forbidden(*args, **kwargs):
        raise AssertionError('A missing video must not be handled as an audio-only task')
    monkeypatch.setattr(wa, 'process_audio_sentence_mode', forbidden)
    monkeypatch.setattr(wa, '_synthesis_resume', forbidden)
    with pytest.raises(FileNotFoundError, match='视频'):
        execution._resume_task(task_id)
    assert tm.get_task(task_id)['type'] == 'video'


@pytest.mark.parametrize('video_success', [True, False])
@pytest.mark.parametrize('reference_mode', ['speaker', 'segment'])
def test_resumed_video_requires_video_completion_and_retains_complete_checkpoint(tmp_path, monkeypatch, video_success, reference_mode):
    from application import dubbing as wa
    from application import task_execution as execution
    task_id = 'resume-video-'+str(video_success)
    audio = tmp_path/'audio.wav'; audio.write_bytes(b'audio')
    source = tmp_path/'source.mp4'; source.write_bytes(b'video')
    segments = [dict(text='Hello', text_zh='你好。', start=0, end=1, speaker='A', unit_id='u1')]
    mapping = [dict(segment_index=0, type='tts_chinese', mixed_start=0, mixed_end=1, orig_start=0, orig_end=1)]
    with tm.tasks_lock:
        tm.tasks[task_id] = dict(task_id=task_id, type='video', process_mode='video', status='processing',
            tts_reference_mode=reference_mode,
            url='file://'+str(source), _video_path=str(source), _audio_path=str(audio), _basename=task_id,
            segments=segments, translations={0:'你好。'}, translated_indices=[0])
    monkeypatch.setattr(wa, '_build_confucius_ref_map', lambda *a:{0:str(audio)})
    def synth(*args, **kwargs):
        assert kwargs['reference_mode'] == reference_mode
        return {0:str(audio)}
    monkeypatch.setattr(wa, 'synthesize_sentences_with_confucius_tts', synth)
    def mix(*args, **kwargs):
        Path(args[5]).write_bytes(b'complete audio')
        return dict(original_duration=1, mixed_duration=1, total_segments=1, translated_segments=1,
                    time_mapping=mapping)
    monkeypatch.setattr(wa, 'mix_sentence_audio', mix)
    seen = []
    def video(task_id):
        current = tm.get_task(task_id)
        assert current['status'] != 'completed'
        assert current['type'] == current['process_mode'] == 'video'
        assert current['time_mapping'] == mapping
        assert Path(current['result']['mixed_audio']).is_file()
        seen.append(task_id)
        tm.update_task(task_id, status='completed' if video_success else 'error',
                       video_result={'video_path':'dubbed.mp4'} if video_success else None)
        return video_success
    monkeypatch.setattr(wa, '_run_video_post_process', video)
    wa._synthesis_resume(task_id)
    current = tm.get_task(task_id)
    assert seen == [task_id]
    assert current['status'] == ('completed' if video_success else 'error')
    assert current['type'] == 'video'
    saved = json.loads((Path(config.RESULT_DIR)/task_id/'task_result.json').read_text())
    assert saved['type'] == saved['process_mode'] == 'video'
    assert saved['tts_reference_mode'] == reference_mode
    assert saved['status'] == current['status']
    assert saved['result']['mixed_audio'] == current['result']['mixed_audio']
    assert saved['time_mapping'] == mapping


@pytest.mark.parametrize('contents', ['{broken', '{"task_id":"another-task"}'])
def test_corrupted_or_foreign_checkpoint_never_falls_back_to_loose_files(contents):
    task_id='checkpoint-ownership'
    database.save_task_to_index(task_id,dict(type='video',basename=task_id,status='error'))
    folder=Path(config.RESULT_DIR)/task_id;folder.mkdir(exist_ok=True)
    (folder/'task_result.json').write_text(contents)
    with pytest.raises(tm.InvalidTaskCheckpoint):tm.restore_task_from_disk(task_id)
