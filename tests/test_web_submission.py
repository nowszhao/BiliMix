"""Web submissions preserve the refined ASS, manifest and source analysis binding."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js required for Web behavior tests')
def test_refined_video_submission_requires_both_files_and_preserves_analysis():
    source = Path(__file__).resolve().parents[1] / 'web/js/task.js'
    script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const code = fs.readFileSync(SOURCE_PATH, 'utf8');
async function run(hasAss, hasManifest, segmentClone=false) {
    const sent = [], alerts = [];
    const elements = {
        'sub-font-size': {value: '40'},
        'keep-bgm-checkbox': {checked: false},
        'video-segment-clone-checkbox': {checked: segmentClone},
    };
    const context = {
        document: {getElementById: id => elements[id] || null, querySelector: () => null},
        currentVideoMode: 'local', videoUploadedPath: '/srv/source.mp4', videoUploadedName: 'source.mp4',
        alert: message => alerts.push(message), showToast: () => {},
        fetch: async (url, options) => {
            sent.push(JSON.parse(options.body));
            return {ok: true, json: async () => ({task_id: 'submitted'})};
        },
    };
    vm.createContext(context);
    vm.runInContext(code, context);
    const task = `
        currentSubtitlePath = ${hasAss ? "'/srv/refined.ass'" : "''"};
        currentDubManifest = ${hasManifest ? "{schema_version:1,analysis_id:'analysis',source_media_sha256:'hash',units:[{unit_id:'u1'}]}" : 'null'};
        clearVideoUpload = () => {};
        closeNewTaskModal = () => {};
        submitVideoTask({disabled:false,querySelector:()=>({textContent:''})});
    `;
    await vm.runInContext(task, context);
    if (hasAss && hasManifest) {
        assert.equal(sent.length, 1);
        assert.equal(sent[0].local_path, '/srv/source.mp4');
        assert.equal(sent[0].subtitle_path, '/srv/refined.ass');
        assert.equal(sent[0].analysis_id, 'analysis');
        assert.equal(sent[0].tts_reference_mode, segmentClone ? 'segment' : 'speaker');
        assert.equal(sent[0].dub_manifest.units[0].unit_id, 'u1');
        assert.equal(alerts.length, 0);
    } else {
        assert.equal(sent.length, 0);
        assert.match(alerts[0], /同时选择/);
    }
}
(async () => { await run(true, false); await run(false, true); await run(true, true); await run(true, true, true); })()
.catch(error => { console.error(error); process.exitCode = 1; });
'''.replace('SOURCE_PATH', json.dumps(str(source)))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
