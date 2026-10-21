"""Offline listening review: no remote credentials and no automatic assignment."""
from __future__ import annotations

import html
import json
from pathlib import Path

from _export import quality
from _project import write_json


def create_review(work, analysis, video):
    report = quality(analysis)
    issues = list(report['unresolved_segments'])
    if not issues:
        issues = [dict(start=w['start'], end=w['end'], text=w['text'], word_ids=[w['word_id']],
                       speaker_candidates=w.get('speaker_candidates', []))
                  for w in analysis['words'] if not w.get('speaker')]
    refs = analysis.get('references', {})
    folder = work / 'output'
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / 'speaker_review.json', {**report, 'segments': issues, 'references': refs})
    def play(start, end, label):
        return f'<button data-start="{float(start):.3f}" data-end="{float(end):.3f}">{html.escape(label)}</button>'
    references = ''.join(play(r['start'], r['end'], f'听人物 {speaker} 的参考') for speaker, r in refs.items())
    rows = []
    for issue in issues:
        options = '<option value="">听后选择人物</option>' + ''.join(
            f'<option value="{html.escape(s, quote=True)}">{html.escape(s)}</option>' for s in refs)
        ids = html.escape(json.dumps(issue['word_ids']), quote=True)
        rows.append(f'<section><p>{issue["start"]:.3f}–{issue["end"]:.3f}s：{html.escape(issue["text"])}</p>'
                    + play(max(0, issue['start']-.4), issue['end']+.4, '听原声及上下文')
                    + f'<select data-ids="{ids}">{options}</select></section>')
    source = html.escape(Path(video).as_uri(), quote=True)
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>人物确认</title>
<style>body{max-width:960px;margin:28px auto;padding:0 20px;font:16px/1.7 sans-serif;color:#20242b}
video{width:100%;max-height:48vh;background:#111;position:sticky;top:0}section{padding:15px 0;border-bottom:1px solid #ddd}
button,select{font:inherit;margin:5px;padding:6px}button{cursor:pointer}</style>
<h1>听原声后确认人物</h1><p>先听人物参考，再逐段确认。候选顺序不代表答案。未选择的片段保持待确认。</p>
''' + f'<video controls id="player" src="{source}"></video><p>{references}</p>' + ''.join(rows) + '''
<button id="save">下载已确认的 decisions.json</button><p id="message"></p>
<script>
const player=document.getElementById('player');let stop=Infinity;
document.querySelectorAll('button[data-start]').forEach(button=>button.onclick=()=>{
  stop=Number(button.dataset.end);player.currentTime=Number(button.dataset.start);player.play().catch(()=>{});
});player.ontimeupdate=()=>{if(player.currentTime>=stop){player.pause();stop=Infinity;}};
document.getElementById('save').onclick=()=>{
  const result=[...document.querySelectorAll('select[data-ids]')].filter(s=>s.value)
    .map(s=>({word_ids:JSON.parse(s.dataset.ids),speaker_id:s.value}));
  if(!result.length){document.getElementById('message').textContent='请先听原声并选择人物。';return;}
  const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download='decisions.json';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
};</script></html>'''
    path = folder / 'speaker_review.html'
    path.write_text(document, encoding='utf-8')
    missing = bool(report['missing_reference_speakers'])
    return {'stage': 'reference_missing' if missing else 'speaker_review',
            'action': 'inspect_missing_reference' if missing else 'user_listen', 'review': str(path),
            'quality': report, 'instructions': 'references/operations.md#speakers'}
