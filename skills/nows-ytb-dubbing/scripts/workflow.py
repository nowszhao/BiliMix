#!/usr/bin/env python3
"""One local entry point. The current Agent supplies FULL and translation files."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from _bmx import Client, canonical_server
from _export import export_work, quality, validate_analysis
from _language import advance_language
from _media import prepare_video, setup, source_subtitles, subtitle_analysis, video_size
from _project import digest, file_hash, project_lock, read_json, write_json
from _review import create_review

SKILL = Path(__file__).resolve().parents[1]
FAILED = {'error', 'failed', 'cancelled'}


def save(work, project, action=None):
    if action is not None:
        project['next'] = action
    write_json(work / 'project.json', project)


def initialize(work, args):
    project = read_json(work / 'project.json')
    if project is None:
        if not args.input:
            raise ValueError('A new project requires --input <YouTube URL or video>')
        value = args.input if args.input.startswith(('http://', 'https://')) else str(Path(args.input).expanduser().resolve())
        project = dict(schema_version=1, project_id=uuid.uuid4().hex, input=value,
                       mode=args.mode or 'dubbing', server=canonical_server(args.server) if args.server else None,
                       source_subtitles=str(Path(args.source_subtitles).expanduser().resolve()) if args.source_subtitles else None,
                       settings=dict(quality=args.quality or '720', font_size=args.font_size or 40,
                                     keep_bgm=bool(args.keep_bgm), cookies_browser=args.cookies_browser,
                                     chunk_words=args.chunk_words or 650,
                                     cookies_file=str(Path(args.cookies_file).expanduser().resolve()) if args.cookies_file else None,
                                     dimensions=[args.width, args.height] if args.width and args.height else None))
        save(work, project)
    else:
        if project.get('schema_version') != 1:
            raise ValueError('Unsupported project schema')
        if args.mode and args.mode != project['mode']:
            raise ValueError('Use a different workdir for another output mode')
        if args.server and project.get('server') and canonical_server(args.server) != project['server']:
            raise ValueError('A project is bound to its server; use a different workdir')
        if args.server:
            project['server'] = canonical_server(args.server)
        if args.input:
            value = args.input if args.input.startswith(('http://', 'https://')) else str(Path(args.input).expanduser().resolve())
            if value != project['input']:
                raise ValueError('Use a different workdir for another input')
        options = {'quality': args.quality, 'font_size': args.font_size, 'keep_bgm': args.keep_bgm,
                   'cookies_browser': args.cookies_browser, 'cookies_file': args.cookies_file,
                   'chunk_words': args.chunk_words}
        if args.cookies_file:
            options['cookies_file'] = str(Path(args.cookies_file).expanduser().resolve())
        if args.width and args.height:
            options['dimensions'] = [args.width, args.height]
        if args.source_subtitles:
            subtitle = str(Path(args.source_subtitles).expanduser().resolve())
            if subtitle != project.get('source_subtitles'):
                raise ValueError('Source subtitles cannot change inside an existing project')
        for key, value in options.items():
            if value is not None and value != project['settings'].get(key):
                raise ValueError(f'Project setting {key} differs; use the saved settings or a new workdir')
    return project


def submit(work, project, client, kind, command, request_identity):
    """Record intent before POST. An unacknowledged POST requires recovery."""
    title = ((project.get('metadata') or {}).get('title')
             or Path(project['video']['path']).stem).strip()
    history = client.call('task', 'list').get('tasks')
    if not isinstance(history, list):
        raise ValueError('Cannot establish pre-submit task history; no task was submitted')
    # Display the real video title. Recovery uses source/type and the set of
    # pre-existing task IDs, rather than putting a machine identifier in it.
    project['pending_submission'] = dict(kind=kind, title=title, request_identity=request_identity,
                                         existing_task_ids=[t['task_id'] for t in history])
    save(work, project)
    result = client.call(*command, '--title', title)
    if not result.get('task_id'):
        raise ValueError('Submission returned no task_id; inspect server history before recovering')
    project[kind] = dict(task_id=result['task_id'], title=title, request_identity=request_identity)
    project.pop('pending_submission')
    save(work, project)


def poll(client, kind, job):
    task = client.call('task', 'status', job['task_id'])
    if task.get('status') == 'completed':
        return None
    return dict(stage=f'{kind}_failed' if task.get('status') in FAILED else f'{kind}_waiting',
                action='inspect_or_retry' if task.get('status') in FAILED else 'wait_then_run',
                task_id=job['task_id'], status=task.get('status'), progress=task.get('progress'),
                step=task.get('step'), message=task.get('message'), instructions='references/operations.md#recovery')


def load_remote_analysis(work, project, client):
    task_id = project['analysis']['task_id']
    result = client.call('task', 'result', task_id)
    analysis = (result.get('result') or {}).get('analysis')
    validate_analysis(analysis, project['video']['sha256'])
    if analysis['analysis_id'] != task_id:
        raise ValueError('Server returned another analysis task')
    write_json(work / 'analysis.json', analysis)
    return analysis


def run_project(work, project, args):
    video = project.get('video')
    if not video:
        video, metadata = prepare_video(work, project)
        project.update(video=video, metadata=dict(title=metadata.get('title', ''),
                       description=metadata.get('description', ''), chapters=metadata.get('chapters', []),
                       width=metadata.get('width'), height=metadata.get('height')))
        stat = Path(video['path']).stat()
        project['source_stat'] = [stat.st_size, stat.st_mtime_ns]
        save(work, project)
    path = Path(video['path'])
    stat = path.stat()
    if project.get('source_stat') != [stat.st_size, stat.st_mtime_ns]:
        if file_hash(path) != video['sha256']:
            raise ValueError('Source video changed; use a new project')
        project['source_stat'] = [stat.st_size, stat.st_mtime_ns]
        save(work, project)
    if project.get('pending_submission'):
        return dict(stage='submission_unknown', action='recover_existing_task',
                    submission=project['pending_submission'], instructions='references/operations.md#recovery')
    client = None
    analysis = read_json(work / 'analysis.json')
    if project['mode'] == 'dubbing':
        client = Client(work, project.get('server'))
        if project.get('server') and client.server != project['server']:
            raise ValueError('SDK connected to another server')
        project['server'] = client.server
        if not project.get('video_server_path'):
            project['video_server_path'] = client.upload(video['path'])
            save(work, project)
        if not project.get('analysis'):
            submit(work, project, client, 'analysis',
                   ['video', 'analyze', '--server-path', project['video_server_path']], video['sha256'])
        waiting = poll(client, 'analysis', project['analysis'])
        if waiting:
            return waiting
        if analysis is None or (project.get('next') or {}).get('stage') == 'speaker_review':
            analysis = load_remote_analysis(work, project, client)
        if analysis.get('analysis_id') != project['analysis']['task_id']:
            raise ValueError('Local analysis is not bound to this project')
    elif analysis is None:
        subtitle = source_subtitles(work, project)
        analysis = subtitle_analysis(subtitle, video['sha256'])
        write_json(work / 'analysis.json', analysis)
        project['source_subtitles_sha256'] = file_hash(subtitle)
        save(work, project)
    validate_analysis(analysis, video['sha256'])
    while True:
        action = advance_language(work, analysis, project['mode'], project.get('metadata', {}),
                                  project['settings']['chunk_words'])
        if not action or action['stage'] != 'advance':
            break
    if action:
        return action
    dimensions = project['settings'].get('dimensions')
    if not dimensions:
        dimensions = video_size(video['path'], work, project.get('metadata'))
        project['settings']['dimensions'] = list(dimensions)
    artifacts = export_work(work, analysis, project['mode'], dimensions, project['settings']['font_size'])
    project['artifacts'] = artifacts
    if project['mode'] == 'subtitles':
        return dict(stage='completed', action='deliver', artifacts=artifacts,
                    timing_source='subtitle_interpolation', speaker_identity_verified=False)
    write_json(work / 'output/quality.json', quality(analysis))
    if not quality(analysis)['dubbing_ready']:
        return create_review(work, analysis, video['path'])
    request_identity = digest(dict(server=client.server, media=video['sha256'],
                                   manifest=file_hash(artifacts['manifest']),
                                   ass=file_hash(artifacts['work_ass']), settings=project['settings']))
    if project.get('dubbing') and project['dubbing']['request_identity'] != request_identity:
        raise ValueError('Speech or settings changed after submission; use a new project to create another task')
    if not project.get('dubbing'):
        if file_hash(video['path']) != video['sha256']:
            raise ValueError('Video changed before submission')
        # Upload the same video once, and its generated bilingual ASS once.
        if project.get('ass_upload', {}).get('sha256') != file_hash(artifacts['work_ass']):
            project['ass_upload'] = dict(path=client.upload(artifacts['work_ass']),
                                         sha256=file_hash(artifacts['work_ass']))
            save(work, project)
        command = ['task', 'submit', '--type', 'video', '--server-path', project['video_server_path'],
                   '--subtitle-path', project['ass_upload']['path'], '--dub-manifest', artifacts['manifest'],
                   '--analysis-id', analysis['analysis_id'], '--subtitle-mode', 'bilingual',
                   '--subtitle-font-size', str(project['settings']['font_size'])]
        if project['settings']['keep_bgm']:
            command.append('--keep-bgm')
        submit(work, project, client, 'dubbing', command, request_identity)
    waiting = poll(client, 'dubbing', project['dubbing'])
    if waiting:
        return waiting
    result = client.call('task', 'result', project['dubbing']['task_id'])
    layout = (result.get('video_result') or {}).get('subtitle_layout')
    if (not layout or layout.get('status') != 'PASS' or layout.get('max_observed_lines', 999) > 4
            or layout.get('max_estimated_height_ratio', 1) > .25001):
        raise ValueError('Server did not return a passing final subtitle layout report')
    write_json(work / 'output/subtitle_layout.json', layout)
    downloads = project.setdefault('downloads', {})
    for kind, filename in [('download', 'dubbed.mp4'), ('download-subtitles', 'dubbed.ass'), ('audio', 'dubbed.mp3')]:
        target = work / 'output' / filename
        if not target.is_file() or downloads.get(filename) != file_hash(target):
            client.download(kind, project['dubbing']['task_id'], target)
            downloads[filename] = file_hash(target)
            save(work, project)
        artifacts[filename] = str(target)
    return dict(stage='completed', action='deliver', task_id=project['dubbing']['task_id'],
                artifacts=artifacts, quality=quality(analysis), subtitle_layout=layout)


def operate(work, project, args):
    if project['mode'] != 'dubbing':
        raise ValueError('This operation requires a dubbing project')
    client = Client(work, project.get('server'))
    if client.server != project.get('server'):
        raise ValueError('Server identity changed')
    if args.command == 'confirm-speakers':
        decisions = read_json(Path(args.decisions).expanduser().resolve())
        if not isinstance(decisions, list) or not decisions:
            raise ValueError('A nonempty list of explicit user listening decisions is required')
        response = client.call('video', 'confirm-speakers', '--analysis-id', project['analysis']['task_id'],
                               '--resolutions', Path(args.decisions).expanduser().resolve())
        revised = response.get('analysis')
        validate_analysis(revised, project['video']['sha256'])
        if revised['analysis_id'] != project['analysis']['task_id']:
            raise ValueError('Confirmation returned a different analysis')
        write_json(work / 'analysis.json', revised)
        return dict(stage='confirmed', action='run_again', quality=quality(revised))
    if args.command == 'refresh-analysis':
        return dict(stage='analysis_refreshed', action='run_again', quality=quality(load_remote_analysis(work, project, client)))
    if args.command == 'retry':
        job = project.get(args.kind)
        if not job:
            raise ValueError('No saved task to retry')
        status = client.call('task', 'status', job['task_id'])
        if status.get('status') not in FAILED:
            raise ValueError('Only a failed or cancelled saved task may be retried')
        client.call('task', 'retry', job['task_id'])
        return dict(stage=f'{args.kind}_waiting', action='wait_then_run', task_id=job['task_id'])
    pending = project.get('pending_submission')
    if not pending:
        raise ValueError('No unacknowledged submission to recover')
    # The status endpoint deliberately omits title and source URL; result exposes
    # their full identity even while a task is still processing.
    task = client.call('task', 'result', args.task_id)
    def matches(candidate):
        return (candidate.get('title') == pending['title']
                and candidate.get('url') == 'file://'+project['video_server_path']
                and candidate.get('type') == ('analysis' if pending['kind'] == 'analysis' else 'video')
                and candidate.get('task_id') not in pending['existing_task_ids'])
    candidates = [item for item in client.call('task', 'list').get('tasks', []) if matches(item)]
    if not matches(task) or [t['task_id'] for t in candidates] != [args.task_id]:
        raise ValueError('No unique new task matches this video title, source and task type; inspect before recovering')
    project[pending['kind']] = dict(kind=pending['kind'], title=pending['title'], task_id=args.task_id,
                                    request_identity=pending['request_identity'])
    project.pop('pending_submission')
    return dict(stage='recovered', action='run_again', task_id=args.task_id)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    for name in ('run', 'status', 'setup', 'confirm-speakers', 'refresh-analysis', 'retry', 'recover'):
        command = commands.add_parser(name)
        command.add_argument('--workdir', required=True)
        if name == 'run':
            command.add_argument('--input')
            command.add_argument('--server')
            command.add_argument('--mode', choices=['dubbing', 'subtitles'])
            command.add_argument('--source-subtitles')
            command.add_argument('--quality', default=None)
            command.add_argument('--font-size', type=int)
            command.add_argument('--keep-bgm', action='store_true', default=None)
            command.add_argument('--width', type=int)
            command.add_argument('--height', type=int)
            command.add_argument('--chunk-words', type=int)
            cookies = command.add_mutually_exclusive_group()
            cookies.add_argument('--cookies-browser')
            cookies.add_argument('--cookies-file')
        elif name == 'confirm-speakers':
            command.add_argument('--decisions', required=True)
        elif name == 'retry':
            command.add_argument('--kind', choices=['analysis', 'dubbing'], required=True)
        elif name == 'recover':
            command.add_argument('--task-id', required=True)
    return result


def main():
    if sys.version_info < (3, 10):
        raise SystemExit('Python 3.10+ is required')
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    args = parser().parse_args()
    work = Path(args.workdir).expanduser().resolve()
    project = None
    try:
        with project_lock(work):
            if args.command == 'setup':
                result = setup(work, SKILL)
            elif args.command == 'status':
                project = read_json(work / 'project.json')
                if project is None:
                    raise ValueError('No project exists in this workdir')
                result = dict(project=project, network_accessed=False)
            else:
                if args.command == 'run':
                    if args.quality:
                        args.quality = args.quality.lower().removesuffix('p')
                        if args.quality != 'best' and (not args.quality.isdigit() or int(args.quality) < 1):
                            raise ValueError('--quality must be best or a positive height')
                    if args.font_size is not None and args.font_size <= 0:
                        raise ValueError('--font-size must be positive')
                    if bool(args.width) != bool(args.height) or any(v is not None and v <= 0 for v in (args.width, args.height)):
                        raise ValueError('Supply both positive --width and --height')
                    if args.chunk_words is not None and not 50 <= args.chunk_words <= 3000:
                        raise ValueError('--chunk-words must be between 50 and 3000')
                    project = initialize(work, args)
                    result = run_project(work, project, args)
                else:
                    project = read_json(work / 'project.json')
                    if project is None:
                        raise ValueError('No saved project')
                    result = operate(work, project, args)
                save(work, project, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(stage='error', message=str(exc)), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
