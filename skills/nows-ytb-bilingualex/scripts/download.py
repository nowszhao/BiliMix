#!/usr/bin/env python3
"""Download one MP4 and metadata. Browser/cookie use is optional and explicit.

Run check_env.py first; supply --env-out if its JSON is outside the current
working directory. This file does not translate or submit dubbing jobs.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def log(message):
    print('[download] '+message, file=sys.stderr, flush=True)


def run(command, timeout=1800):
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                          env={**os.environ,"PYTHONUTF8":"1"})


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip(' .')
    name = name.encode('utf-8')[:160].decode('utf-8', errors='ignore').rstrip(' .') or 'video'
    if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        name = '_'+name
    return name


def quality_format(quality):
    value = quality.lower().removesuffix('p')
    if value == 'best':
        return 'bv*+ba/b'
    if not value.isdigit() or int(value) <= 0:
        raise ValueError('Quality must be best or a positive height, e.g. 720p')
    return f'bv*[height<={value}]+ba/b[height<={value}]'


def fetch_info(yt, url, cookies_browser=None, cookies_file=None):
    command = [yt] if isinstance(yt,str) else list(yt)
    auth = ['--cookies-from-browser',cookies_browser] if cookies_browser else (['--cookies',cookies_file] if cookies_file else [])
    result = run([*command,'--dump-single-json','--no-playlist','--no-warnings',*auth,url])
    if result.returncode:
        return None, result.stderr.strip()[-600:]
    try:
        return json.loads(result.stdout), None
    except ValueError:
        return None, 'yt-dlp returned invalid metadata JSON'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--quality', default='720p')
    cookies = parser.add_mutually_exclusive_group()
    cookies.add_argument('--cookies-browser', help='yt-dlp browser[:profile] selector, optional')
    cookies.add_argument('--cookies-file', help='Netscape-format cookie file, optional')
    parser.add_argument('--output', default='.')
    parser.add_argument('--env-out', default='yt_env.json')
    args = parser.parse_args()
    try:
        env = json.loads(Path(args.env_out).read_text(encoding='utf-8'))
        yt = env.get('yt_dlp_command') or ([env['yt_dlp']] if env.get('yt_dlp') else None)
        ffmpeg = env.get('ffmpeg')
        if not yt or not ffmpeg:
            raise ValueError('Download tools missing; run check_env.py on this machine first')
        if not isinstance(yt,list) or not all(isinstance(v,str) for v in yt):
            raise ValueError('Invalid yt_dlp_command in environment JSON')
        work = Path(args.output).expanduser().resolve(); work.mkdir(parents=True, exist_ok=True)
        info, error = fetch_info(yt,args.url,args.cookies_browser,args.cookies_file)
        if info is None:
            raise ValueError(error)
        title = info.get('title') or 'video'
        base = sanitize(title)+'_'+sanitize(str(info.get('id') or 'video'))
        auth = ['--cookies-from-browser',args.cookies_browser] if args.cookies_browser else (['--cookies',args.cookies_file] if args.cookies_file else [])
        command = [*yt,'--no-playlist','--no-warnings','--no-progress',*auth,
            '--ffmpeg-location',ffmpeg,'-f',quality_format(args.quality),
            '--merge-output-format','mp4','--remux-video','mp4',
            '--print','after_move:filepath','-o',str(work/(base+'.%(ext)s')),args.url]
        result = run(command)
        if result.returncode:
            raise ValueError(result.stderr.strip()[-600:])
        paths = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        video = paths[-1].resolve() if paths else work/(base+'.mp4')
        if not video.is_file() or video.suffix.lower() != '.mp4' or video.parent != work:
            raise ValueError('yt-dlp did not report the requested MP4; no unrelated files will be reused')
        (work/'video_info.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        manifest = {'url':args.url,'title':title,'safe_title':base,
            'duration_sec':info.get('duration') or 0,'video_path':str(video),
            'metadata_path':'video_info.json','subtitle_path':None,'subtitle_kind':None,
            'subtitle_lang':None,'quality':args.quality,
            'downloaded_at':datetime.now(timezone.utc).isoformat()}
        (work/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(manifest,ensure_ascii=False)); return 0
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        log(str(error)); return 1


if __name__ == '__main__':
    from portable_io import configure_utf8
    configure_utf8()
    raise SystemExit(main())
