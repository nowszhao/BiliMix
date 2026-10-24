"""Real queue/API/persistence flow with model calls replaced, not ownership logic."""
import json
import time
from pathlib import Path
import pytest

from core import config
from pipeline.dubbing_plan import file_sha256
from tests.test_audio_first import analysis_fixture, manifest_fixture


def wait_for(tm, task_id, states):
    end=time.monotonic()+5
    while time.monotonic()<end:
        task=tm.get_task(task_id)
        if task.get('status') in states:return task
        time.sleep(.01)
    raise AssertionError(tm.get_task(task_id))


@pytest.mark.parametrize('reference_mode', ['speaker', 'segment'])
def test_queued_analysis_manifest_submission_and_persistence(tmp_path,monkeypatch,reference_mode):
    from services.web_app import app
    from application import dubbing as wa, task_execution as execution, source_analysis as source_analysis
    from core import task_manager as tm
    from core import task_manager as tm
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    monkeypatch.setattr(config,'SKIP_CONFIRMATION',True)
    monkeypatch.setattr(config,'SENTENCE_TTS_VOICE_CLONE',True)
    media=tmp_path/'source.mp4';media.write_bytes(b'owned test media')
    from pydub.generators import Sine
    audio=tmp_path/'source.wav'
    Sine(220).to_audio_segment(duration=12000).export(str(audio), format='wav')
    a=analysis_fixture();a['source_media_sha256']=file_sha256(media)
    monkeypatch.setattr(execution,'prepare_video',lambda *a,**k:dict(ok=True,video_path=str(media),audio_path=str(audio),title='test'))
    monkeypatch.setattr(execution,'_probe_audio_duration',lambda _:11)
    monkeypatch.setattr(wa,'separate_vocals',lambda *a,**k:dict(ok=False))
    monkeypatch.setattr(source_analysis,'separate_vocals',lambda *a,**k:dict(ok=False))
    monkeypatch.setattr(source_analysis,'analyze_source',lambda *args,**kwargs:dict(a))
    monkeypatch.setattr(wa,'_ensure_vocals_for_tts',lambda _:str(audio))
    pool={s:dict(path=str(tmp_path/f'{s}.wav'),sha256=s,speaker=s) for s in ['A','B']}
    monkeypatch.setattr(wa,'extract_reference_pool',lambda *a,**k:pool)
    def forbidden_translation(*args, **kwargs):
        raise AssertionError('Skill translation must never be replaced on the server')
    monkeypatch.setattr(wa,'translate_sentences',forbidden_translation)
    captured=[]
    def synth(segments,indices,translations,*args,**kwargs):
        captured.append(kwargs['ref_audio_map'])
        return {i:str(audio) for i in indices}
    monkeypatch.setattr(wa,'synthesize_sentences_with_confucius_tts',synth)
    def mix(**kwargs):
        Path(kwargs['output_path']).write_bytes(b'complete output')
        return dict(original_duration=11,mixed_duration=3,total_segments=3,translated_segments=3,
                    time_mapping=[dict(segment_index=i,type='tts_chinese',mixed_start=i,mixed_end=i+1,
                                       orig_start=s['start'],orig_end=s['end']) for i,s in enumerate(kwargs['segments'])])
    monkeypatch.setattr(wa,'mix_sentence_audio',mix)
    monkeypatch.setattr(wa,'_run_video_post_process',lambda task_id:tm.update_task(task_id,status='completed',step='done'))
    client=app.test_client()
    assert client.get('/api').get_json()['dubbing_protocol']==1
    response=client.post('/api/dubbing/analyze',json=dict(local_path=str(media)))
    assert response.status_code==200,response.get_json()
    aid=response.get_json()['task_id'];task=wait_for(tm,aid,{'completed','error'})
    assert task['status']=='completed',task
    analysis=client.get(f'/api/task/{aid}/result').get_json()['result']['analysis']
    assert analysis['analysis_id']==aid
    manifest,_=manifest_fixture();manifest['analysis_id']=aid;manifest['source_media_sha256']=file_sha256(media)
    subtitle=tmp_path/'work.ass'
    subtitle.write_text('[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,A,0,0,0,,Hello || 你好。\n',encoding='utf-8')
    response=client.post('/api/submit',json=dict(type='video',local_path=str(media),subtitle_path=str(subtitle),dub_manifest=manifest,analysis_id=aid,tts_reference_mode=reference_mode))
    assert response.status_code==200,response.get_json()
    tid=response.get_json()['task_id'];task=wait_for(tm,tid,{'completed','error'})
    assert task['status']=='completed',task
    if reference_mode == 'speaker':
        assert captured[0][0]==captured[0][2]!=captured[0][1]
    else:
        assert len(set(captured[0].values())) == 3
    saved=Path(config.RESULT_DIR)/task['_basename']/'task_result.json'
    state=json.loads(saved.read_text())
    assert state['_dub_manifest']==manifest
    assert state['_reference_manifest']['A']['speaker']=='A'
    assert state['tts_reference_mode'] == reference_mode
    assert state['_tts_reference_manifest']['mode'] == reference_mode
    result=client.get(f'/api/task/{tid}/result').get_json()
    assert result['tts_reference_mode'] == reference_mode
    assert all(u['actual_mode'] == reference_mode for u in result['tts_reference_manifest']['units'])


