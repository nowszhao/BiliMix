#!/usr/bin/env python3
"""Audit a delivered dubbing video: stream anchors, subtitle range and coverage.

This does not need libass. It decodes the final audio and checks that every ASS
page interval actually contains voiced audio, which catches the two failures the
renderer cares about: a page that drifts away from the speech, or a container
whose tracks no longer share one timeline.

Usage:
    python scripts/check_video_sync.py --video dubbed.mp4 --ass dubbed.ass
"""
import argparse
import json
import os
import subprocess
import tempfile
import wave


def probe(path):
    raw = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,start_time,duration,nb_frames",
        "-of", "json", path], text=True, timeout=60)
    streams = json.loads(raw).get("streams", [])
    result = {}
    for stream in streams:
        kind = stream.get("codec_type")
        if kind in ("video", "audio") and kind not in result:
            result[kind] = stream
    return result


def ass_seconds(value):
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def ass_events(path):
    events = []
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            start, end = ass_seconds(parts[1]), ass_seconds(parts[2])
            if end > start:
                events.append((start, end))
    return events


def envelope(path, rate=8000):
    """10ms RMS envelope of the final mixed audio."""
    import numpy as np
    descriptor, wav = tempfile.mkstemp(suffix=".wav")
    os.close(descriptor)
    try:
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-i", path,
            "-vn", "-ac", "1", "-ar", str(rate), wav], check=True)
        with wave.open(wav, "rb") as stream:
            samples = stream.readframes(stream.getnframes())
            rate = stream.getframerate()
    finally:
        os.unlink(wav)
    values = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768
    window = max(1, int(rate * .01))
    usable = (len(values) // window) * window
    if not usable:
        return np.zeros(0), window / rate
    rms = np.sqrt(np.mean(values[:usable].reshape(-1, window) ** 2, axis=1))
    return rms, window / rate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--ass", required=True)
    parser.add_argument("--offset-tolerance", type=float, default=.05)
    parser.add_argument("--drift-tolerance", type=float, default=.25)
    parser.add_argument("--silent-page-ratio", type=float, default=.10)
    args = parser.parse_args()

    errors = []
    streams = probe(args.video)
    video, audio = streams.get("video"), streams.get("audio")
    if not video or not audio:
        raise SystemExit("missing video or audio stream")
    for kind, stream in (("video", video), ("audio", audio)):
        start = float(stream.get("start_time") or 0)
        if abs(start) > args.offset_tolerance:
            errors.append(f"{kind} stream starts at {start:+.3f}s")
    video_duration = float(video.get("duration") or 0)
    audio_duration = float(audio.get("duration") or 0)
    drift = abs(video_duration - audio_duration)
    if drift > args.drift_tolerance:
        errors.append(f"stream duration drift {drift:.3f}s")

    events = ass_events(args.ass)
    if not events:
        errors.append("no ASS dialogue events")
    cursor = -1.0
    for start, end in events:
        if start < cursor - .001 or end > video_duration + .3:
            errors.append(f"ASS event outside the timeline: {start:.2f}-{end:.2f}")
            break
        cursor = end

    rms, step = envelope(args.video)
    threshold = max(1e-3, float(rms.max()) * .02) if len(rms) else 1.0
    voiced = (rms > threshold) if len(rms) else rms
    silent = []
    for start, end in events:
        first = max(0, int(start / step))
        last = min(len(rms), int(end / step))
        if last <= first or not voiced[first:last].any():
            silent.append((start, end, 0.0))
            continue
        ratio = float(voiced[first:last].mean())
        if ratio < .1:
            silent.append((start, end, ratio))
    if len(voiced) and events:
        onset = int(voiced.argmax()) * step
        offset = onset - events[0][0]
        if abs(offset) > 1.0:
            errors.append(f"first subtitle starts {offset:+.2f}s from the first speech")

    print(f"视频 {video_duration:.3f}s / 音频 {audio_duration:.3f}s，"
          f"漂移 {drift*1000:.0f}ms，字幕页 {len(events)}，"
          f"无语音页 {len(silent)}")
    if errors:
        for error in errors:
            print(f"错误: {error}")
        return 1
    if events and len(silent) / len(events) > args.silent_page_ratio:
        print("警告: 较多字幕页区间内没有检测到语音，请检查字幕时间轴")
        return 1
    print("同步检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
