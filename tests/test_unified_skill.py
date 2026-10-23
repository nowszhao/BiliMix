"""Offline workflow regression cases; these never contact a running BiliMix."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture
def language(monkeypatch):
    scripts = Path(__file__).resolve().parents[1]/'skills/nows-ytb-dubbing/scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('unified_language', scripts/'_language.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source():
    texts = ['We', 'can', 'finish', 'this', 'sentence.', 'Yes.']
    return dict(schema_version=1, analysis_id='analysis', source_media_sha256='hash',
                words=[dict(word_id=i, text=t, start=i*.5, end=(i+1)*.5,
                            speaker='A' if i<5 else 'B', speaker_candidates=[]) for i,t in enumerate(texts)])


def test_full_tail_is_consumed_in_next_window_without_splitting_or_repeating(language, tmp_path):
    analysis = source()
    first = language.advance_language(tmp_path, analysis, 'dubbing', {}, chunk_words=3)
    request = json.loads(Path(first['input']).read_text())
    Path(first['output']).write_text(json.dumps(dict(input_id=request['input_id'],
                                                   sentences=[{'text':'We can finish this sentence.'}])))
    assert language.advance_language(tmp_path, analysis, 'dubbing', {}, chunk_words=3)['stage']=='advance'
    second = language.advance_language(tmp_path, analysis, 'dubbing', {}, chunk_words=3)
    request = json.loads(Path(second['input']).read_text())
    assert [w['word_id'] for w in request['words']]==[5]
    assert request['previous_sentences'][-1]['text']=='We can finish this sentence.'


def test_full_rejects_cross_speaker_merge_and_word_loss(language):
    analysis = source()
    request = dict(input_id='input', words=analysis['words'])
    for text in ('We can finish this sentence. Yes.', 'We finish this sentence.'):
        with pytest.raises(ValueError):
            language.apply_full(request,dict(input_id='input',sentences=[{'text':text}]),analysis,'dubbing')


def test_translation_groups_only_same_speaker_adjacent_sources(language):
    analysis = source()
    request = dict(input_id='input', source_units=[
        dict(unit_id='s1',source_word_ids=[0,1]),dict(unit_id='s2',source_word_ids=[2,3,4]),
        dict(unit_id='s3',source_word_ids=[5])])
    unit = dict(source_unit_ids=['s1','s2'],text_en='We can finish this sentence.',text_zh='我们能把这句话说完整。')
    accepted = language.apply_translation(request,dict(input_id='input',units=[unit]),analysis,'dubbing')
    assert accepted[0]['source_word_ids']==list(range(5))
    unit['source_unit_ids']=['s1','s2','s3']
    with pytest.raises(ValueError,match='speaker'):
        language.apply_translation(request,dict(input_id='input',units=[unit]),analysis,'dubbing')


@pytest.mark.skipif(sys.version_info < (3,10), reason='Unified workflow requires Python 3.10+')
def test_local_project_status_performs_no_remote_operation(tmp_path, monkeypatch, capsys):
    scripts=Path(__file__).resolve().parents[1]/'skills/nows-ytb-dubbing/scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('unified_workflow',scripts/'workflow.py')
    workflow=importlib.util.module_from_spec(spec);spec.loader.exec_module(workflow)
    (tmp_path/'project.json').write_text(json.dumps({'schema_version':1,'next':{'stage':'full'}}))
    def unexpected(*args,**kwargs):raise AssertionError('status must not connect')
    monkeypatch.setattr(workflow,'Client',unexpected)
    monkeypatch.setattr(sys,'argv',['workflow.py','status','--workdir',str(tmp_path)])
    assert workflow.main()==0
    assert json.loads(capsys.readouterr().out)['network_accessed'] is False


def test_ambiguous_submission_is_saved_before_post_and_blocks_automatic_replay(tmp_path, monkeypatch):
    scripts=Path(__file__).resolve().parents[1]/'skills/nows-ytb-dubbing/scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('unified_submit',scripts/'workflow.py')
    workflow=importlib.util.module_from_spec(spec);spec.loader.exec_module(workflow)
    video=tmp_path/'source.mp4';video.write_bytes(b'video')
    stat=video.stat()
    project=dict(project_id='project',input=str(video),mode='dubbing',
                 video=dict(path=str(video),sha256=workflow.file_hash(video)),
                 source_stat=[stat.st_size,stat.st_mtime_ns])
    class LostReply:
        def call(self,*args):
            if args == ('task', 'list'):
                return {'tasks': []}
            raise ValueError('connection lost after POST')
    with pytest.raises(ValueError,match='lost'):
        workflow.submit(tmp_path,project,LostReply(),'analysis',['video','analyze'],'hash')
    saved=json.loads((tmp_path/'project.json').read_text())
    assert saved['pending_submission']['kind']=='analysis'
    def unexpected(*args,**kwargs):raise AssertionError('must recover before contacting server')
    monkeypatch.setattr(workflow,'Client',unexpected)
    action=workflow.run_project(tmp_path,saved,None)
    assert action['stage']=='submission_unknown'


@pytest.mark.parametrize('metadata,expected', [({'title':'YouTube Original Title'},'YouTube Original Title'),
                                              ({},'local interview')])
def test_submission_uses_video_title_without_machine_identifier(tmp_path, monkeypatch, metadata, expected):
    scripts=Path(__file__).resolve().parents[1]/'skills/nows-ytb-dubbing/scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('unified_title',scripts/'workflow.py')
    workflow=importlib.util.module_from_spec(spec);spec.loader.exec_module(workflow)
    project=dict(project_id='internal',metadata=metadata,video={'path':'local interview.mp4'})
    calls=[]
    class Client:
        def call(self,*args):
            calls.append(args)
            return {'tasks':[{'task_id':'previous'}]} if args == ('task','list') else {'task_id':'new'}
    workflow.submit(tmp_path,project,Client(),'analysis',['video','analyze'],'hash')
    assert calls[-1][-2:]==('--title',expected)
    assert project['analysis']['title']==expected


@pytest.mark.parametrize('mode', ['speaker', 'segment'])
def test_reference_mode_is_bound_to_project_and_preserved_on_resume(tmp_path, monkeypatch, mode):
    scripts=Path(__file__).resolve().parents[1]/'skills/nows-ytb-dubbing/scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('workflow_reference_mode',scripts/'workflow.py')
    workflow=importlib.util.module_from_spec(spec);spec.loader.exec_module(workflow)
    args=workflow.parser().parse_args(['run','--input',str(tmp_path/'source.mp4'),
                                     '--workdir',str(tmp_path),'--tts-reference-mode',mode])
    project=workflow.initialize(tmp_path,args)
    assert project['settings']['tts_reference_mode'] == mode
    resume=workflow.parser().parse_args(['run','--workdir',str(tmp_path)])
    assert workflow.initialize(tmp_path,resume)['settings']['tts_reference_mode'] == mode
    args.tts_reference_mode='speaker' if mode == 'segment' else 'segment'
    with pytest.raises(ValueError,match='tts_reference_mode'):
        workflow.initialize(tmp_path,args)
