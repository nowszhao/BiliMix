"""Apply acoustic verification without using transcript meaning to guess a voice."""
import re


POLICY_VERSION = 2


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


def verified_word_assignments(analysis, evidence):
    """Confident independently voiced runs remain separate inside an utterance."""
    catalog={w['word_id']:w for w in analysis['words']}
    output={}
    for unit in evidence['units']:
        ids=unit['word_ids'];duration=catalog[ids[-1]]['end']-catalog[ids[0]]['start']
        winner=confident(unit['scores'],duration)
        method='acoustic'
        if winner is None and unit.get('context_scores') and unit['scores']:
            contextual=confident(unit['context_scores'],unit['context_duration'])
            if contextual==unit['scores'][0][0]:
                winner=contextual;method='acoustic_bounded_context'
        if winner is None:
            # Repair a single edge token only when native ownership and the
            # acoustic scores agree; never smooth an independent short answer.
            counts={}
            for i in ids:counts[catalog[i]['speaker']]=counts.get(catalog[i]['speaker'],0)+1
            dominant=max(counts,key=counts.get)
            minority=[i for i in ids if catalog[i]['speaker']!=dominant]
            score=dict(unit['scores']).get(dominant,-1)
            edge=minority and len(minority)==1 and minority[0] in (ids[0],ids[-1])
            if edge and len(ids)>=5 and counts[dominant]/len(ids)>=.8 and score>=.2 and unit['scores'] and unit['scores'][0][1]-score<=.1 and all(score-value>=.1 for speaker,value in unit['scores'] if speaker in counts and speaker!=dominant) and catalog[minority[0]]['end']-catalog[minority[0]]['start']<=.25:
                winner=dominant;method='native_and_acoustic_boundary'
        if winner is None:continue
        protected={}
        for run in unit.get('runs',[]):
            run_ids=run['word_ids'];rd=catalog[run_ids[-1]]['end']-catalog[run_ids[0]]['start']
            run_winner=confident(run['scores'],rd)
            if run_winner and run_winner!=winner:
                protected.update({i:run_winner for i in run_ids})
            elif run_winner is None and rd>=.6 and run_ids[0]!=ids[0] and run_ids[-1]!=ids[-1]:
                # Unknown internal speech can be a real interruption. Preserve
                # its native identity instead of overruling it by whole-span vote.
                protected.update({i:catalog[i]['speaker'] for i in run_ids})
        for i in ids:
            speaker=protected.get(i,winner)
            output[i]={'speaker':speaker,'speaker_assignment':method,
                       'speaker_verified':i not in protected or any(confident(r['scores'],catalog[r['word_ids'][-1]]['end']-catalog[r['word_ids'][0]]['start'])==speaker for r in unit.get('runs',[]) if i in r['word_ids'])}
    return output
