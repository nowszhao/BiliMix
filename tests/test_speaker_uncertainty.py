"""Uncertain source ownership must survive analysis without inventing a voice."""
import copy

import pytest

from pipeline.dubbing_plan import validate_manifest
from pipeline.speaker_analysis import build_analysis
from pipeline.speaker_verification import verified_word_assignments


def gap_fixture():
    words = [dict(word='before', start=3531+i*.5, end=3531+(i+1)*.5) for i in range(12)]
    words += [dict(word='No.', start=3537.845, end=3538.005)]
    words += [dict(word='after', start=3539+i*.5, end=3539+(i+1)*.5) for i in range(8)]
    turns = [dict(start=3531, end=3537.36846875, speaker='A'),
             dict(start=3538.90409375, end=3543.2, speaker='B')]
    return {'segments': [{'words': words}]}, turns


def test_real_cross_speaker_gap_preserves_every_word_and_reports_review():
    asr, turns = gap_fixture()
    analysis = build_analysis(asr, 'media', turns)
    assert len(analysis['words']) == 21
    assert [w['word_id'] for w in analysis['words']] == list(range(21))
    word = analysis['words'][12]
    assert word['text'] == 'No.' and word['speaker'] is None
    assert word['speaker_candidates'] == ['A', 'B']
    assert word['speaker_verified'] is False
    assert analysis['dubbing_ready'] is False
    assert analysis['unresolved_segments'][0]['word_ids'] == [12]
    assert analysis['references'].keys() == {'A', 'B'}
    assert all(not (ref['start'] <= word['start'] < ref['end']) for ref in analysis['references'].values())


def test_short_same_speaker_vad_gap_is_marked_and_not_reference_eligible():
    analysis = build_analysis({'segments': [{'words': [dict(word='them.', start=118.067, end=118.227)]}]},
        'media', [dict(start=116.80596875, end=118.03784375, speaker='A'),
                  dict(start=118.67909375, end=121.00784375, speaker='A')])
    word = analysis['words'][0]
    assert word['speaker'] == 'A'
    assert word['speaker_assignment'] == 'gap_bridge_unanimous'
    assert word['reference_eligible'] is False


def test_same_speaker_neighbors_do_not_justify_a_word_deep_inside_silence():
    analysis = build_analysis({'segments': [{'words': [dict(word='maybe', start=1.5, end=1.7)]}]},
        'media', [dict(start=0, end=1, speaker='A'), dict(start=2.5, end=4, speaker='A')])
    assert analysis['words'][0]['speaker'] is None


def test_overlapping_equal_voices_are_reviewed_not_arbitrarily_tiebroken():
    analysis = build_analysis({'segments': [{'words': [dict(word='yes', start=1, end=1.5)]}]},
        'media', [dict(start=0, end=3, speaker='A'), dict(start=1, end=2, speaker='B')])
    assert analysis['words'][0]['speaker'] is None
    assert analysis['words'][0]['overlap_speakers'] == ['A', 'B']


def test_whole_utterance_winner_does_not_fill_an_unresolved_interruption():
    words = [dict(word_id=i, start=i, end=i+1, speaker=s) for i,s in enumerate(['A', None, 'A'])]
    evidence = {'units': [dict(word_ids=[0,1,2], scores=[['A', .9], ['B', .1]], runs=[
        dict(word_ids=[0], scores=[['A', .9], ['B', .1]]),
        dict(word_ids=[1], scores=[['A', .3], ['B', .29]]),
        dict(word_ids=[2], scores=[['A', .9], ['B', .1]])])]}
    decisions = verified_word_assignments({'words': words}, evidence)
    assert decisions.get(1, {}).get('speaker') is None
    assert decisions[0]['speaker'] == decisions[2]['speaker'] == 'A'


def test_bounded_local_acoustic_evidence_can_resolve_a_tiny_unknown():
    words = [dict(word_id=0, start=3537.845, end=3538.005, speaker=None)]
    local = dict(word_ids=[0], scores=[], context_scores=[['B', .8], ['A', .2]], context_duration=.46)
    evidence = {'units': [dict(word_ids=[0], scores=[], runs=[local]) ]}
    decisions = verified_word_assignments({'words': words}, evidence)
    assert decisions[0]['speaker'] == 'B'
    assert decisions[0]['speaker_verified'] is True