def test_bad_or_unready_analysis_is_400(tmp_path,monkeypatch):
    from services.web_app import app
    from application import dubbing as wa, task_execution as execution, source_analysis as source_analysis
    from core import task_manager as tm
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    media=tmp_path/'source.mp4';media.write_bytes(b'media')
    response=app.test_client().post('/api/submit',json=dict(type='video',local_path=str(media),analysis_id='missing',dub_manifest={}))
    assert response.status_code==400


def test_missing_external_translation_cannot_fall_back(tmp_path, monkeypatch):
    from services.web_app import app
    from application import dubbing as wa, task_execution as execution, source_analysis as source_analysis
    from core import task_manager as tm
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    media=tmp_path/'source.mp4';media.write_bytes(b'media')
    response=app.test_client().post('/api/submit',json=dict(
        type='video',local_path=str(media),subtitle_path=str(tmp_path/'missing.ass')))
    assert response.status_code==400
    assert '禁止回退' in response.get_json()['error']


def test_invalid_ass_cannot_trigger_server_translation(tmp_path, monkeypatch):
    from services.web_app import app
    from application import dubbing as wa, task_execution as execution, source_analysis as source_analysis
    from core import task_manager as tm
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    monkeypatch.setattr(wa,'separate_vocals',lambda *a,**k:dict(ok=False))
    monkeypatch.setattr(source_analysis,'separate_vocals',lambda *a,**k:dict(ok=False))
    def forbidden(*args, **kwargs):
        raise AssertionError('Must not transcribe or translate instead of skill output')
    monkeypatch.setattr(source_analysis,'analyze_source',forbidden)
    monkeypatch.setattr(wa,'translate_sentences',forbidden)
    source=tmp_path/'audio.wav';source.write_bytes(b'not read by mocked model')
    subtitle=tmp_path/'invalid.ass';subtitle.write_text('[Events]\n')
    task_id='invalid-ass-test'
    with tm.tasks_lock:
        tm.tasks[task_id]=dict(task_id=task_id,status='processing',
                              _subtitle_path=str(subtitle),url='file://'+str(source))
    wa.process_audio_sentence_mode(task_id,str(source))
    task=tm.get_task(task_id)
    assert task['status']=='error'
    assert '禁止回退' in task['message']


def test_failed_analysis_retries_analysis_without_server_translation(tmp_path,monkeypatch):
    from services.web_app import app
    from application import dubbing as wa, task_execution as execution, source_analysis as source_analysis
    from core import task_manager as tm
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    source=tmp_path/'source.mp4';source.write_bytes(b'media')
    audio=tmp_path/'audio.wav';audio.write_bytes(b'audio')
    monkeypatch.setattr(execution,'prepare_video',lambda *a,**k:dict(ok=True,video_path=str(source),audio_path=str(audio)))
    monkeypatch.setattr(wa,'separate_vocals',lambda *a,**k:dict(ok=False))
    monkeypatch.setattr(source_analysis,'separate_vocals',lambda *a,**k:dict(ok=False))
    monkeypatch.setattr(execution,'_probe_audio_duration',lambda _:11)
    calls=[]
    def analyze(*args,**kwargs):
        calls.append(args)
        if len(calls)==1:raise ValueError('binding failed after native inference')
        return analysis_fixture()
    monkeypatch.setattr(source_analysis,'analyze_source',analyze)
    def forbidden(*args,**kwargs):raise AssertionError('Analysis retry must not enter translation or TTS')
    monkeypatch.setattr(wa,'process_audio_sentence_mode',forbidden)
    client=app.test_client()
    response=client.post('/api/dubbing/analyze',json=dict(local_path=str(source)))
    tid=response.get_json()['task_id'];assert wait_for(tm,tid,{'error'})['status']=='error'
    response=client.post(f'/api/task/{tid}/retry')
    assert response.status_code==200
    assert wait_for(tm,tid,{'completed','error'})['status']=='completed'
    assert len(calls)==2
    assert client.post(f'/api/task/{tid}/retry').status_code == 400
    response=client.post(f'/api/task/{tid}/retry',json={'refresh_analysis':True})
    assert response.status_code==200
    assert wait_for(tm,tid,{'completed','error'})['status']=='completed'
    assert len(calls)==3
    from core.task_manager import tasks,tasks_lock,restore_task_from_disk
    with tasks_lock:tasks.pop(tid)
    restored=restore_task_from_disk(tid)
    assert restored['status']=='completed'
    assert restored['type']=='analysis'
    assert restored['_audio_path']==str(audio)
    assert restored['_video_path']==str(source)
    assert restored['url']=='file://'+str(source)
    assert client.post(f'/api/task/{tid}/retry',json={'refresh_analysis':True}).status_code==200
    assert wait_for(tm,tid,{'completed','error'})['status']=='completed'
    assert len(calls)==4


