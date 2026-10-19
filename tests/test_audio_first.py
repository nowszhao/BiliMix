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
