"""Run shipped clients from a clean HOME/foreign cwd against disposable APIs."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT/'skills'
WRAPPER = SKILLS/'nows-ytb-bmxtask/scripts/bmx_video_task.py'


class TestServer:
    __test__ = False
    def __init__(self,name,media_hash):
        self.name,self.media_hash = name,media_hash
        self.uploads,self.analysis_jobs,self.video_jobs = 0,0,[]
        self.fail_submission = False
        state = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,value,status=200):
                body=json.dumps(value).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_GET(self):
                if self.path=='/api':return self.reply({'dubbing_protocol':1})
                if self.path=='/api/auth/check':return self.reply({'authenticated':True,'auth_enabled':False})
                if self.path.endswith('/result'):
                    return self.reply({'status':'completed','result':{'analysis':{
                        'analysis_id':'analysis-'+state.name,'source_media_sha256':state.media_hash,
                        'words':[dict(word_id=0,speaker='A',text='Hello',start=0,end=1)]}}})
                if self.path.startswith('/api/task/'):
                    return self.reply({'status':'completed','progress':100})
                self.reply({'error':'not found'},404)
            def do_POST(self):
                data=self.rfile.read(int(self.headers.get('Content-Length','0')))
                if self.path=='/api/upload':
                    state.uploads+=1
                    return self.reply({'local_path':f'/srv/{state.name}/upload_{state.uploads}'})
                if self.path=='/api/parse-subtitle':return self.reply({'count':1,'bilingual_count':1})
                if self.path=='/api/submit':
                    body=json.loads(data)
                    if body.get('type')=='analysis':
                        state.analysis_jobs+=1
                        return self.reply({'task_id':'analysis-'+state.name})
                    state.video_jobs.append(body)
                    if state.fail_submission:return self.reply({'error':'response unavailable'},503)
                    return self.reply({'task_id':'video-'+state.name+'-'+str(len(state.video_jobs))})
                self.reply({'error':'not found'},404)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url='http://127.0.0.1:'+str(self.http.server_port)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
    def close(self):
        self.http.shutdown();self.http.server_close();self.thread.join(timeout=5)


@pytest.fixture
def client_work(tmp_path):
    work=tmp_path/'Agent 工作目录 with spaces';work.mkdir()
    media=work/'video 中文.mp4';media.write_bytes(b'portable client contract media')
    subtitle=work/'translated.work.ass';subtitle.write_text('[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,A,0,0,0,,Hello || 你好。\n',encoding='utf-8')
    env={**os.environ,'PYTHONPATH':str(ROOT/'sdk'),'BILIMIX_HOME':str(tmp_path/'clean-client-home'),
         'HOME':str(tmp_path/'fresh-home'),'USERPROFILE':str(tmp_path/'fresh-home'),'PYTHONUTF8':'1'}
    for name in ('BILIMIX_CLI','BILIMIX_SERVER','BILIMIX_SKILLS_DIR'):env.pop(name,None)
    foreign=tmp_path/'unrelated cwd';foreign.mkdir()
    def invoke(*args,ok=True):
        result=subprocess.run([sys.executable,str(WRAPPER),'--video',str(media),'--workdir',str(work),*map(str,args)],
            cwd=foreign,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
        if ok:assert result.returncode==0,result.stderr
        return result
    return work,media,subtitle,env,invoke


def test_fresh_agent_analyze_submit_resume_and_switch_servers(client_work):
    work,media,subtitle,env,invoke=client_work
    servers=[TestServer(n,hashlib.sha256(media.read_bytes()).hexdigest())for n in ['one','two']]
    try:
        first,second=servers
        analysis=json.loads(invoke('--server',first.url,'--analyze-only').stdout)
        assert analysis['analysis_id']=='analysis-one' and first.analysis_jobs==1
        invoke('--server',first.url,'--analyze-only')
        assert first.analysis_jobs==1 and first.uploads==1
        manifest={'schema_version':1,'analysis_id':analysis['analysis_id'],'source_media_sha256':first.media_hash,
            'units':[dict(unit_id='u1',speaker_id='A',text_zh='你好。',source_word_ids=[0])]}
        path=work/'dub_manifest.json';path.write_text(json.dumps(manifest),encoding='utf-8')
        submitted=json.loads(invoke('--server',first.url,'--subtitle',subtitle,'--dub-manifest',path).stdout)
        reused=json.loads(invoke('--server',first.url,'--subtitle',subtitle,'--dub-manifest',path).stdout)
        assert submitted['task_id']==reused['task_id'] and reused['reused']
        assert len(first.video_jobs)==1 and first.uploads==2
        request=first.video_jobs[0]
        assert request['subtitle_path'].startswith('/srv/one/') and request['local_path'].startswith('/srv/one/')
        assert request['dub_manifest']==manifest and request['analysis_id']=='analysis-one'
        invoke('--server',second.url,'--analyze-only')
        assert second.uploads==second.analysis_jobs==1
        state=json.loads((work/'analysis_state.json').read_text(encoding='utf-8'))
        assert state['server']==second.url and state['video_server_path'].startswith('/srv/two/')
    finally:
        for server in servers:server.close()


def test_unknown_submit_response_is_not_automatically_replayed(client_work):
    work,media,subtitle,env,invoke=client_work
    server=TestServer('uncertain',hashlib.sha256(media.read_bytes()).hexdigest());server.fail_submission=True
    try:
        first=invoke('--server',server.url,'--subtitle',subtitle,ok=False)
        assert first.returncode!=0 and len(server.video_jobs)==1
        assert (work/'submission_pending.json').is_file()
        second=invoke('--server',server.url,'--subtitle',subtitle,ok=False)
        assert second.returncode!=0 and len(server.video_jobs)==1
        assert 'no confirmed receipt' in second.stderr
    finally:server.close()


def test_dry_run_requires_no_cli_home_or_network_and_writes_nothing(client_work):
    work,media,subtitle,env,invoke=client_work
    before=set(work.iterdir())
    result=invoke('--server','http://127.0.0.1:1','--subtitle',subtitle,'--dry-run')
    assert json.loads(result.stdout)['dry_run'] is True
    assert set(work.iterdir())==before
    assert not Path(env['BILIMIX_HOME']).exists()


def test_windows_and_unix_venv_paths_do_not_depend_on_an_agent():
    module=runpy.run_path(str(SKILLS/'nows-ytb-bilingualex/scripts/check_env.py'))
    resolver=module['venv_executable']
    assert Path(resolver('tools','python',True)).parts[-2:]==('Scripts','python.exe')
    assert Path(resolver('tools','python',False)).parts[-2:]==('bin','python')


def test_filename_is_legal_on_windows_and_utf8_filesystems():
    module=runpy.run_path(str(SKILLS/'nows-ytb-bilingualex/scripts/download.py'))
    sanitize=module['sanitize']
    assert sanitize('CON')=='_CON'
    assert not sanitize('trailing. ').endswith(('.', ' '))
    assert len(sanitize('中文'*200).encode('utf-8'))<=160
    assert not any(c in sanitize('A:B/C?D*')for c in ':/*?')


def test_client_auth_sessions_are_isolated_by_server(tmp_path,monkeypatch):
    sys.path.insert(0,str(ROOT/'sdk'))
    from bilimix_cli import cli
    monkeypatch.setattr(cli,'SESSION_DIR',tmp_path)
    (tmp_path/'session.json').write_text('{"session":"legacy-unowned"}',encoding='utf-8')
    first=cli.BiliMixClient('https://one.invalid')
    assert not first.session.cookies
    first.session.cookies.set('session','one');first._save_cookies()
    second=cli.BiliMixClient('https://two.invalid')
    assert not second.session.cookies
    assert cli.BiliMixClient('https://one.invalid').session.cookies.get('session')=='one'


def test_packages_install_in_an_arbitrary_directory_with_no_local_state(tmp_path):
    install=runpy.run_path(str(ROOT/'scripts/install_skills.py'))['install']
    target=tmp_path/'another Agent'/'skills'
    result=install(target)
    assert len(result['installed'])==2
    check=target/'nows-ytb-bilingualex/scripts/check_env.py'
    output=subprocess.run([sys.executable,str(check),'--help'],cwd=tmp_path,capture_output=True,text=True,timeout=10)
    assert output.returncode==0
    backup=install(target,True)['backup'];assert Path(backup).is_dir()
    package=runpy.run_path(str(ROOT/'scripts/package_skills.py'))['package']
    archives=package(tmp_path/'distributable')
    import zipfile
    for item in archives:
        with zipfile.ZipFile(item['path'])as archive:
            assert not any('__pycache__' in n or n.endswith('.json')for n in archive.namelist())


def test_model_cache_respects_portable_huggingface_environment(tmp_path):
    from pipeline.model_runtime import hub_cache_dir,configure_diarization_environment
    env={'HF_HOME':str(tmp_path/'HF home')}
    assert hub_cache_dir(env)==tmp_path/'HF home/hub'
    snapshot=hub_cache_dir(env)/'models--pyannote--speaker-diarization-community-1/snapshots/revision'
    for item in ['config.yaml','embedding/pytorch_model.bin','segmentation/pytorch_model.bin','plda/plda.npz']:
        p=snapshot/item;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'cache fixture')
    configure_diarization_environment(env)
    assert env['HF_HUB_OFFLINE']=='1'
    custom={**env,'BILIMIX_DIARIZATION_MODEL':'someone/another-model'};custom.pop('HF_HUB_OFFLINE')
    configure_diarization_environment(custom)
    assert 'HF_HUB_OFFLINE' not in custom


def test_asr_uses_selected_runtime_instead_of_a_foreign_path(tmp_path):
    from pipeline.model_runtime import whisperx_command
    runtime=tmp_path/'model runtime';runtime.mkdir()
    executable=runtime/('whisperx.exe'if os.name=='nt'else'whisperx');executable.write_bytes(b'launcher')
    assert whisperx_command('whisperx',str(runtime/'python'))==[str(executable)]
    executable.unlink()
    assert whisperx_command('whisperx',str(runtime/'python'))==[str(runtime/'python'),'-m','whisperx']
    assert whisperx_command(['custom-python','-m','whisperx'])==['custom-python','-m','whisperx']


def test_standalone_subtitles_run_without_site_packages_or_sdk(tmp_path):
    data=json.loads((ROOT/'tests/fixtures/long_utterance.json').read_text(encoding='utf-8'))
    source=tmp_path/'working.ass';source.write_text('Dialogue: 0,0:00:00.00,0:00:10.00,Default,A,0,0,0,,'+data['text_en']+' || '+data['text_zh']+'\n',encoding='utf-8')
    output=tmp_path/'观看字幕.ass'
    result=subprocess.run([sys.executable,'-S',str(SKILLS/'nows-ytb-bilingualex/scripts/render_subtitles.py'),
        '--input',str(source),'--output',str(output),'--width','1280','--height','720'],
        cwd=tmp_path,capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['display_pages']>1 and report['max_lines']<=4 and report['max_estimated_height_ratio']<=.25
    assert source.is_file() and output.is_file()


def test_standalone_overlapping_source_cues_fail_before_writing(tmp_path):
    source=tmp_path/'overlap.ass';source.write_text(
        'Dialogue: 0,0:00:00.00,0:00:02.00,Default,A,0,0,0,,Hello || 你好。\n'
        'Dialogue: 0,0:00:01.00,0:00:03.00,Default,B,0,0,0,,Yes || 是的。\n',encoding='utf-8')
    output=tmp_path/'result.ass'
    result=subprocess.run([sys.executable,'-S',str(SKILLS/'nows-ytb-bilingualex/scripts/render_subtitles.py'),
        '--input',str(source),'--output',str(output),'--width','1280','--height','720'],
        cwd=tmp_path,capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode!=0 and 'Overlapping' in result.stderr and not output.exists()


@pytest.mark.parametrize('produced',[True,False])
def test_downloader_uses_selected_ffmpeg_without_browser_or_wrong_video(tmp_path,monkeypatch,produced):
    module=runpy.run_path(str(SKILLS/'nows-ytb-bilingualex/scripts/download.py'))
    namespace=module['main'].__globals__
    envfile=tmp_path/'yt_env.json';ffmpeg=str(tmp_path/'tool bin/ffmpeg')
    envfile.write_text(json.dumps({'yt_dlp_command':['selected-python','-m','yt_dlp'],'ffmpeg':ffmpeg}),encoding='utf-8')
    (tmp_path/'unrelated.mp4').write_bytes(b'not this video')
    calls=[]
    def fake_run(command,**kwargs):
        calls.append(command)
        if '--dump-single-json' in command:
            return subprocess.CompletedProcess(command,0,json.dumps({'title':'New Video','id':'newid','duration':4}),'')
        destination=tmp_path/'New Video_newid.mp4'
        if produced:destination.write_bytes(b'requested output')
        return subprocess.CompletedProcess(command,0,str(destination)if produced else '','')
    monkeypatch.setitem(namespace,'run',fake_run)
    monkeypatch.setattr(sys,'argv',['download.py','--url','https://example.invalid/video','--env-out',str(envfile),'--output',str(tmp_path)])
    status=module['main']()
    assert '--cookies-from-browser' not in calls[0] and '--cookies-from-browser' not in calls[1]
    assert calls[1][calls[1].index('--ffmpeg-location')+1]==ffmpeg
    if produced:
        assert status==0 and json.loads((tmp_path/'manifest.json').read_text(encoding='utf-8'))['video_path'].endswith('New Video_newid.mp4')
    else:
        assert status!=0 and not (tmp_path/'manifest.json').exists()
