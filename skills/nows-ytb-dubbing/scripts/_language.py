"""Two Agent passes over stable source IDs, with resumable prefix consumption."""
from __future__ import annotations

import re

from _project import digest, read_json, write_json


def tokens(text):
    normalized = text.replace('’', "'").replace('‘', "'").lower()
    return re.findall(r"[^\W_]+(?:'[^\W_]+)*", normalized, flags=re.UNICODE)


def valid_ids(ids, catalog):
    if (not isinstance(ids, list) or not ids or any(type(i) is not int or i not in catalog for i in ids)
            or ids != list(range(ids[0], ids[-1]+1))):
        raise ValueError('Expected a nonempty contiguous source word ID range')


def owner(ids, catalog, mode):
    valid_ids(ids, catalog)
    speakers = {catalog[i].get('speaker') for i in ids}
    if mode == 'dubbing' and len(speakers) != 1:
        raise ValueError('Speech crosses a real speaker change or an unresolved region; split it')
    return next(iter(speakers)) if len(speakers) == 1 else None


def apply_full(request, response, analysis, mode):
    if not isinstance(response, dict) or response.get('input_id') != request['input_id']:
        raise ValueError('FULL output input_id differs from the current input')
    sentences = response.get('sentences')
    if not isinstance(sentences, list) or not sentences:
        raise ValueError('FULL output requires nonempty sentences')
    words = request['words']
    catalog = {w['word_id']: w for w in analysis['words']}
    expected, positions = [], []
    for index, word in enumerate(words):
        values = tokens(word['text'])
        if not values:
            raise ValueError(f"Nonlexical source word {word['word_id']}: inspect the source analysis")
        expected.extend(values)
        positions.extend([index] * len(values))
    cursor, output = 0, []
    for sentence in sentences:
        text = sentence.get('text', '') if isinstance(sentence, dict) else ''
        values = tokens(text)
        if not values or values != expected[cursor:cursor+len(values)]:
            raise ValueError('FULL must preserve the exact ordered source word stream')
        end = cursor + len(values)
        if (cursor and positions[cursor] == positions[cursor-1]) or (
                end < len(positions) and positions[end-1] == positions[end]):
            raise ValueError('A source word was split across FULL sentences')
        ids = [w['word_id'] for w in words[positions[cursor]:positions[end-1]+1]]
        owner(ids, catalog, mode)
        output.append({'source_word_ids': ids, 'text': text.strip()})
        cursor = end
    # Consuming a prefix lets the unfinished tail become writable in the next
    # window. Neither a chunk size nor a look-ahead edge becomes a TTS boundary.
    return output