def test_external_ass_requires_manifest_before_task_creation(tmp_path, monkeypatch):
    from services.web_app import app
    from application import dubbing as wa, task_execution as execution, source_analysis as source_analysis
    from core import task_manager as tm
    monkeypatch.setattr(config, 'AUTH_ENABLED', False)
    media = tmp_path / 'source.mp4'
    media.write_bytes(b'media')
    subtitle = tmp_path / 'work.ass'
    subtitle.write_text('[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,A,0,0,0,,Hello || 你好。\n', encoding='utf-8')
    before = set(tm.tasks)
    response = app.test_client().post('/api/submit', json={
        'type': 'video', 'local_path': str(media), 'subtitle_path': str(subtitle)})
    assert response.status_code == 400
    assert '同时提供' in response.get_json()['error']
    assert set(tm.tasks) == before

def test_failed_setting_save_keeps_runtime_and_reports_api_failure(tmp_path, monkeypatch):
    from core import config_manager
    from services.web_app import app
    from application import dubbing as wa, task_execution as execution, source_analysis as source_analysis
    from core import task_manager as tm
    monkeypatch.setattr(config, 'AUTH_ENABLED', False)
    monkeypatch.setattr(config, 'WATERMARK_TEXT', 'current')
    def fail(*args, **kwargs):
        raise OSError('disk unavailable')
    monkeypatch.setattr(config_manager, 'save_settings', fail)
    response = app.test_client().post('/api/config', json={'watermark_text': 'unsaved'})
    assert response.status_code == 500
    assert config.WATERMARK_TEXT == 'current'


def test_setting_api_rejects_unknown_keys_and_false_as_string(monkeypatch):
    from services.web_app import app
    from application import dubbing as wa, task_execution as execution, source_analysis as source_analysis
    from core import task_manager as tm
    monkeypatch.setattr(config, 'AUTH_ENABLED', False)
    client = app.test_client()
    for body in ({'watermark_enabled': 'false'}, {'unknown_option': 1},
                 {'sub_font_size_default': 1.5}, {'watermark_opacity': float('nan')}):
        assert client.post('/api/config', json=body).status_code == 400


def test_queued_refined_video_is_fully_recoverable_before_any_model_starts(tmp_path,monkeypatch):
    from services.web_app import app
    from services import task_api
    from core import task_manager as tm
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    monkeypatch.setattr(task_api.execution,'enqueue_task',lambda *a,**k:None)
    media=tmp_path/'source.mp4';media.write_bytes(b'original video')
    analysis=analysis_fixture();analysis['source_media_sha256']=file_sha256(media)
    aid='queued-analysis';analysis['analysis_id']=aid
    tm.tasks[aid]=dict(task_id=aid,type='analysis',status='completed',result={'analysis':analysis})
    manifest,_=manifest_fixture();manifest['source_media_sha256']=file_sha256(media);manifest['analysis_id']=aid
    subtitle=tmp_path/'refined.ass';subtitle.write_text('Dialogue: 0,0:00:00.00,0:00:01.00,Default,A,0,0,0,,Hello || 你好。\n')
    response=app.test_client().post('/api/submit',json=dict(type='video',local_path=str(media),
        subtitle_path=str(subtitle),analysis_id=aid,dub_manifest=manifest,tts_reference_mode='segment'))
    assert response.status_code==200,response.get_json()
    tid=response.get_json()['task_id'];state=tm.get_task(tid)
    assert state['_basename']==tid
    assert (Path(config.RESULT_DIR)/tid/'task_result.json').is_file()
    tm.tasks.pop(tid)
    restored=tm.restore_task_from_disk(tid)
    assert restored['type']=='video' and restored['_dub_manifest']==manifest
    assert restored['_analysis_id']==aid and restored['_subtitle_path']==str(subtitle)
    assert restored['tts_reference_mode']=='segment'
