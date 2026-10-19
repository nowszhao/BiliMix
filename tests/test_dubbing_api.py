"""Real queue/API/persistence flow with model calls replaced, not ownership logic."""
import json
import time
from pathlib import Path

from core import config
from pipeline.dubbing_plan import file_sha256
from tests.test_audio_first import analysis_fixture, manifest_fixture


def wait_for(wa, task_id, states):
    end=time.monotonic()+5
    while time.monotonic()<end:
        task=wa.get_task(task_id)
        if task.get('status') in states:return task
        time.sleep(.01)
    raise AssertionError(wa.get_task(task_id))


def test_queued_analysis_manifest_submission_and_persistence(tmp_path,monkeypatch):
    from services import web_app as wa
    from core import task_manager as tm
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    monkeypatch.setattr(config,'SKIP_CONFIRMATION',True)
    monkeypatch.setattr(config,'SENTENCE_TTS_VOICE_CLONE',True)
    media=tmp_path/'source.mp4';media.write_bytes(b'owned test media')
    audio=tmp_path/'source.wav';audio.write_bytes(b'owned source audio')
    a=analysis_fixture();a['source_media_sha256']=file_sha256(media)
    monkeypatch.setattr(wa,'prepare_video',lambda *a,**k:dict(ok=True,video_path=str(media),audio_path=str(audio),title='test'))
    monkeypatch.setattr(wa,'_probe_audio_duration',lambda _:11)
    monkeypatch.setattr(wa,'separate_vocals',lambda *a,**k:dict(ok=False))
    monkeypatch.setattr(wa,'analyze_source',lambda *args,**kwargs:dict(a))
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
    monkeypatch.setattr(wa,'_run_video_post_process',lambda task_id:wa.update_task(task_id,status='completed',step='done'))
    client=wa.app.test_client()
    assert client.get('/api').get_json()['dubbing_protocol']==1
    response=client.post('/api/dubbing/analyze',json=dict(local_path=str(media)))
    assert response.status_code==200,response.get_json()
    aid=response.get_json()['task_id'];task=wait_for(wa,aid,{'completed','error'})
    assert task['status']=='completed',task
    analysis=client.get(f'/api/task/{aid}/result').get_json()['result']['analysis']
    assert analysis['analysis_id']==aid
    manifest,_=manifest_fixture();manifest['analysis_id']=aid;manifest['source_media_sha256']=file_sha256(media)
    response=client.post('/api/submit',json=dict(type='video',local_path=str(media),dub_manifest=manifest,analysis_id=aid))
    assert response.status_code==200,response.get_json()
    tid=response.get_json()['task_id'];task=wait_for(wa,tid,{'completed','error'})
    assert task['status']=='completed',task
    assert captured[0][0]==captured[0][2]!=captured[0][1]
    saved=Path(config.RESULT_DIR)/task['_basename']/'task_result.json'
    state=json.loads(saved.read_text())
    assert state['_dub_manifest']==manifest
    assert state['_reference_manifest']['A']['speaker']=='A'


def test_bad_or_unready_analysis_is_400(tmp_path,monkeypatch):
    from services import web_app as wa
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    media=tmp_path/'source.mp4';media.write_bytes(b'media')
    response=wa.app.test_client().post('/api/submit',json=dict(type='video',local_path=str(media),analysis_id='missing',dub_manifest={}))
    assert response.status_code==400


def test_missing_external_translation_cannot_fall_back(tmp_path, monkeypatch):
    from services import web_app as wa
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    media=tmp_path/'source.mp4';media.write_bytes(b'media')
    response=wa.app.test_client().post('/api/submit',json=dict(
        type='video',local_path=str(media),subtitle_path=str(tmp_path/'missing.ass')))
    assert response.status_code==400
    assert '禁止回退' in response.get_json()['error']


def test_invalid_ass_cannot_trigger_server_translation(tmp_path, monkeypatch):
    from services import web_app as wa
    monkeypatch.setattr(config,'AUTH_ENABLED',False)
    monkeypatch.setattr(wa,'separate_vocals',lambda *a,**k:dict(ok=False))
    def forbidden(*args, **kwargs):
        raise AssertionError('Must not transcribe or translate instead of skill output')
    monkeypatch.setattr(wa,'analyze_source',forbidden)
    monkeypatch.setattr(wa,'translate_sentences',forbidden)
    source=tmp_path/'audio.wav';source.write_bytes(b'not read by mocked model')
    subtitle=tmp_path/'invalid.ass';subtitle.write_text('[Events]\n')
    task_id='invalid-ass-test'
    with wa.tasks_lock:
        wa.tasks[task_id]=dict(task_id=task_id,status='processing',
                              _subtitle_path=str(subtitle),url='file://'+str(source))
    wa.process_audio_sentence_mode(task_id,str(source))
    task=wa.get_task(task_id)
    assert task['status']=='error'
    assert '禁止回退' in task['message']