def apply_translation(request, response, analysis, mode):
    if not isinstance(response, dict) or response.get('input_id') != request['input_id']:
        raise ValueError('Translation output input_id differs from the current input')
    units = response.get('units')
    if not isinstance(units, list) or not units:
        raise ValueError('Translation requires nonempty complete Chinese units')
    sources = {s['unit_id']: s for s in request['source_units']}
    expected = list(sources)
    catalog = {w['word_id']: w for w in analysis['words']}
    consumed, output = [], []
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError('Each translated unit must be an object')
        ids = unit.get('source_unit_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in sources for i in ids):
            raise ValueError('Unknown source_unit_ids')
        words = [i for uid in ids for i in sources[uid]['source_word_ids']]
        owner(words, catalog, mode)
        english, chinese = unit.get('text_en'), unit.get('text_zh')
        if not all(isinstance(t, str) and t.strip() for t in (english, chinese)):
            raise ValueError('Complete English and Chinese text required; never invent filler')
        if len(chinese) > 1500:
            raise ValueError('Chinese exceeds the TTS limit; refine at a natural clause boundary')
        if any('||' in t or '\t' in t or '\n' in t or '\r' in t for t in (english, chinese)):
            raise ValueError('Translation text contains an output delimiter or line break')
        boundary = unit.get('boundary_after', 'sentence_end')
        if boundary not in ('continuation', 'sentence_end', 'turn_end'):
            raise ValueError('Invalid boundary_after')
        captions = unit.get('captions') or []
        if captions and (not isinstance(captions, list)
                         or any(not isinstance(c, dict) or not isinstance(c.get('text_zh'), str)
                                or not isinstance(c.get('text_en', ''), str) for c in captions)
                         or ''.join(c['text_zh'] for c in captions) != chinese):
            raise ValueError('Optional captions must exactly cover Chinese text')
        output.append(dict(source_unit_ids=ids, source_word_ids=words,
                           text_en=english.strip(), text_zh=chinese.strip(),
                           boundary_after=boundary, captions=captions))
        consumed.extend(ids)
    if consumed != expected[:len(consumed)]:
        raise ValueError('Translation must consume a contiguous prefix, once, in source order')
    return output


def advance_language(work, analysis, mode, context, chunk_words=650):
    """Return an explicit Agent action, or None when both passes are complete."""
    folder = work / 'language'
    refined_path, units_path = folder / 'refined.json', folder / 'units.json'
    refined = read_json(refined_path, [])
    translated = read_json(units_path, [])
    catalog = {w['word_id']: w for w in analysis['words']}
    all_ids = list(catalog)
    full_ids = [i for unit in refined for i in unit['source_word_ids']]
    if full_ids != all_ids[:len(full_ids)]:
        raise ValueError('Refined source coverage changed; repair language/refined.json')
    for unit in refined:
        owner(unit['source_word_ids'], catalog, mode)
        source_text = ' '.join(catalog[i]['text'] for i in unit['source_word_ids'])
        if tokens(unit['text']) != tokens(source_text):
            raise ValueError('Existing FULL text differs from its source words')
    if [unit['unit_id'] for unit in refined] != [f's_{i+1:06d}' for i in range(len(refined))]:
        raise ValueError('FULL unit IDs must be unique and ordered')
    if len(full_ids) < len(all_ids):
        cursor = len(full_ids)
        words = analysis['words'][cursor:cursor+chunk_words+100]
        request = dict(stage='full', source_media_sha256=analysis['source_media_sha256'],
                       context=context, previous_sentences=refined[-2:],
                       target_last_word_id=words[min(chunk_words, len(words))-1]['word_id'],
                       source_end=cursor+len(words)==len(all_ids), words=words)
        stage, instructions = 'full', 'references/language.md#full'
    else:
        consumed = [uid for unit in translated for uid in unit['source_unit_ids']]
        expected = [unit['unit_id'] for unit in refined]
        if consumed != expected[:len(consumed)]:
            raise ValueError('Translated source coverage changed; repair language/units.json')
        lookup = {source['unit_id']: source for source in refined}
        for unit in translated:
            owner(unit['source_word_ids'], catalog, mode)
            if unit['source_word_ids'] != [i for uid in unit['source_unit_ids']
                                           for i in lookup[uid]['source_word_ids']]:
                raise ValueError('Translated word mapping differs from its FULL source')
        if len(consumed) == len(expected):
            return None
        cursor, batch, size = len(consumed), [], 0
        for unit in refined[cursor:]:
            batch.append({**unit, 'speaker': owner(unit['source_word_ids'], catalog, mode)})
            size += len(unit['source_word_ids'])
            if size >= chunk_words:
                break
        extra = refined[cursor+len(batch):cursor+len(batch)+2]
        batch.extend({**u, 'speaker': owner(u['source_word_ids'], catalog, mode)} for u in extra)
        request = dict(stage='translate', source_media_sha256=analysis['source_media_sha256'],
                       context=context, previous_units=translated[-2:],
                       target_last_unit_id=batch[-len(extra)-1]['unit_id'] if extra else batch[-1]['unit_id'],
                       source_end=cursor+len(batch)==len(refined), source_units=batch)
        stage, instructions = 'translate', 'references/language.md#translate'
    request['input_id'] = digest(request)
    stage_dir = folder / stage
    input_path, output_path = stage_dir / 'input.json', stage_dir / 'output.json'
    old_input = read_json(input_path)
    response = read_json(output_path)
    if old_input != request:
        output_path.unlink(missing_ok=True)
        response = None
    write_json(input_path, request)
    if response is not None:
        if stage == 'full':
            additions = apply_full(request, response, analysis, mode)
            for unit in additions:
                unit['unit_id'] = f's_{len(refined)+1:06d}'
                refined.append(unit)
            write_json(refined_path, refined)
        else:
            additions = apply_translation(request, response, analysis, mode)
            for unit in additions:
                unit['unit_id'] = f'u_{len(translated)+1:06d}'
                translated.append(unit)
            write_json(units_path, translated)
        output_path.unlink(missing_ok=True)
        return {'stage': 'advance', 'action': 'run_again'}
    return dict(stage=stage, action='agent_write', input=str(input_path), output=str(output_path),
                instructions=instructions, input_id=request['input_id'])
