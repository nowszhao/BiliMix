#!/usr/bin/env python3
"""Read-only dependency checks for portable clients or a model server.

No packages/models are installed, no tasks are submitted, no token is printed.
Use --scope server in the same Python environment as the backend service.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def command_result(command):
    try:
        result = subprocess.run(command,capture_output=True,text=True,timeout=30)
        return result.returncode == 0, result.stdout+result.stderr
    except (OSError,subprocess.SubprocessError) as error:
        return False,str(error)


def module_probe(python, names):
    code = "import importlib.util,json; names="+repr(names)+"; print(json.dumps({n:importlib.util.find_spec(n) is not None for n in names}))"
    ok,value = command_result([python,'-c',code])
    try:
        return json.loads(value) if ok else {}
    except ValueError:
        return {}


def inspect(scope='client', server=None):
    checks = []
    def add(name,ok,detail):
        checks.append({'name':name,'ok':bool(ok),'detail':detail})
    add('python',sys.version_info >= (3,10),sys.version.split()[0]+' (client requires >=3.10)')
    if scope == 'client':
        add('cli',bool(importlib.util.find_spec('bilimix_cli') or shutil.which('bmx')),
            'Install with this Python: python -m pip install /path/to/BiliMix/sdk')
        add('yt-dlp',bool(importlib.util.find_spec('yt_dlp') or shutil.which('yt-dlp')),
            'Needed for URL download; local MP4 input does not need a downloader')
        add('ffmpeg',bool(os.environ.get('BILIMIX_FFMPEG') or shutil.which('ffmpeg') or importlib.util.find_spec('imageio_ffmpeg')),
            'Needed for local download/remux; server must have its own FFmpeg')
    else:
        sys.path.insert(0,str(ROOT))
        from core import config
        from pipeline.model_runtime import hub_cache_dir,model_snapshots,whisperx_command
        from pipeline.step3_tts_confucius import _get_confucius_root
        add('server_python',(3,10) <= sys.version_info[:2] <= (3,12),
            'Use Python 3.10-3.12 for the current speech stack; 3.10 recommended')
        for binary in ('ffmpeg','ffprobe'):
            ok,value = command_result([binary,'-version'])
            add(binary,ok,
                value.splitlines()[0] if value else 'FFmpeg/FFprobe >=7 required')
        with tempfile.TemporaryDirectory(prefix='bilimix-ffmpeg-check-') as temporary:
            graph = Path(temporary)/'graph.txt';graph.write_text('[0:v]null[outv]',encoding='utf-8')
            ok,_ = command_result(['ffmpeg','-v','error','-f','lavfi','-i','color=s=16x16:d=0.04',
                '-/filter_complex',str(graph),'-map','[outv]','-frames:v','1','-f','null','-'])
            add('ffmpeg_filter_file',ok,'Requires file-backed filter_complex (including compatible nightly builds)')
        ok,filters = command_result(['ffmpeg','-hide_banner','-filters'])
        add('ffmpeg_filters',ok and all(re.search(r'\s'+name+r'\s',filters) for name in ('ass','tpad','drawtext')),
            'Requires libass, tpad and drawtext')
        for label,python,names in [
            ('server_modules',sys.executable,['flask','pydub','soundfile','demucs']),
            ('asr_modules',getattr(config,'WHISPERX_PYTHON','') or sys.executable,['torch','whisperx','pyannote']),
            ('tts_modules',getattr(config,'CONFUCIUS4_TTS_PYTHON','') or sys.executable,['torch','transformers','soundfile'])]:
            found = module_probe(python,names)
            add(label,all(found.get(n,False) for n in names),{'python':python,'modules':found})
        command = whisperx_command(config.WHISPERX_BIN,getattr(config,"WHISPERX_PYTHON","") or sys.executable)
        ok,_ = command_result(command+['--help'])
        add('whisperx_command',ok,command)
        root = Path(_get_confucius_root())
        add('tts_source',(root/'confuciustts/cli/inference.py').is_file() and (root/'config/inference_config.yaml').is_file(),str(root))
        ok,font = command_result(['fc-match','-f','%{family}','Noto Sans CJK SC'])
        add('cjk_font',ok and 'Noto Sans CJK' in font,'Noto Sans CJK SC must be installed for predictable CJK layout')
        # Presence only: model access rights must be granted by the user's account.
        token_present = bool(os.environ.get('HF_TOKEN') or getattr(config,'WHISPERX_HF_TOKEN',''))
        add('diarization_access',token_present or bool(model_snapshots()),
            {'token_configured':token_present,'cache_directory':str(hub_cache_dir()),
             'note':'Cached files/token presence do not prove repository access or a complete model download'})
        add('data_directory',Path(config.DATA_DIR).is_dir() and os.access(config.DATA_DIR,os.W_OK),config.DATA_DIR)
    if server:
        try:
            from bilimix_cli.client import BiliMixClient
            client = BiliMixClient(server,quiet=True)
            result = client.get('/api')
            add('server_protocol',result.get('dubbing_protocol') == 1,{'server':server,'dubbing_protocol':result.get('dubbing_protocol')})
        except Exception as error:
            add('server_protocol',False,str(error))
    return {'status':'PASS' if all(c['ok'] for c in checks) else 'FAIL','scope':scope,'checks':checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope',choices=['client','server'],default='client')
    parser.add_argument('--server',help='Optionally check an existing server; never submit a task')
    args = parser.parse_args()
    try:
        report = inspect(args.scope,args.server)
    except Exception as error:
        report = {'status':'FAIL','scope':args.scope,'error':str(error)}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
