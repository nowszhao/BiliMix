import copy
import json
import os
from pathlib import Path
import re

import pytest
from pydub import AudioSegment
from core import config
from pipeline.dubbing_plan import validate_manifest
from pipeline.speaker_analysis import build_analysis
from pipeline import ref_audio_utils as refs
from pipeline.step4b_sentence_mixer import mix_sentence_audio
from pipeline import step5_video_assemble as video
from pipeline.tts_cache import synthesis_key


def analysis_fixture():
    segments = []
    for speaker, start, text in [('A', 0, 'the reason we changed was reliability'),
                                 ('B', 4, 'yes I agree that is important'),
                                 ('A', 8, 'we should keep the whole sentence')]:
        segments.append({'speaker': speaker, 'words': [
            {'word': word, 'speaker': speaker, 'start': start+i*.5, 'end': start+(i+1)*.5}
            for i, word in enumerate(text.split())]})
    result = build_analysis({'segments': segments}, 'mediahash')
    result['analysis_id'] = 'analysis_task'
    return result


def manifest_fixture():
    analysis = analysis_fixture()
    units = [{'unit_id': f'u{i}', 'speaker_id': t['speaker'], 'text_zh': '完整中文句子。',
              'source_word_ids': t['word_ids']} for i, t in enumerate(analysis['turns'])]
    return {'schema_version': 1, 'analysis_id': analysis['analysis_id'],
            'source_media_sha256': 'mediahash', 'units': units}, analysis


def test_complete_speaker_units_and_caption_decoupling():
    manifest, analysis = manifest_fixture()
    manifest['units'][0]['captions'] = [{'text_zh': '完整中文'}, {'text_zh': '句子。'}]
    units = validate_manifest(manifest, analysis)
    assert len(units) == 3
    assert len(units[0]['captions']) == 2
    assert analysis['references'].keys() == {'A', 'B'}


def test_context_restores_clipped_edges_without_crossing_source_words():
    from pipeline.speaker_verification import context_bounds,verified_word_assignments
    words=[dict(word_id=i,start=s,end=e,speaker='B',text='word')
           for i,(s,e) in enumerate([(0,.7),(1,1.2),(1.25,1.3),(1.4,1.7),(1.8,2)])]
    bounds=context_bounds(words,[1,2,3])
    assert bounds==pytest.approx((.85,1.75))
    evidence={'units':[dict(word_ids=[1,2,3],scores=[['A',.27],['B',.12]],
        context_scores=[['A',.32],['B',.14]],context_duration=.9,runs=[])]}
    decision=verified_word_assignments({'words':words},evidence)
    assert {v['speaker'] for v in decision.values()}=={'A'}
    evidence['units'][0]['context_scores']=[['B',.6],['A',.1]]
    assert not verified_word_assignments({'words':words},evidence)


def test_real_short_sentence_score_patterns_resolve_complete_voice():
    from pipeline.speaker_verification import confident
    # Two real utterance patterns: native labels split "This is ..." and
    # "I'll see you in New York" across voices despite a clear acoustic lead.
    assert confident([['A',.291492],['B',.123951]],1.86)=='A'
    assert confident([['A',.29448],['B',.196306]],1.27)=='A'
    assert confident([['A',.316596],['B',.237041]],2.04) is None


@pytest.mark.parametrize('corruption', ['speaker', 'reorder', 'missing', 'duplicate', 'media', 'caption'])
def test_rejects_corrupted_translation_ownership(corruption):
    manifest, analysis = manifest_fixture()
    if corruption == 'speaker': manifest['units'][0]['speaker_id'] = 'B'
    if corruption == 'reorder': manifest['units'].reverse()
    if corruption == 'missing': manifest['units'].pop()
    if corruption == 'duplicate': manifest['units'][1]['source_word_ids'] = manifest['units'][0]['source_word_ids']
    if corruption == 'media': manifest['source_media_sha256'] = 'other'
    if corruption == 'caption': manifest['units'][0]['captions'] = [{'text_zh': 'missing words'}]
    with pytest.raises(ValueError): validate_manifest(manifest, analysis)


