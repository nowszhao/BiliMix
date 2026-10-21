"""Media preparation and subtitle-only import. Dependencies stay in the project."""
from __future__ import annotations

import html
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from _bmx import tools_python
from _language import tokens
from _project import digest, file_hash, write_json


def execute(command, timeout=7200):
    result = subprocess.run(list(map(str, command)), capture_output=True, text=True,
                            encoding='utf-8', timeout=timeout,
                            env={**os.environ, 'PYTHONUTF8': '1'})
    if result.returncode:
        raise ValueError((result.stderr or result.stdout)[-1600:] or 'Media command failed')
    return result.stdout


def setup(work, skill):
    python = tools_python(work)
    if not python.is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(python.parent.parent)], check=True)
    client = json.loads((skill / 'assets/client.json').read_text(encoding='utf-8'))
    wheel = skill / 'assets' / client['wheel']
    if file_hash(wheel) != client['sha256']:
        raise ValueError('Bundled SDK checksum mismatch')
    # pip output is diagnostic; workflow stdout remains a single JSON result.
    subprocess.run([str(python), '-m', 'pip', 'install', str(wheel), 'yt-dlp', 'imageio-ffmpeg'],
                   check=True, stdout=sys.stderr, stderr=sys.stderr)
    return {'stage': 'setup', 'python': str(python), 'sdk_version': client['version']}


def download_tools(work):
    isolated = tools_python(work)
    python = str(isolated) if isolated.is_file() else sys.executable
    explicit = os.environ.get('BILIMIX_YTDLP')
    if explicit:
        yt = [explicit]
    elif isolated.is_file():
        yt = [python, '-m', 'yt_dlp']
    elif importlib.util.find_spec('yt_dlp'):
        yt = [python, '-m', 'yt_dlp']
    elif shutil.which('yt-dlp'):
        yt = [shutil.which('yt-dlp')]
    else:
        raise ValueError('yt-dlp missing: run workflow.py setup --workdir <project>')
    ffmpeg = os.environ.get('BILIMIX_FFMPEG') or shutil.which('ffmpeg')
    if not ffmpeg:
        ffmpeg = execute([python, '-c', 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], 60).strip()
    if not Path(ffmpeg).is_file():
        raise ValueError('FFmpeg missing: run workflow.py setup --workdir <project>')
    return yt, ffmpeg


def cookie_arguments(settings):
    if settings.get('cookies_browser'):
        return ['--cookies-from-browser', settings['cookies_browser']]
    if settings.get('cookies_file'):
        return ['--cookies', settings['cookies_file']]
    return []


def prepare_video(work, project):
    value = project['input']
    if not value.startswith(('https://', 'http://')):
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError('Source video does not exist: ' + str(path))
        return {'path': str(path), 'sha256': file_hash(path)}, {'title': path.stem}
    yt, ffmpeg = download_tools(work)
    settings = project['settings']
    auth = cookie_arguments(settings)
    info = json.loads(execute([*yt, '--dump-single-json', '--no-playlist', '--no-warnings', *auth, value]))
    quality = settings['quality']
    fmt = 'bv*+ba/b' if quality == 'best' else f'bv*[height<={quality}]+ba/b[height<={quality}]'
    folder = work / 'source'
    folder.mkdir(parents=True, exist_ok=True)
    output = execute([*yt, '--no-playlist', '--no-warnings', '--no-progress', *auth,
                      '--ffmpeg-location', ffmpeg, '-f', fmt, '--merge-output-format', 'mp4',
                      '--remux-video', 'mp4', '--print', 'after_move:filepath',
                      '-o', str(folder / 'video.%(ext)s'), value])
    paths = [Path(line.strip()).resolve() for line in output.splitlines() if line.strip()]
    path = paths[-1] if paths else folder / 'video.mp4'
    if not path.is_file() or path.parent != folder or path.suffix.lower() != '.mp4':
        raise ValueError('Downloader did not return the requested MP4')
    write_json(folder / 'metadata.json', info)
    return {'path': str(path), 'sha256': file_hash(path)}, info


