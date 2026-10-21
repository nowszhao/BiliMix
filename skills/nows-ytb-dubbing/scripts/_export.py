"""Generate work ASS and manifest from one translated speech representation."""
from __future__ import annotations

import math

from _language import owner
from _project import read_json, write_json
from subtitle_layout import SubtitleLayout, ass_document


def timestamp(value):
    centiseconds = round(value * 100)
    total, fraction = divmod(centiseconds, 100)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}'


def literal(text):
    return text.replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\n', ' ').replace('\r', ' ')


def validate_analysis(analysis, media_hash):
    if (not isinstance(analysis, dict) or analysis.get('schema_version') != 1
            or not analysis.get('analysis_id') or analysis.get('source_media_sha256') != media_hash):
        raise ValueError('Analysis identity or media hash mismatch')
    words = analysis.get('words')
    if not isinstance(words, list) or not words:
        raise ValueError('Source word analysis is empty')
    for index, word in enumerate(words):
        times = [word.get('start'), word.get('end')]
        if (type(word.get('word_id')) is not int or word['word_id'] != index
                or not isinstance(word.get('text'), str) or not word['text'].strip()
                or not all(type(t) in (int, float) and math.isfinite(t) for t in times)
                or times[0] < 0 or times[1] <= times[0]
                or (index and word['start'] < words[index-1]['start'])):
            raise ValueError('Invalid/unordered source word analysis')


def quality(analysis):
    unresolved = analysis.get('unresolved_segments', [])
    unknown = [w['word_id'] for w in analysis['words'] if not w.get('speaker')]
    missing = analysis.get('missing_reference_speakers', [])
    expected = {w['speaker'] for w in analysis['words'] if w.get('speaker')}
    missing = sorted(set(missing) | (expected - analysis.get('references', {}).keys()))
    uncertain = []
    for word in analysis['words']:
        if not word.get('speaker') or word.get('speaker_verified') or word.get('human_confirmed'):
            continue
        if (not uncertain or uncertain[-1]['speaker'] != word['speaker']
                or word['word_id'] != uncertain[-1]['word_ids'][-1]+1
                or word['start']-uncertain[-1]['end'] > .8):
            uncertain.append(dict(speaker=word['speaker'], start=word['start'], end=word['end'],
                                  word_ids=[], text=''))
        item = uncertain[-1]
        item['end'] = max(item['end'], word['end'])
        item['word_ids'].append(word['word_id'])
        item['text'] = (item['text']+' '+word['text']).strip()
    return dict(dubbing_ready=bool(analysis.get('dubbing_ready')) and not unknown and not missing,
                unresolved_segments=unresolved, unresolved_word_ids=unknown,
                missing_reference_speakers=missing,
                acoustically_verified_words=sum(bool(w.get('speaker_verified')) for w in analysis['words']),
                human_confirmed_words=sum(bool(w.get('human_confirmed')) for w in analysis['words']),
                unverified_assigned_segments=uncertain)


def export_work(work, analysis, mode, dimensions, font_size):
    units = read_json(work / 'language/units.json', [])
    catalog = {w['word_id']: w for w in analysis['words']}
    if [i for unit in units for i in unit['source_word_ids']] != list(catalog):
        raise ValueError('Translated words must cover the source exactly once, in order')
    header = ('[Script Info]\nScriptType: v4.00+\n'
              f'PlayResX: {dimensions[0]}\nPlayResY: {dimensions[1]}\n'
              '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
              'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, '
              'Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
              f'Style: Default,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,'
              '0,0,0,0,100,100,0,0,1,2,0,2,30,30,25,1\n'
              '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
    lines, manifest_units, entries = [], [], []
    for number, unit in enumerate(units, 1):
        ids = unit['source_word_ids']
        speaker = owner(ids, catalog, mode)
        start, end = catalog[ids[0]]['start'], catalog[ids[-1]]['end']
        if timestamp(start) == timestamp(end):
            raise ValueError('Speech is shorter than ASS precision; inspect alignment')
        name = (speaker or '').replace(',', '_').replace('\r', ' ').replace('\n', ' ')
        english, chinese = unit['text_en'], unit['text_zh']
        if not english.strip() or not chinese.strip() or '||' in english or '||' in chinese:
            raise ValueError('Invalid bilingual speech text')
        if len(chinese) > 1500:
            raise ValueError('Chinese speech exceeds the engine request limit')
        captions = unit.get('captions') or []
        if captions and ''.join(c['text_zh'] for c in captions) != chinese:
            raise ValueError('Caption text differs from complete Chinese speech')
        lines.append(f'Dialogue: 0,{timestamp(start)},{timestamp(end)},Default,{name},0,0,0,,{literal(english)} || {literal(chinese)}')
        manifest_units.append(dict(unit_id=f'u_{number:06d}', speaker_id=speaker,
                                   source_word_ids=ids, text_en=english, text_zh=chinese,
                                   boundary_after=unit.get('boundary_after', 'sentence_end'),
                                   captions=unit.get('captions', [])))
        entries.append(dict(start=start, end=end, speaker=speaker or '', english=english, chinese=chinese))
    output = work / 'output'
    output.mkdir(parents=True, exist_ok=True)
    ass = output / 'refined.ass'
    ass.write_text(header+'\n'.join(lines)+'\n', encoding='utf-8')
    if mode == 'subtitles':
        for left, right in zip(entries, entries[1:]):
            if left['end'] > right['start'] + .001:
                raise ValueError('Viewing subtitles would overlap; repair source timing')
        document, report = ass_document(entries, SubtitleLayout(*dimensions, font_size))
        viewing = output / 'bilingual.ass'
        viewing.write_text(document, encoding='utf-8')
        write_json(output / 'bilingual.ass.layout.json', report)
        return dict(work_ass=str(ass), subtitles=str(viewing), subtitle_layout=report)
    if not quality(analysis)['dubbing_ready']:
        (output / 'dub_manifest.json').unlink(missing_ok=True)
        return {'work_ass': str(ass)}
    manifest = dict(schema_version=1, analysis_id=analysis['analysis_id'],
                    source_media_sha256=analysis['source_media_sha256'], units=manifest_units)
    path = output / 'dub_manifest.json'
    write_json(path, manifest)
    return {'work_ass': str(ass), 'manifest': str(path)}