def test_ass_preserves_name_and_text_commas(tmp_path):
    from pipeline.subtitle_parser import parse_ass_subtitle
    ass = tmp_path/'x.ass'
    ass.write_text('[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                   'Dialogue: 0,0:00:00.00,0:00:02.00,Default,A,0,0,0,,Hello, world || 你好，世界\n')
    segments, translations, _ = parse_ass_subtitle(str(ass))
    assert segments[0]['speaker'] == 'A'
    assert segments[0]['text'] == 'Hello, world'


@pytest.mark.parametrize('labels', [('', ''), ('A', 'B')])
def test_short_answer_never_borrows_other_speaker(tmp_path, monkeypatch, labels):
    monkeypatch.setattr(refs, '_get_audio_duration_ms', lambda _: 10000)
    clips = []
    monkeypatch.setattr(refs, '_extract_audio_clip', lambda *a: clips.append(a))
    segments = [dict(start=0, end=1, speaker=labels[0]), dict(start=1, end=6, speaker=labels[1])]
    with pytest.raises(ValueError):
        refs.extract_ref_audio_speaker_local('x', segments, [{'segment_index': 0}], str(tmp_path))
    assert not clips


def test_same_speaker_fallback_crop_stays_inside_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(refs, '_get_audio_duration_ms', lambda _: 60000)
    clips = []
    monkeypatch.setattr(refs, '_extract_audio_clip', lambda _, s, e, p: clips.append((s,e)))
    refs.extract_ref_audio_speaker_local('x', [dict(start=0,end=1,speaker='A'),
        dict(start=20,end=50,speaker='A')], [{'segment_index':0}], str(tmp_path))
    assert 20000 <= clips[0][0] < clips[0][1] <= 50000


def test_reference_content_invalidates_cache(tmp_path):
    ref = tmp_path/'same.wav'
    ref.write_bytes(b'B')
    old = synthesis_key('完整句子', 'zh', str(ref), 'model1', {'temperature': .8}, 'A')
    ref.write_bytes(b'A')
    assert old != synthesis_key('完整句子','zh',str(ref),'model1',{'temperature': .8},'A')
    assert old != synthesis_key('完整句子','zh',str(ref),'model2',{'temperature': .8},'A')


def test_audio_timeline_uses_actual_clips_and_no_duplicate_edge_pause(tmp_path, monkeypatch):
    # Synthetic silence tests duration arithmetic, not perceived voice quality.
    clip = tmp_path/'clip.wav'
    AudioSegment.silent(duration=1000).export(str(clip), format='wav')
    from pipeline import step4b_sentence_mixer as mixer
    monkeypatch.setattr(mixer, '_normalize_tts_audio', lambda x: x)
    monkeypatch.setattr(mixer, '_apply_dynamics_processing', lambda x: x)
    result = mix_sentence_audio('', [dict(start=0,end=1,unit_id='u1',speaker='A'),
        dict(start=1,end=2,unit_id='u2',speaker='A')], [0,1], {0:'第一句',1:'第二句'},
        {0:str(clip),1:str(clip)},str(tmp_path/'out.wav'))
    assert result['time_mapping'][1]['mixed_start'] == 1
    assert result['mixed_duration'] == 2


def test_missing_tts_is_failure(tmp_path):
    with pytest.raises(ValueError, match='Missing synthesized'):
        mix_sentence_audio('',[dict(start=0,end=1)],[0],{0:'不能漏掉的句子'}, {}, str(tmp_path/'out.wav'))


@pytest.mark.parametrize('blocked', [False,True])
def test_zero_source_gap_has_video_hold(blocked):
    entries = [dict(type='tts_chinese',orig_start=0,orig_end=2,mixed_start=0,mixed_end=3),
               dict(type='tts_chinese',orig_start=2,orig_end=4,mixed_start=3.15,mixed_end=6.15)]
    if blocked:
        path, _ = video._build_block_filter_graph(entries,include_trailing_gap=False)
    else:
        path = video._build_filter_graph(entries)
    try:
        graph = Path(path).read_text()
        assert 'tpad=stop_mode=clone:stop_duration=0.150000' in graph
    finally:
        Path(path).unlink()


def test_source_analysis_rejects_unidentified_and_unaligned():
    for word in ({'word':'x','start':0,'end':1}, {'word':'x','speaker':'A'}):
        with pytest.raises(ValueError): build_analysis({'segments':[{'words':[word]}]},'media')


def test_natural_pause_does_not_discard_all_reference_candidates():
    words=[dict(word='hello',start=s,end=s+.5,speaker='A') for s in [0,.5,1,1.5,2,4,4.5,5,5.5,6]]
    result=build_analysis({'segments':[{'words':words}]},'media',
                          [dict(start=0,end=7,speaker='A')])
    assert result['references']['A']['end']-result['references']['A']['start']>=2


def test_reference_candidate_excludes_native_overlap():
    words=[dict(word='hello',start=s,end=s+.5,speaker='A') for s in [0,.5,1,1.5,2]]
    result=build_analysis({'segments':[{'words':words}]},'media',
        [dict(start=0,end=3,speaker='A'),dict(start=1,end=2,speaker='B')])
    assert 'A' not in result['references']


def test_cumulative_frames_match_across_block_boundaries():
    from pipeline.timeline_plan import video_chains
    entries=[dict(orig_start=i,orig_end=i+1,mixed_start=i*.88,mixed_end=i*.88+.73) for i in range(100)]
    def frames(parts):
        return sum(int(re.search(r'trim=end_frame=(\d+)',p)[1]) for p in parts)
    whole=frames(video_chains(entries,25))
    blocked=0
    for start in range(0,100,7):
        end=min(start+7,100)
        blocked+=frames(video_chains(entries[start:end+1] if end<100 else entries[start:end],25,end<100))
    assert whole==blocked==round(entries[-1]['mixed_end']*25)


def test_final_caption_splits_preserve_speaker_name(tmp_path):
    segment=dict(start=0,end=2,speaker='A',text='A whole sentence.',
                 captions=[dict(text_zh='完整'),dict(text_zh='句子。')])
    mapping=[dict(type='tts_chinese',segment_index=0,mixed_start=0,mixed_end=3)]
    path=video.generate_bilingual_srt([segment],{0:'完整句子。'},mapping,str(tmp_path/'final.ass'))
    dialogues=[line for line in Path(path).read_text().splitlines() if line.startswith('Dialogue:')]
    assert len(dialogues)==2
    assert all(line.split(',')[4]=='A' for line in dialogues)


def test_real_diarization_boundary_jitter_uses_unique_nearby_turn():
    transcript={'segments':[{'words':[
        {'word':'I','start':1023.138,'end':1023.178},
        {'word':"don't",'start':1023.198,'end':1023.319}]}]}
    turns=[dict(start=1003.688,end=1022.605,speaker='A'),
           dict(start=1023.17909375,end=1037.539,speaker='B')]
    analysis=build_analysis(transcript,'media',turns)
    assert [w['speaker'] for w in analysis['words']]==['B','B']
    assert analysis['words'][0]['speaker_assignment']=='boundary_tolerance'


@pytest.mark.parametrize('turns',[
    [dict(start=1.1,end=3,speaker='A')],
    [dict(start=0,end=.995,speaker='A'),dict(start=1.025,end=3,speaker='B')]])
def test_boundary_tolerance_rejects_large_gap_or_ambiguous_identity(turns):
    with pytest.raises(ValueError,match='speaker'):
        build_analysis({'segments':[{'words':[dict(word='I',start=1,end=1.01)]}]},'media',turns)


def test_native_result_cache_survives_binding_errors_and_validates_content(tmp_path,monkeypatch):
    from pipeline.speaker_analysis import _native_speaker_turns
    import subprocess
    calls=[]
    class Process:
        returncode=0
        def __init__(self,cmd,**kwargs):
            calls.append(cmd)
            Path(cmd[-1]).write_text(json.dumps([dict(start=0,end=3,speaker='A')]))
        def communicate(self,**kwargs):return '', ''
        def poll(self):return 0
    monkeypatch.setattr(subprocess,'Popen',Process)
    first=_native_speaker_turns('audio',str(tmp_path),'signature')
    assert _native_speaker_turns('audio',str(tmp_path),'signature')==first
    assert len(calls)==1
    (tmp_path/'speaker_turns.json').write_text('[]')
    assert _native_speaker_turns('audio',str(tmp_path),'signature')==first
    assert len(calls)==2
    _native_speaker_turns('changed-audio',str(tmp_path),'new-signature')
    assert len(calls)==3


def test_acoustic_verification_repairs_native_question_and_keeps_real_turn():
    from pipeline.speaker_verification import verified_word_assignments
    words=[dict(word_id=i,speaker=s,start=i*.5,end=(i+1)*.5) for i,s in enumerate(['A','B','B','A'])]
    evidence={'units':[dict(word_ids=list(range(4)),scores=[['B',.70],['A',.20]],runs=[
        dict(word_ids=[0],scores=[['B',.60],['A',.10]]),
        dict(word_ids=[1,2],scores=[['B',.80],['A',.10]]),
        dict(word_ids=[3],scores=[['B',.50],['A',.10]])])]}
    out=verified_word_assignments({'words':words},evidence)
    assert [out[i]['speaker'] for i in range(4)]==['B']*4
    words=[dict(word_id=i,speaker='A' if i<3 else 'B',start=i,end=i+1) for i in range(6)]
    evidence={'units':[dict(word_ids=list(range(6)),scores=[['A',.7],['B',.5]],runs=[
        dict(word_ids=[0,1,2],scores=[['A',.8],['B',.1]]),
        dict(word_ids=[3,4,5],scores=[['B',.8],['A',.1]])])]}
    out=verified_word_assignments({'words':words},evidence)
    assert [out[i]['speaker'] for i in range(6)]==['A']*3+['B']*3


def test_acoustic_verification_does_not_guess_tiny_or_ambiguous_answer():
    from pipeline.speaker_verification import verified_word_assignments,confident
    words=[dict(word_id=0,speaker='A',start=0,end=.15)]
    assert verified_word_assignments({'words':words},{'units':[dict(word_ids=[0],scores=[['B',.8],['A',.1]],runs=[])]})=={}
    assert confident([['B',.4],['A',.35]],2) is None
    assert confident([['A',.8]],3)=='A'


def test_only_verified_words_can_enter_final_cloning_reference():
    # Ambiguous internal words are retained as dialogue but excluded from references.
    words=[dict(word='hello',start=i*.5,end=(i+1)*.5,speaker='A') for i in range(8)]
    evidence={'units':[dict(word_ids=[0,1,2,3],scores=[['A',.8],['B',.1]],runs=[]),
                       dict(word_ids=[4,5,6,7],scores=[['A',.3],['B',.29]],runs=[])]}
    result=build_analysis({'segments':[{'words':words}]},'media',[dict(start=0,end=4,speaker='A')],verification=evidence)
    assert result['references']['A']['end']==2
    assert not result['words'][-1]['speaker_verified']


@pytest.mark.parametrize('fps_text', ['25', '30000/1001', '24000/1001'])
@pytest.mark.parametrize('blocked', [False, True])
def test_real_ffmpeg_preserves_irregular_audio_frame_plan(tmp_path, fps_text, blocked):
    """Exercise encoding: inspecting the planned graph alone missed lost frames."""
    import shutil
    import subprocess
    from fractions import Fraction
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg/ffprobe required')
    fps = float(Fraction(fps_text))
    source = tmp_path/'source.mp4'
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    f'color=c=blue:size=160x90:rate={fps_text}', '-t', '9',
                    '-c:v', 'libx264', '-preset', 'ultrafast', str(source)], check=True)
    entries, cursor = [], 0.0
    for i in range(51):
        duration = .16 + i % 7 * .073
        # Tiny source/output gaps and a new long pause all need real frames.
        source_start = i * .15
        source_end = source_start + (.149 if i % 4 == 0 else .1)
        entries.append(dict(type='tts_chinese', orig_start=source_start,
                            orig_end=source_end, mixed_start=cursor,
                            mixed_end=cursor+duration))
        cursor += duration + ([.009, .012, .035, .003][i % 4])
    # Adjacent source spans with a long new audio pause must hold the image.
    entries[11]['orig_start'] = entries[10]['orig_end']
    shift = 1.6
    for entry in entries[11:]:
        entry['mixed_start'] += shift
        entry['mixed_end'] += shift
    outputs = []
    size = 7 if blocked else len(entries)
    for start in range(0, len(entries), size):
        end = min(start+size, len(entries))
        if blocked:
            gp, _ = video._build_block_filter_graph(entries[start:end+1],
                include_trailing_gap=end < len(entries), fps=fps)
        else:
            gp = video._build_filter_graph(entries, fps=fps)
        output = tmp_path/f'block_{start}.mp4'
        expected = round((entries[end]['mixed_start'] if end < len(entries)
                          else entries[-1]['mixed_end'])*fps)-round(entries[start]['mixed_start']*fps)
        try:
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(source),
                '-/filter_complex', gp, '-map', '[outv]', '-an', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-r', str(fps), str(output)], check=True)
            video._validate_video_frames(str(output), expected)
        finally:
            Path(gp).unlink()
        outputs.append(output)
    concat = tmp_path/'concat.txt'
    concat.write_text(''.join(f"file '{p}'\n" for p in outputs))
    result = tmp_path/'final.mp4'
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0',
                    '-i', str(concat), '-c', 'copy', str(result)], check=True)
    video._validate_video_frames(str(result), round(entries[-1]['mixed_end']*fps))
    streams = json.loads(subprocess.check_output(['ffprobe', '-v', 'error',
        '-show_entries', 'stream=duration', '-of', 'json', str(result)], text=True))['streams']
    assert abs(float(streams[0]['duration'])-entries[-1]['mixed_end']) <= 1/fps


def test_successful_encode_with_missing_frames_is_rejected(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: '{"streams":[{"nb_frames":"4"}]}')
    with pytest.raises(RuntimeError, match='expected 5, got 4'):
        video._validate_video_frames('short.mp4', 5)
