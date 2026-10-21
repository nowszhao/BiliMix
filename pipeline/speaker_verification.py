"""Apply acoustic verification without using transcript meaning to guess a voice."""
import re


POLICY_VERSION = 3


def context_bounds(words, ids, padding=.15):
    """Recover clipped phoneme edges, without entering a neighboring word."""
    first, last = words[ids[0]], words[ids[-1]]
    left = min(padding, max(0, (first['start']-words[ids[0]-1]['end'])/2)) if ids[0] else min(padding,first['start'])
    right = min(padding, max(0, (words[ids[-1]+1]['start']-last['end'])/2)) if ids[-1]+1<len(words) else padding
    return first['start']-left, last['end']+right


def utterance_spans(transcription):
    """Verification windows only; FULL/translation still choose actual TTS units."""
    output, ids, offset = [], [], 0
    for segment in transcription.get('segments',[]):
        for word in segment.get('words',[]):
            ids.append(offset);offset+=1
            if re.search(r'[.!?][\"\')\]]?$',word.get('word','')):
                output.append(ids);ids=[]
        if ids:
            output.append(ids);ids=[]
    return output


def confident(scores, duration):
    if duration<.35 or not scores:return None
    if len(scores)==1:return scores[0][0] if scores[0][1]>=.35 else None
    first,second=scores[:2]
    minimum=.25 if duration<.7 else .28
    margin=.15 if duration<.7 else .08
    return first[0] if first[1]>=minimum and first[1]-second[1]>=margin else None


def _local_winner(evidence, duration):
    winner = confident(evidence.get('scores', []), duration)
    if winner is not None:
        return winner, 'acoustic'
    context = evidence.get('context_scores', [])
    contextual = confident(context, evidence.get('context_duration', 0))
    direct = evidence.get('scores', [])
    if contextual is not None and direct and contextual == direct[0][0]:
        return contextual, 'acoustic_bounded_context'
    # A tiny word may be below the embedding's minimum input length. Accept
    # its own bounded context only with a stronger independent acoustic lead.
    if (contextual is not None and not direct and len(context) >= 2
            and context[0][1] >= .45 and context[0][1]-context[1][1] >= .20):
        return contextual, 'acoustic_bounded_context'
    return None, None


def verified_word_assignments(analysis, evidence):
    """Verify each voiced run; a parent winner never fills an unknown run."""
    from pipeline.dubbing_plan import known_speaker
    if evidence.get('missing_reference_speakers'):
        return {}
    catalog = {w['word_id']: w for w in analysis['words']}
    output = {}
    for unit in evidence['units']:
        ids = unit['word_ids']
        duration = catalog[ids[-1]]['end']-catalog[ids[0]]['start']
        winner, method = _local_winner(unit, duration)
        runs = unit.get('runs') or []
        if not runs:
            for i in ids:
                speaker = catalog[i]['speaker']
                if not runs or runs[-1]['speaker'] != speaker:
                    runs.append({'speaker': speaker, 'word_ids': []})
                runs[-1]['word_ids'].append(i)
        if len(runs) == 1 and 'scores' not in runs[0]:
            runs[0] = {**unit, **runs[0]}
        for run in runs:
            run_ids = run['word_ids']
            run_duration = catalog[run_ids[-1]]['end']-catalog[run_ids[0]]['start']
            local, local_method = _local_winner(run, run_duration)
            if local is not None:
                for i in run_ids:
                    output[i] = dict(speaker=local, speaker_assignment=local_method, speaker_verified=True)
            elif winner is not None:
                # A whole utterance can corroborate an existing owner. It
                # cannot overrule a different voice or invent a missing one.
                for i in run_ids:
                    if known_speaker(catalog[i]['speaker']) and catalog[i]['speaker'] == winner:
                        output[i] = dict(speaker=winner, speaker_assignment=method, speaker_verified=True)
    return output
