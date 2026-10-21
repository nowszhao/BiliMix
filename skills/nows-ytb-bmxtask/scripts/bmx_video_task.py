#!/usr/bin/env python3
"""Submit refined video dubbing using any Python/Agent installation.

stdout is JSON; diagnostics go to stderr. --dry-run performs no network or
writes. Completed uploads/analysis and submission receipts are server-scoped.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

DEBUG = False


def log(message):
    print('[bmx_video_task] '+str(message), file=sys.stderr, flush=True)


def as_command(value):
    return [value] if isinstance(value,str) else list(value)


def run(command, dry_run):
    log(subprocess.list2cmdline(command))
    if dry_run:
        return subprocess.CompletedProcess(command,0,'','')
    return subprocess.run(command,capture_output=True,text=True,encoding="utf-8",
                          env={**os.environ,"PYTHONUTF8":"1"})


def bmx_json(bmx, args, dry_run, field=None, retries=1):
    command = [*as_command(bmx),*args]
    if field:
        command += ['--field',field]
    for attempt in range(retries):
        result = run(command,dry_run)
        if dry_run:
            return None, '<dry-run:'+str(field)+'>'
        text = (result.stdout or '').strip()
        if result.returncode == 0 and text:
            if field:
                return None,text
            try:
                return json.loads(text),None
            except ValueError:
                log('CLI did not return JSON; update bilimix-cli')
        if DEBUG or attempt == retries-1:
            log((result.stderr or text)[-600:])
        if attempt+1 < retries:
            time.sleep(1)
    return None,None


def resolve_bmx(explicit=None):
    if explicit or os.environ.get('BILIMIX_CLI'):
        return explicit or os.environ['BILIMIX_CLI']
    suffix = '.exe' if os.name == 'nt' else ''
    candidate = Path(sys.executable).parent/('bmx'+suffix)
    if candidate.is_file() and os.access(candidate,os.X_OK):
        return str(candidate)
    if importlib.util.find_spec('bilimix_cli'):
        return [sys.executable,'-m','bilimix_cli']
    return shutil.which('bmx')


def canonical_server(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError('Supply an HTTP(S) server URL without embedded credentials')
    return urlunsplit((parsed.scheme.lower(),parsed.netloc.lower(),parsed.path.rstrip('/'),'',''))


def file_hash(path):
    h = hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def save_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.replace(path)


def emit(value):
    print(json.dumps(value,ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video',required=True)
    parser.add_argument('--subtitle',help='External refined EN || ZH ASS')
    parser.add_argument('--dub-manifest')
    parser.add_argument('--workdir',help='State/output directory; default: video directory')
    parser.add_argument('--server',help='Override CLI/config server for every operation')
    parser.add_argument('--bmx',help='CLI path; alternatively BILIMIX_CLI or python -m bilimix_cli')
    parser.add_argument('--analyze-only',action='store_true')
    parser.add_argument('--analysis-out')
    parser.add_argument('--title')
    parser.add_argument('--duration')
    parser.add_argument('--subtitle-mode',choices=['bilingual','chinese_only','none'],default='bilingual')
    parser.add_argument('--subtitle-font-size',type=int,default=40)
    parser.add_argument('--ref-select-mode',choices=['speaker_global','speaker_local','segment'],default='speaker_global')
    parser.add_argument('--keep-bgm',action='store_true')
    parser.add_argument('--no-submit',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--fresh-upload',action='store_true',help='Ignore the saved upload/analysis binding')
    parser.add_argument('--new-task',action='store_true',help='Explicitly create another dubbing job')
    parser.add_argument('--debug',action='store_true')
    args = parser.parse_args()
    global DEBUG
    DEBUG = args.debug
    if not args.analyze_only and not args.subtitle:
        parser.error('--subtitle is required for external refined dubbing')
    if args.subtitle_font_size <= 0:
        parser.error('--subtitle-font-size must be positive')
    try:
        bmx = resolve_bmx(args.bmx) or ('bmx' if args.dry_run else None)
        if not bmx:
            raise ValueError('Install bilimix-cli in this Python environment or specify --bmx')
        bmx = as_command(bmx)
        if args.server:
            bmx += ['--server',canonical_server(args.server)]
        manifest = load_json(Path(args.dub_manifest)) if args.dub_manifest else None
        if args.dub_manifest and not manifest:
            raise ValueError('Dubbing manifest is missing or empty')
        _,protocol = bmx_json(bmx,['api'],args.dry_run,field='dubbing_protocol')
        if not args.dry_run and protocol != '1':
            raise ValueError('BiliMix audio-first protocol 1 is required; update server/CLI')
        auth,_ = bmx_json(bmx,['auth','status'],args.dry_run)
        if not args.dry_run and (not auth or not auth.get('authenticated')):
            raise ValueError('Server unavailable or unauthenticated; run bmx auth login')
        identity = (auth or {}).get('server') or args.server or os.environ.get('BILIMIX_SERVER')
        if not identity and not args.dry_run:
            raise ValueError('CLI must report its server; update bilimix-cli or supply --server')
        server = canonical_server(identity) if identity else '<dry-run-server>'
        video = Path(args.video).expanduser().resolve()
        work = Path(args.workdir).expanduser().resolve() if args.workdir else video.parent
        state_path,receipt_path = work/'analysis_state.json',work/'dubbing_state.json'
        fingerprint = file_hash(video) if not args.dry_run else 'dry-run'
        if manifest and not args.dry_run and manifest.get('source_media_sha256') != fingerprint:
            raise ValueError('Manifest source hash does not match the video')
        state = load_json(state_path) if not args.dry_run else {}
        if (args.fresh_upload or state.get('server') != server
                or state.get('source_media_sha256') != fingerprint):
            state = {'server':server,'source_media_sha256':fingerprint}
        request = {'server':server,'media':fingerprint,
            'manifest':file_hash(args.dub_manifest) if args.dub_manifest and not args.dry_run else None,
            'subtitle':file_hash(args.subtitle) if args.subtitle and not args.dry_run else None,
            'mode':args.subtitle_mode,'font_size':args.subtitle_font_size,'keep_bgm':args.keep_bgm,
            'ref_select_mode':args.ref_select_mode,'title':args.title,'duration':args.duration}
        request_hash = hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        receipt = load_json(receipt_path) if not args.dry_run else {}
        if not args.analyze_only and not args.no_submit and not args.new_task and receipt.get('request_hash') == request_hash:
            status,_ = bmx_json(bmx,['task','status',receipt['task_id']],False)
            if status is None:
                raise ValueError('Saved task could not be checked; inspect it before using --new-task')
            emit({**receipt,'reused':True,'status':status.get('status')});return 0
        pending_path = work/'submission_pending.json'
        if pending_path.exists() and not args.analyze_only and not args.no_submit and not args.new_task and not args.dry_run:
            raise ValueError('An earlier submit has no confirmed receipt. Check server task history before --new-task')
        paths = {'video':state.get('video_server_path')}
        if not paths['video']:
            _,paths['video'] = bmx_json(bmx,['audio','upload',str(video)],args.dry_run,field='local_path')
            if not paths['video']:
                raise ValueError('Video upload failed')
            state['video_server_path'] = paths['video']
            if not args.dry_run:
                save_json(state_path,state)
        if args.analyze_only:
            destination = Path(args.analysis_out).expanduser().resolve() if args.analysis_out else work/'analysis.json'
            if args.dry_run:
                bmx_json(bmx,['video','analyze','--server-path',paths['video']],True)
                emit({'dry_run':True,'action':'analyze','analysis_path':str(destination)});return 0
            if not state.get('analysis_id'):
                result,_ = bmx_json(bmx,['video','analyze','--server-path',paths['video']],False)
                if not result or not result.get('task_id'):
                    raise ValueError('Analysis submission failed')
                state['analysis_id'] = result['task_id'];save_json(state_path,state)
            task = state['analysis_id']
            waited,_ = bmx_json(bmx,['task','wait',task],False)
            if waited is None:
                raise ValueError('Analysis wait failed; resume the saved task instead of resubmitting')
            result,_ = bmx_json(bmx,['task','result',task],False)
            analysis = (result or {}).get('result',{}).get('analysis')
            if not analysis or analysis.get('source_media_sha256') != fingerprint or analysis.get('analysis_id') != task:
                raise ValueError('Analysis incomplete/failed or source identity changed; inspect the saved task')
            save_json(destination,analysis)
            emit({'analysis_id':task,'analysis_path':str(destination),'state_path':str(state_path),'server':server});return 0
        _,paths['subtitle'] = bmx_json(bmx,['audio','upload',str(Path(args.subtitle).expanduser().resolve())],args.dry_run,field='local_path')
        if not paths['subtitle']:
            raise ValueError('Subtitle upload failed')
        parsed,_ = bmx_json(bmx,['subtitle','parse',paths['subtitle']],args.dry_run,retries=3)
        if not args.dry_run and (not parsed or not parsed.get('count') or parsed.get('count') != parsed.get('bilingual_count')):
            raise ValueError('External ASS must contain nonempty bilingual EN || ZH rows')
        if args.no_submit:
            emit({'video_server_path':paths['video'],'subtitle_server_path':paths['subtitle'],'server':server});return 0
        command = ['task','submit','--type','video','--server-path',paths['video'],
            '--subtitle-path',paths['subtitle'],'--subtitle-mode',args.subtitle_mode,
            '--subtitle-font-size',str(args.subtitle_font_size),'--ref-select-mode',args.ref_select_mode,
            '--title',args.title or video.stem]
        if manifest:
            command += ['--dub-manifest',str(Path(args.dub_manifest).resolve()),'--analysis-id',manifest['analysis_id']]
        if args.duration:
            command += ['--duration',args.duration]
        if args.keep_bgm:
            command += ['--keep-bgm']
        if not args.dry_run:
            save_json(pending_path,{'server':server,'request_hash':request_hash,'request':request})
        result,_ = bmx_json(bmx,command,args.dry_run)
        if args.dry_run:
            emit({'dry_run':True,'action':'submit','request':request});return 0
        if not result or not result.get('task_id'):
            raise ValueError('Submission did not return task_id; check server history before retrying')
        receipt = {'task_id':result['task_id'],'server':server,'request_hash':request_hash,
            'source_media_sha256':fingerprint,'video_server_path':paths['video'],
            'subtitle_server_path':paths['subtitle']}
        save_json(receipt_path,receipt)
        pending_path.unlink(missing_ok=True)
        emit(receipt);return 0
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        log(error);return 1


if __name__ == '__main__':
    from portable_io import configure_utf8
    configure_utf8()
    raise SystemExit(main())