def source_subtitles(work, project):
    if project.get('source_subtitles'):
        return Path(project['source_subtitles'])
    if not project['input'].startswith(('https://', 'http://')):
        raise ValueError('Subtitle-only local video needs --source-subtitles <English.srt>')
    yt, ffmpeg = download_tools(work)
    folder = work / 'source'
    execute([*yt, '--no-playlist', '--skip-download', '--write-subs', '--write-auto-subs',
             '--sub-langs', 'en,en-orig', '--sub-format', 'srt/vtt', '--convert-subs', 'srt',
             '--ffmpeg-location', ffmpeg, *cookie_arguments(project['settings']),
             '-o', str(folder / 'captions.%(ext)s'), project['input']])
    files = sorted(folder.glob('captions.en*.srt'))
    if not files:
        raise ValueError('No English subtitles found; use dubbing analysis or provide --source-subtitles')
    return files[0]


def subtitle_analysis(path, media_hash):
    """Subtitle timing is approximate and is never eligible for voice cloning."""
    raw = Path(path).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
    words, previous_text = [], []
    def seconds(text):
        h, m, s = text.replace(',', '.').split(':')
        return int(h)*3600 + int(m)*60 + float(s)
    pattern = re.compile(r'(\d+:\d{2}:\d{2}[,.]\d+)\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d+)')
    for block in re.split(r'\n\s*\n', raw.strip()):
        lines = block.splitlines()
        index = next((i for i, line in enumerate(lines) if pattern.search(line)), None)
        if index is None:
            continue
        match = pattern.search(lines[index])
        start, end = map(seconds, match.groups())
        text = html.unescape(re.sub(r'<[^>]+>', '', ' '.join(lines[index+1:])))
        parts = text.split()
        if not parts or end <= start:
            continue
        # Only remove a repeated rolling-caption prefix when time overlaps.
        if words and start < words[-1]['end']:
            overlap = 0
            for count in range(1, min(len(previous_text), len(parts))+1):
                if tokens(' '.join(previous_text[-count:])) == tokens(' '.join(parts[:count])):
                    overlap = count
            if not overlap:
                raise ValueError('Overlapping independent source subtitles: resolve timing or use server word alignment')
            parts = parts[overlap:]
            start = words[-1]['end']
            if not parts:
                continue
            if end <= start:
                raise ValueError('Rolling subtitles contain new words without a visible interval')
        for number, part in enumerate(parts):
            words.append(dict(word_id=len(words), text=part, start=start+(end-start)*number/len(parts),
                              end=start+(end-start)*(number+1)/len(parts), speaker=None,
                              speaker_verified=False, speaker_candidates=[]))
        previous_text.extend(parts)
    if not words:
        raise ValueError('No timed English speech found in the SRT')
    return dict(schema_version=1, analysis_id='subtitles-'+digest(words)[:24],
                source_media_sha256=media_hash, timing_source='subtitle_interpolation',
                words=words, references={}, dubbing_ready=False)


def video_size(video, work, metadata=None):
    if metadata and metadata.get('width') and metadata.get('height'):
        return int(metadata['width']), int(metadata['height'])
    probe = shutil.which('ffprobe')
    if probe:
        info = json.loads(execute([probe, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                   'stream=width,height', '-of', 'json', video], 60))
        if info.get('streams'):
            return int(info['streams'][0]['width']), int(info['streams'][0]['height'])
    ffmpeg = os.environ.get('BILIMIX_FFMPEG') or shutil.which('ffmpeg')
    if not ffmpeg:
        _, ffmpeg = download_tools(work)
    result = subprocess.run([ffmpeg, '-hide_banner', '-i', str(video)], capture_output=True,
                            text=True, encoding='utf-8', timeout=60)
    for line in result.stderr.splitlines():
        match = re.search(r'\b(\d{2,5})x(\d{2,5})\b', line) if 'Video:' in line else None
        if match:
            return int(match[1]), int(match[2])
    raise ValueError('Cannot read video resolution; supply --width and --height')
