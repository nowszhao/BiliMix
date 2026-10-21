"""Run the real skill scripts through refinement, translation, and submission data."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pipeline.dubbing_plan import validate_manifest
from tests.test_audio_first import analysis_fixture

SKILLS = Path(os.environ.get('BILIMIX_SKILLS_DIR', str(Path(__file__).resolve().parents[1]/'skills')))
if not (SKILLS/'nows-ytb-bilingualex/scripts').is_dir():
    raise RuntimeError('Bundled skills missing; restore repository skills/ or set BILIMIX_SKILLS_DIR')
SCRIPTS = SKILLS/'nows-ytb-bilingualex/scripts'


def run(script, *args):
    result = subprocess.run([sys.executable, '-B', str(SCRIPTS/script), *map(str,args)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout+result.stderr
    return result.stdout


def test_real_skill_pipeline_keeps_full_speech_and_speaker_ownership(tmp_path):
    analysis = analysis_fixture()
    (tmp_path/'analysis.json').write_text(json.dumps(analysis))
    run('import_analysis.py', tmp_path/'analysis.json','--workdir',tmp_path)
    run('refine_srt.py','prepare-full',tmp_path/'transcript_official.srt',tmp_path/'raw_aligned.srt','--workdir',tmp_path)
    # Translator decisions: keep complete turns; actual source words unchanged.
    (tmp_path/'refine_parts/refined_01.txt').write_text('\n'.join(
        f'S{i}\t{turn["text"]}.' for i,turn in enumerate(analysis['turns'],1))+'\n')
    run('check_refined.py','--workdir',tmp_path)
    run('refine_srt.py','apply','--workdir',tmp_path)
    run('split_translation.py',tmp_path/'transcript_refined.srt','--out',tmp_path)
    rows=(tmp_path/'transcript.txt').read_text().splitlines()
    (tmp_path/'parts/trans_01.txt').write_text('\n'.join(row+'\t完整自然的一句话。' for row in rows)+'\n')
    run('assemble_final.py','--workdir',tmp_path)
    run('build_dub_manifest.py','prepare','--workdir',tmp_path)
    plan={'units':[{'source_indices':[i],'text_zh':'完整自然的一句话。'} for i in range(1,4)]}
    (tmp_path/'dub_units_plan.json').write_text(json.dumps(plan))
    run('build_dub_manifest.py','apply','--workdir',tmp_path)
    manifest=json.loads((tmp_path/'dub_manifest.json').read_text())
    assert [u['speaker'] for u in validate_manifest(manifest,analysis)] == ['A','B','A']


def test_missing_refined_block_fails(tmp_path):
    (tmp_path/'refine_parts').mkdir()
    (tmp_path/'refine_plan.json').write_text(json.dumps({'parts':[{'file':'refine_part_01.txt'}]}))
    (tmp_path/'refine_parts/refine_part_01.txt').write_text('1\t0\t1\tx\n')
    result=subprocess.run([sys.executable,'-B',str(SCRIPTS/'check_refined.py'),'--workdir',str(tmp_path)],capture_output=True,text=True)
    assert result.returncode != 0


def test_ass_centisecond_rounding_carries(tmp_path):
    srt=tmp_path/'x.srt'
    srt.write_text('1\n00:00:00,995 --> 00:00:02,000\nA complete sentence.\n')
    run('split_translation.py',srt,'--out',tmp_path)
    assert '\t0:00:01.00\t' in (tmp_path/'transcript.txt').read_text()


def test_exact_fallback_last_sentence_end_and_word_sequence(tmp_path):
    script=SKILLS/'nows-ytb-bmxtask/scripts/refine_apply_exact.py'
    (tmp_path/'transcript_official.srt').write_text(
        '1\n00:00:00,000 --> 00:00:01,000\none two three four five\n\n'
        '2\n00:00:01,000 --> 00:00:03,000\nsix seven eight nine ten\n')
    (tmp_path/'refine_parts').mkdir()
    (tmp_path/'refine_plan.json').write_text(json.dumps({'parts':[{'file':'refine_part_01.txt'}]}))
    refined=tmp_path/'refine_parts/refined_01.txt'
    refined.write_text('S1\tone two three four five six seven eight nine ten\n')
    result=subprocess.run([sys.executable,'-B',str(script),'--workdir',str(tmp_path)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert '--> 00:00:03,000' in (tmp_path/'transcript_refined.srt').read_text()
    refined.write_text('S1\ttwo one three four five six seven eight nine ten\n')
    result=subprocess.run([sys.executable,'-B',str(script),'--workdir',str(tmp_path)],capture_output=True,text=True)
    assert result.returncode!=0


def test_long_monologue_is_bounded_context_without_losing_words(tmp_path):
    analysis=analysis_fixture()
    words=[dict(word_id=i,text=f'word{i}',start=i*.3,end=(i+1)*.3,speaker='A') for i in range(205)]
    analysis['words']=words
    analysis['turns']=[dict(turn_id='turn1',speaker='A',start=0,end=61.5,
                            word_ids=list(range(205)),text=' '.join(w['text'] for w in words))]
    (tmp_path/'analysis.json').write_text(json.dumps(analysis))
    run('import_analysis.py',tmp_path/'analysis.json','--workdir',tmp_path)
    run('refine_srt.py','prepare-full',tmp_path/'transcript_official.srt',tmp_path/'raw_aligned.srt','--workdir',tmp_path)
    chunk=(tmp_path/'refine_parts/refine_part_01.txt').read_text()
    assert chunk.count('speaker=A turn=turn1')==3
    assert 'word204' in chunk


def test_wrapper_prefers_its_runtime_cli_over_old_path_cli(tmp_path, monkeypatch):
    import runpy
    module=runpy.run_path(str(SKILLS/'nows-ytb-bmxtask/scripts/bmx_video_task.py'))
    runtime=tmp_path/'runtime';runtime.mkdir()
    old=tmp_path/'old';old.mkdir()
    for root in (runtime,old):
        cli=root/'bmx';cli.write_text('#!/bin/sh\nexit 0\n');cli.chmod(0o755)
    monkeypatch.setattr(sys,'executable',str(runtime/'python'))
    monkeypatch.setenv('PATH',str(old))
    monkeypatch.delenv('BILIMIX_CLI',raising=False)
    resolve=module['resolve_bmx']
    assert resolve()==str(runtime/'bmx')
    assert resolve('/explicit/bmx')=='/explicit/bmx'


def test_environment_prefers_runtime_downloader_over_old_path_binary(tmp_path, monkeypatch):
    import runpy
    module=runpy.run_path(str(SKILLS/'nows-ytb-bilingualex/scripts/check_env.py'))
    runtime=tmp_path/'runtime';runtime.mkdir()
    old=tmp_path/'old';old.mkdir()
    for root in (runtime,old):
        binary=root/'yt-dlp';binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o755)
    monkeypatch.setattr(sys,'executable',str(runtime/'python'))
    monkeypatch.setenv('PATH',str(old))
    assert module['probe_yt_dlp'](str(tmp_path/'venv'))==str(runtime/'yt-dlp')
