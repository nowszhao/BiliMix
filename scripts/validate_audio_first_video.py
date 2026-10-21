#!/usr/bin/env python3
"""Real FFmpeg regression: 20 speech clips, 19 new pauses, four video blocks.

No speech model needed. Verify frame counts and continuous master-audio duration.
Run from project root: python scripts/validate_audio_first_video.py --workdir /tmp/video-check
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydub import AudioSegment
from core import config
from pipeline.step5_video_assemble import generate_bilingual_ass, assemble_video


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True)
    args = parser.parse_args()
    root = Path(args.workdir)
    root.mkdir(parents=True, exist_ok=True)
    config.WATERMARK_ENABLED = False
    source = str(root/'source.mp4')
    subprocess.run(['ffmpeg','-y','-loglevel','error','-f','lavfi','-i',
                    'testsrc2=size=320x180:rate=25','-t','20','-c:v','libx264',
                    '-preset','ultrafast',source],check=True)
    entries = [dict(type='tts_chinese',segment_index=i,orig_start=i,orig_end=i+1,
                    mixed_start=i*.88,mixed_end=i*.88+.73,chinese='完整的一句话。') for i in range(20)]
    segments = [dict(start=i,end=i+1,text='A complete sentence.') for i in range(20)]
    translations = {i:'完整的一句话。' for i in range(20)}
    audio = str(root/'audio.wav')
    AudioSegment.silent(duration=17450).export(audio,format='wav')
    ass = generate_bilingual_ass(segments,translations,entries,str(root/'captions.ass'),
                                 video_height=180,video_width=320)
    results = {}
    for name, limit in [('regular',200),('blocks',5)]:
        config.VIDEO_MAX_CONCAT_SEGMENTS = limit
        config.VIDEO_BLOCK_SIZE = 5
        output = str(root/(name+'.mp4'))
        assemble_video(source,audio,ass,output,time_mapping=entries,segments=segments,
                       translations=translations,video_width=320,video_height=180)
        value = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries',
            'stream=codec_type,duration,nb_frames','-of','json',output],text=True))
        streams = {s['codec_type']:s for s in value['streams']}
        assert int(streams['video']['nb_frames']) == round(17.45*25),value
        assert abs(float(streams['video']['duration'])-float(streams['audio']['duration'])) <= 1/25,value
        assert abs(float(streams['audio']['duration'])-17.45) < .005,value
        results[name] = value
    (root/'validation.json').write_text(json.dumps(results,indent=2))
    print('REAL_VIDEO_TIMELINE_PASS',json.dumps(results))


if __name__ == '__main__': main()