def test_a_weak_short_interjection_does_not_inherit_a_longer_speaker():
    words = [dict(word_id=0, start=0, end=3, speaker='A'),
             dict(word_id=1, start=3, end=3.16, speaker='B')]
    evidence = {'units': [dict(word_ids=[0,1], scores=[['A', .9], ['B', .1]], runs=[
        dict(word_ids=[0], scores=[['A', .9], ['B', .1]]),
        dict(word_ids=[1], scores=[['A', .31], ['B', .3]])])]}
    decisions = verified_word_assignments({'words': words}, evidence)
    assert decisions.get(1, {'speaker': 'B'})['speaker'] == 'B'


def test_unresolved_source_cannot_be_assigned_by_a_client_manifest():
    asr, turns = gap_fixture()
    analysis = build_analysis(asr, 'media', turns)
    manifest = dict(schema_version=1, analysis_id=analysis['analysis_id'], source_media_sha256='media',
                    units=[dict(unit_id='all', speaker_id='A', text_zh='不能擅自认领。', source_word_ids=list(range(21)))])
    with pytest.raises(ValueError, match='unresolved'):
        validate_manifest(manifest, analysis)


def test_analysis_reaches_acoustic_worker_with_unknown_and_keeps_partial_cache(tmp_path, monkeypatch):
    from pipeline import speaker_analysis as sa
    from pipeline import step1_transcribe
    asr, turns = gap_fixture()
    audio = tmp_path/'source.wav';audio.write_bytes(b'audio')
    media = tmp_path/'source.mp4';media.write_bytes(b'video')
    output = tmp_path/'analysis'
    calls = []
    monkeypatch.setattr(step1_transcribe, 'transcribe', lambda *a, **k: asr)
    monkeypatch.setattr(sa, 'native_analysis_signature', lambda *a:'native')
    monkeypatch.setattr(sa, 'verification_signature', lambda *a:'acoustic')
    monkeypatch.setattr(sa, '_native_speaker_turns', lambda *a:turns)
    def verify(audio, transcript, initial, folder, *args):
        import json
        calls.append(initial)
        assert initial['words'][12]['speaker'] is None
        assert json.loads((output/'speaker_analysis.json').read_text())['verification_complete'] is False
        if len(calls)==1:
            raise RuntimeError('model interrupted')
        return {'units': []}
    monkeypatch.setattr(sa, '_verify_source_speakers', verify)
    with pytest.raises(RuntimeError, match='interrupted'):
        sa.analyze_source(str(audio), str(media), str(output))
    result = sa.analyze_source(str(audio), str(media), str(output))
    assert result['verification_complete'] is True
    assert not result['dubbing_ready'] and len(result['words'])==21
    assert len(calls)==2
    assert sa.analyze_source(str(audio), str(media), str(output))==result
    assert len(calls)==2


def test_explicit_confirmation_preserves_words_and_cannot_change_known_people():
    from pipeline.speaker_analysis import confirm_speakers
    asr, turns = gap_fixture()
    analysis = build_analysis(asr, 'media', turns)
    original = copy.deepcopy(analysis)
    revised = confirm_speakers(analysis, [{'word_ids':[12], 'speaker_id':'B'}])
    assert analysis==original
    assert revised['dubbing_ready'] and not revised['unresolved_segments']
    assert revised['analysis_id']==analysis['analysis_id']
    assert revised['words'][12]['human_confirmed'] is True
    assert not revised['words'][12]['speaker_verified']
    assert not revised['words'][12]['reference_eligible']
    assert [(w['word_id'],w['text'],w['start'],w['end']) for w in revised['words']]==[
        (w['word_id'],w['text'],w['start'],w['end']) for w in original['words']]
    for invalid in ([{'word_ids':[0],'speaker_id':'B'}], [{'word_ids':[12,12],'speaker_id':'A'}],
                    [{'word_ids':[12],'speaker_id':'invented'}], [{'word_ids':[True],'speaker_id':'A'}]):
        with pytest.raises(ValueError):confirm_speakers(analysis, invalid)


