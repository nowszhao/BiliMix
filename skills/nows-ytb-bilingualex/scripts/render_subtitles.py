#!/usr/bin/env python3
"""Create bounded viewing subtitles from the refined EN || ZH working ASS.

The original working ASS stays unchanged for BiliMix import. This standalone
mode uses source timing; the server generates dubbing-time subtitles separately.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys
from subtitle_layout import SubtitleLayout,ass_document


def seconds(value):
    h,m,s = value.split(':')
    return int(h)*3600+int(m)*60+float(s)


def read_entries(path):
    result = []
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not line.startswith('Dialogue:'):
            continue
        fields = line.split(',',9)
        text = re.sub(r'\{[^}]*\}','',fields[9]).replace('\\N',' ')
        if ' || ' not in text:
            raise ValueError('Expected a refined EN || ZH working ASS')
        english,chinese = text.split(' || ',1)
        start,end = seconds(fields[1]),seconds(fields[2])
        if result and start < result[-1]['end']-.001:
            raise ValueError('Overlapping source captions would stack; finish FULL/timing cleanup first')
        result.append({'start':start,'end':end,'speaker':fields[4],'english':english,'chinese':chinese})
    if not result:
        raise ValueError('No bilingual dialogue found')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--width',type=int,required=True)
    parser.add_argument('--height',type=int,required=True)
    parser.add_argument('--font-size',type=int,default=40)
    args = parser.parse_args()
    try:
        output = Path(args.output).expanduser().resolve()
        if output == Path(args.input).expanduser().resolve():
            raise ValueError('Keep the working ASS: choose another output path')
        layout = SubtitleLayout(args.width,args.height,args.font_size)
        document,report = ass_document(read_entries(args.input),layout,margin_v=max(10,round(args.height*.03)))
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(document,encoding='utf-8')
        Path(str(output)+'.layout.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'path':str(output),'display_pages':report['display_pages'],
                         'max_lines':report['max_observed_lines'],
                         'max_estimated_height_ratio':report['max_estimated_height_ratio']},ensure_ascii=False));return 0
    except (OSError,ValueError,IndexError) as error:
        print(str(error),file=sys.stderr);return 1


if __name__ == '__main__':
    from portable_io import configure_utf8
    configure_utf8()
    raise SystemExit(main())
