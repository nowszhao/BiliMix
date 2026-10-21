"""Display budgets are independent of TTS and preserved across export paths."""
import json
from pathlib import Path
import random
import re

import pytest
from pipeline.subtitle_layout import SubtitleLayout,ass_document,text_width

FIXTURE = json.loads((Path(__file__).parent/'fixtures/long_utterance.json').read_text(encoding='utf-8'))


def compact(text):
    return re.sub(r'\s+','',text)


@pytest.mark.parametrize('dimensions',[(1280,720),(1920,1080),(1080,1920),(320,180)])
@pytest.mark.parametrize('size',[40,120])
def test_real_long_utterance_is_paginated_without_changing_text_or_time(dimensions,size):
    layout = SubtitleLayout(*dimensions,size)
    entry = dict(start=2.25,end=16.75,chinese=FIXTURE['text_zh'],english=FIXTURE['text_en'],speaker='A')
    pages = layout.paginate(entry)
    assert len(pages)>1
    assert pages[0]['start']==entry['start'] and pages[-1]['end']==entry['end']
    assert compact(''.join(p['chinese']for p in pages))==compact(entry['chinese'])
    assert compact(''.join(p['english']for p in pages))==compact(entry['english'])
    for i,page in enumerate(pages):
        assert page['speaker']=='A' and page['end']>page['start']
        assert len(page['chinese_lines'])<=2 and len(page['english_lines'])<=2
        assert page['line_count']<=4
        assert page['estimated_height']<=dimensions[1]*.25
        if i:assert page['start']==pages[i-1]['end']
        for line in page['chinese_lines']:assert text_width(line,layout.font_size)<=layout.text_width
        for line in page['english_lines']:assert text_width(line,layout.english_size)<=layout.text_width


@pytest.mark.parametrize('language',['chinese','english'])
def test_long_unbroken_content_and_combining_characters_stay_bounded(language):
    text = 'a'*350+'，中文 e\u0301 '+('https://example.invalid/'+'x'*200)
    entry = dict(start=0,end=30,chinese='',english='');entry[language]=text
    layout = SubtitleLayout(480,270,40)
    pages = layout.paginate(entry)
    assert compact(''.join(p[language]for p in pages))==compact(text)
    assert all(p['line_count']<=4 and p['estimated_height']<=67.5 for p in pages)


def test_fixed_seed_variable_bilingual_lengths_preserve_all_content():
    randomizer = random.Random(7)
    layout = SubtitleLayout(720,1280,48)
    for _ in range(20):
        cn='完整的话语，'+''.join(randomizer.choice('中文。测试、')for _ in range(randomizer.randrange(1,160)))
        en=' '.join(randomizer.choice(['context','a','longer','sentence.'])for _ in range(randomizer.randrange(1,120)))
        pages=layout.paginate(dict(start=0,end=15,chinese=cn,english=en))
        assert compact(''.join(p['chinese']for p in pages))==compact(cn)
        assert compact(''.join(p['english']for p in pages))==compact(en)
        assert all(p['line_count']<=4 for p in pages)


def test_source_ass_controls_cannot_override_font_or_create_hidden_lines():
    layout=SubtitleLayout(1280,720,40)
    document,report=ass_document([dict(start=0,end=10,chinese=r'完整{\fs400}句子\n后半句',english='Complete sentence.',speaker='A')],layout)
    assert '\\fs400' not in document
    assert '{\\rChinese\\q2}' in document and '{\\rEnglish\\q2}' in document
    assert report['max_observed_lines']<=4


def test_whole_and_block_export_use_identical_display_layout(tmp_path):
    from pipeline import step5_video_assemble as video
    segments=[dict(start=0,end=10,speaker='A',text=FIXTURE['text_en'])]
    translations={0:FIXTURE['text_zh']}
    mapping=[dict(type='tts_chinese',segment_index=0,orig_start=0,orig_end=10,
                  mixed_start=0,mixed_end=15,chinese=FIXTURE['text_zh'],speaker='A')]
    whole=video.generate_bilingual_srt(segments,translations,mapping,str(tmp_path/'whole.ass'),
        video_width=1280,video_height=720,subtitle_font_size=40)
    block=video._build_block_ass(mapping,0,str(tmp_path/'block.ass'),video_width=1280,
        video_height=720,subtitle_mode='bilingual',font_size=40,segments=segments,translations=translations)
    assert Path(whole).read_text(encoding='utf-8')==Path(block).read_text(encoding='utf-8')
    report=json.loads(Path(whole+'.layout.json').read_text(encoding='utf-8'))
    assert report['display_pages']>1 and report['max_observed_lines']<=4
    assert len(segments)==len(mapping)==1  # No new speech/TTS units.


def test_invalid_caption_budget_fails_instead_of_overflowing():
    with pytest.raises(ValueError):SubtitleLayout(1280,720,40,max_lines=1)


def test_sub_centisecond_pages_fail_instead_of_becoming_invisible():
    with pytest.raises(ValueError,match='centisecond'):
        ass_document([dict(start=0,end=.001,chinese='完整句子。',english='A complete sentence.')],SubtitleLayout(1280,720,40))


def test_none_mode_writes_no_visible_dialogue(tmp_path):
    from pipeline.step5_video_assemble import generate_bilingual_srt
    path=generate_bilingual_srt([dict(start=0,end=2,text='Source',speaker='A')],{0:'完整句子。'},
        [dict(type='tts_chinese',segment_index=0,mixed_start=0,mixed_end=3)],
        str(tmp_path/'none.ass'),subtitle_mode='none',video_width=1280,video_height=720)
    assert not any(line.startswith('Dialogue:')for line in Path(path).read_text(encoding='utf-8').splitlines())
    assert json.loads(Path(path+'.layout.json').read_text(encoding='utf-8'))['display_pages']==0