def test_missing_competing_reference_never_resolves_by_only_available_voice():
    evidence = {'units':[dict(word_ids=[0], scores=[['A', .9]], runs=[])], 'missing_reference_speakers':['B']}
    assert not verified_word_assignments({'words':[dict(word_id=0,start=0,end=1,speaker=None)]}, evidence)


def test_confirmation_api_persists_current_analysis_and_rejects_invalid_review(tmp_path, monkeypatch):
    import json
    from services import web_app as wa
    from core import config
    from pipeline.speaker_analysis import atomic_json
    asr, turns = gap_fixture()
    analysis = build_analysis(asr, 'media', turns)
    analysis['analysis_id']='review_task'
    task = dict(task_id='review_task',type='analysis',status='completed', result={'analysis':analysis})
    folder=tmp_path/'analysis_review_task';folder.mkdir()
    atomic_json(str(folder/'task_result.json'), task)
    monkeypatch.setattr(config, 'RESULT_DIR', str(tmp_path))
    monkeypatch.setattr(wa, 'get_task', lambda task_id: copy.deepcopy(task))
    def update(task_id, **changes):task.update(changes)
    monkeypatch.setattr(wa, 'update_task', update)
    with wa.app.test_client() as client:
        response=client.post('/api/dubbing/review_task/confirm-speakers',json={'resolutions':[{'word_ids':[12],'speaker_id':'B'}]})
        assert response.status_code==200
        assert response.get_json()['dubbing_ready']
        persisted=json.loads((folder/'task_result.json').read_text())
        cached=json.loads((folder/'speaker_analysis.json').read_text())
        assert persisted['result']['analysis']==cached==task['result']['analysis']
        assert client.post('/api/dubbing/review_task/confirm-speakers',json={'resolutions':[{'word_ids':[12],'speaker_id':'A'}]}).status_code==400


def test_worker_without_references_returns_reviewable_evidence_without_loading_models(tmp_path):
    import json, subprocess, sys
    from pathlib import Path
    asr, turns=gap_fixture()
    analysis=build_analysis(asr, 'media', turns)
    analysis['references']={}
    source=tmp_path/'asr.json';source.write_text(json.dumps(asr))
    initial=tmp_path/'initial.json';initial.write_text(json.dumps(analysis))
    output=tmp_path/'evidence.json'
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'workers/verify_speakers.py'),
                           'unused.wav',str(source),str(initial),str(output)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0, result.stderr
    evidence=json.loads(output.read_text())
    assert evidence['units']==[] and evidence['missing_reference_speakers']==['A','B']


def test_verification_worker_streams_progress_and_writes_evidence(tmp_path, monkeypatch):
    import json, sys
    from core import config, task_manager
    from pipeline.speaker_analysis import _verify_source_speakers
    workers=tmp_path/'workers';workers.mkdir()
    (workers/'verify_speakers.py').write_text(
        'import json,sys\n'
        'print(json.dumps(dict(event="speaker_verification",completed=1,total=2)),flush=True)\n'
        'print(json.dumps(dict(event="speaker_verification",completed=2,total=2)),flush=True)\n'
        'open(sys.argv[-1],"w").write(json.dumps(dict(units=[])))\n')
    monkeypatch.setattr(config,'BASE_DIR',str(tmp_path))
    monkeypatch.setattr(config,'WHISPERX_PYTHON',sys.executable,raising=False)
    updates=[]
    monkeypatch.setattr(task_manager,'get_task',lambda task_id:{'type':'analysis'})
    monkeypatch.setattr(task_manager,'update_task',lambda task_id,**kw:updates.append(kw))
    result=_verify_source_speakers('unused.wav',{'segments':[]},{'words':[]},str(tmp_path),'signature','test')
    assert result=={'units':[]}
    assert [u['progress'] for u in updates]==[80,87,95]
    assert 'test' not in task_manager.task_subprocesses
    count=len(updates)
    assert _verify_source_speakers('unused.wav',{}, {}, str(tmp_path),'signature','test')==result
    assert len(updates)==count
