#!/usr/bin/env python3
"""Prepare translator input / apply a complete Chinese utterance grouping plan.

prepare --workdir .
apply --workdir . --plan dub_units_plan.json

Plan: {"units":[{"source_indices":[1,2],"text_zh":"complete Chinese",
                  "text_en":"corrected English", "captions":[...]}]}
Grouping and Chinese rewriting are translation decisions, never a heuristic.
"""
import argparse
import json
from pathlib import Path
from audio_first import load_analysis


def read_rows(work):
    meta = json.loads((work/'subtitle_meta.json').read_text())['events']
    rows = {}
    for file in sorted((work/'parts').glob('trans_*.txt')):
        for line in file.read_text().splitlines():
            if not line.strip(): continue
            fields = line.split('\t')
            if len(fields)!=5: raise ValueError(f'Bad translation row in {file}')
            idx = int(fields[0])
            if idx in rows: raise ValueError(f'Duplicate idx {idx}')
            source = meta[idx-1]
            if not source.get('source_word_ids') or not source.get('speaker_id'):
                raise ValueError('Refine using analysis.json before translation')
            rows[idx] = {'idx':idx,'text_en':fields[3],'text_zh':fields[4],**source}
            rows[idx]['text_en'],rows[idx]['text_zh'] = fields[3],fields[4]
    if sorted(rows)!=list(range(1,len(meta)+1)): raise ValueError('Missing translation rows')
    return dict(sorted(rows.items()))


def build(work, plan):
    analysis = load_analysis(work/'analysis.json')
    rows = read_rows(work)
    used, words_used, units = [], [], []
    catalog = {w['word_id']:w for w in analysis['words']}
    for number, item in enumerate(plan['units'],1):
        indices = item['source_indices']
        sources = [rows[i] for i in indices]
        speakers = {s['speaker_id'] for s in sources}
        if not sources or len(speakers)!=1: raise ValueError('Group cannot cross speakers')
        word_ids = [w for s in sources for w in s['source_word_ids']]
        text = item['text_zh'].strip()
        if not text: raise ValueError('Empty Chinese utterance')
        if item.get('captions') and ''.join(c['text_zh'] for c in item['captions'])!=text:
            raise ValueError('Display captions must cover complete Chinese text')
        units.append({'unit_id':f'u_{number:05d}','speaker_id':next(iter(speakers)),
                      'source_word_ids':word_ids,'text_zh':text,
                      'text_en':item.get('text_en',' '.join(s['text_en'] for s in sources)),
                      'captions':item.get('captions',[]),
                      'boundary_after':item.get('boundary_after','sentence_end')})
        used.extend(indices);words_used.extend(word_ids)
    if used!=list(rows): raise ValueError('Grouping skips/duplicates/reorders source rows')
    if words_used!=list(catalog): raise ValueError('Source word coverage/order changed')
    return {'schema_version':1,'analysis_id':analysis['analysis_id'],
            'source_media_sha256':analysis['source_media_sha256'],'units':units}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','apply'])
    parser.add_argument('--workdir',default='.')
    parser.add_argument('--plan',default='dub_units_plan.json')
    args=parser.parse_args();work=Path(args.workdir)
    if args.command=='prepare':
        data={'rows':list(read_rows(work).values()),'instructions':
              'Create complete spoken Chinese units; same-speaker adjacent rows may merge. '
              'Preserve all content and dialogue order. Each source index exactly once. '
              'Captions are display-only bilingual pages, separate from whole TTS utterances. '
              'Prefer short semantic captions; the renderer enforces line/height budgets. '
              'Output units with source_indices, text_zh, text_en and optional captions.'}
        destination=work/'dub_unit_input.json'
    else:
        plan_path=Path(args.plan)
        if not plan_path.is_absolute(): plan_path=work/plan_path
        data=build(work,json.loads(plan_path.read_text()));destination=work/'dub_manifest.json'
    destination.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(destination)


if __name__=='__main__': main()
