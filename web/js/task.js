/* ============================================================
   BiliMix — 任务提交 / 取消 / 轮询 / 进度模块
   依赖: state.js, utils.js, audio-sync.js
   ============================================================ */

// ============================================================
// Input Mode Switching
// ============================================================

function switchInputMode(mode) {
    currentInputMode = mode;
    document.querySelectorAll('.input-mode-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.mode === mode);
    });
    document.querySelectorAll('.input-mode-content').forEach(content => {
        content.classList.toggle('active', content.id === `input-mode-${mode}`);
    });
}

// ============================================================
// Submit Task
// ============================================================

async function submitTask() {
    const difficultySelect = document.getElementById('difficulty-select');
    const modeSelect = document.getElementById('mode-select');
    const btn = document.getElementById('generate-btn');

    // 如果有 Mini Player 在播放，先关闭
    if (miniPlayerState) {
        miniPlayerClose();
    }

    let url = '';
    let title = '';
    if (currentInputMode === 'search') {
        url = selectedEpisodeUrl;
        title = selectedEpisodeTitle;
        if (!url) { showToast('❗ 请先搜索并选择一个播客单集'); return; }
    } else if (currentInputMode === 'rss') {
        url = rssSelectedEpisodeUrl;
        title = rssSelectedEpisodeTitle;
        if (!url) { showToast('❗ 请先解析 RSS 并选择一个单集'); return; }
    } else {
        const urlInput = document.getElementById('audio-url');
        url = urlInput.value.trim();
        if (!url) { shakeElement(urlInput.parentElement); urlInput.focus(); return; }
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            shakeElement(urlInput.parentElement); urlInput.focus(); return;
        }
    }

    tasks_url = url;
    currentTaskTitle = title;
    currentProcessMode = modeSelect ? modeSelect.value : 'word_replace';
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '提交中...';

    try {
        const resp = await fetch('/api/submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: url,
                title: title,
                difficulty: difficultySelect.value,
                process_mode: currentProcessMode,
                skip_confirmation: document.getElementById('skip-confirm-check')?.checked ?? true,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || '提交失败');
            btn.disabled = false;
            btn.querySelector('.btn-text').textContent = '开始生成';
            return;
        }
        currentTaskId = data.task_id;
        showSection('progress');
        startPolling();
    } catch (err) {
        alert('网络错误: ' + err.message);
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '开始生成';
    }
}

// ============================================================
// Cancel Task
// ============================================================

async function cancelTask() {
    if (!currentTaskId) return;

    const btn = document.getElementById('cancel-btn');
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '终止中...';

    try {
        const resp = await fetch(`/api/task/${currentTaskId}/cancel`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || '终止失败');
            btn.disabled = false;
            btn.querySelector('.btn-text').textContent = '终止任务';
            return;
        }
        stopPolling();
        setTimeout(() => {
            updateProgress({
                status: 'cancelled',
                progress: get_current_progress(),
                message: '任务已被终止',
                step: 'done',
            });
            showCancelledUI();
        }, 500);
    } catch (err) {
        alert('网络错误: ' + err.message);
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '终止任务';
    }
}

function get_current_progress() {
    const pct = document.getElementById('progress-pct');
    return parseInt(pct.textContent) || 0;
}

function showCancelledUI() {
    const statusText = document.getElementById('progress-status-text');
    const spinner = document.getElementById('progress-spinner');
    const pct = document.getElementById('progress-pct');
    const cancelArea = document.getElementById('cancel-area');

    statusText.textContent = '已终止';
    spinner.className = 'spinner error';
    pct.style.color = 'var(--warning)';
    cancelArea.innerHTML = `
        <button class="btn-secondary" onclick="resetAll()" style="margin-top: 8px;">
            ← 返回首页
        </button>
    `;
}

// ============================================================
// Polling
// ============================================================

function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, 1500);
    pollStatus();
}

function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function pollStatus() {
    if (!currentTaskId) return;

    try {
        const resp = await fetch(`/api/task/${currentTaskId}`);
        const data = await resp.json();

        if (data.process_mode) currentProcessMode = data.process_mode;
        updateProgress(data);

        if (data.status === 'completed') {
            stopPolling();
            await loadResult();
        } else if (data.status === 'awaiting_confirmation') {
            stopPolling();
            await loadConfirmationUI();
        } else if (data.status === 'awaiting_sentence_confirmation') {
            stopPolling();
            await loadSentenceConfirmationUI();
        } else if (data.status === 'error') {
            stopPolling();
            showError(data.message);
            showCancelledUI();
        } else if (data.status === 'cancelled') {
            stopPolling();
            showCancelledUI();
        }
    } catch (err) {
        console.error('Poll error:', err);
    }
}

// ============================================================
// Progress UI
// ============================================================

function updateProgress(data) {
    const bar = document.getElementById('progress-bar');
    const pct = document.getElementById('progress-pct');
    const message = document.getElementById('progress-message');
    const statusText = document.getElementById('progress-status-text');
    const spinner = document.getElementById('progress-spinner');

    const progress = data.progress || 0;
    bar.style.width = progress + '%';
    pct.textContent = progress + '%';
    message.textContent = data.message || '';

    if (data.status === 'completed') {
        statusText.textContent = '处理完成';
        spinner.className = 'spinner done';
        pct.style.color = '#34C759';
    } else if (data.status === 'error') {
        statusText.textContent = '处理出错';
        spinner.className = 'spinner error';
        pct.style.color = '#FF3B30';
    } else if (data.status === 'cancelled') {
        statusText.textContent = '已终止';
        spinner.className = 'spinner error';
        pct.style.color = '#FF9500';
    } else {
        statusText.textContent = '处理中...';
    }

    // 步骤条
    const mode = currentProcessMode;
    const wordSteps = document.getElementById('steps-word-replace');
    const sentSteps = document.getElementById('steps-sentence-translate');
    const smartSteps = document.getElementById('steps-smart-translate');

    if (wordSteps) wordSteps.classList.add('hidden');
    if (sentSteps) sentSteps.classList.add('hidden');
    if (smartSteps) smartSteps.classList.add('hidden');

    let stepsConfig;
    if (mode === 'smart_translate') {
        if (smartSteps) smartSteps.classList.remove('hidden');
        stepsConfig = {
            steps: ['download', 'transcribe', 'identify', 'translate', 'confirm', 'synthesize', 'mix'],
            stepOrder: {
                'download': 0, 'downloading': 0, 'transcribe': 1, 'identify': 2,
                'translate': 3, 'confirm_sentence': 4, 'confirm': 4,
                'synthesize': 5, 'merge': 6, 'vocabulary': 6, 'done': 7,
            },
            prefix: 'sm-step-', container: smartSteps,
        };
    } else if (mode === 'sentence_translate') {
        if (sentSteps) sentSteps.classList.remove('hidden');
        stepsConfig = {
            steps: ['download', 'transcribe', 'translate', 'confirm', 'synthesize', 'mix'],
            stepOrder: {
                'download': 0, 'downloading': 0, 'transcribe': 1, 'translate': 2,
                'confirm_sentence': 3, 'confirm': 3, 'synthesize': 4,
                'merge': 5, 'vocabulary': 5, 'done': 6,
            },
            prefix: 'st-step-', container: sentSteps,
        };
    } else {
        if (wordSteps) wordSteps.classList.remove('hidden');
        stepsConfig = {
            steps: ['download', 'transcribe', 'identify', 'confirm', 'synthesize', 'merge'],
            stepOrder: {
                'download': 0, 'downloading': 0, 'transcribe': 1, 'identify': 2,
                'translate': 2, 'confirm': 3, 'confirm_sentence': 3,
                'synthesize': 4, 'merge': 5, 'vocabulary': 5, 'done': 6,
            },
            prefix: 'step-', container: wordSteps,
        };
    }

    const currentStep = stepsConfig.stepOrder[data.step] ?? -1;
    stepsConfig.steps.forEach((step, i) => {
        const el = document.getElementById(stepsConfig.prefix + step);
        if (!el) return;
        el.classList.remove('active', 'done');
        if (i < currentStep) el.classList.add('done');
        else if (i === currentStep) el.classList.add('active');
    });

    if (stepsConfig.container) {
        stepsConfig.container.querySelectorAll('.step-line').forEach((line, i) => {
            line.classList.remove('done');
            if (i < currentStep) line.classList.add('done');
        });
    }
}

function showError(message) {
    document.getElementById('progress-message').textContent = '❌ ' + message;
    document.getElementById('progress-message').style.color = '#FF3B30';
}

// ============================================================
// Mode Selection
// ============================================================

function onModeChange() {
    const modeSelect = document.getElementById('mode-select');
    const modeHint = document.getElementById('mode-hint');
    const modeHintText = document.getElementById('mode-hint-text');
    const difficultyGroup = modeSelect.closest('.input-options')
        .querySelector('.option-group:first-child');
    const mode = modeSelect.value;

    if (mode === 'sentence_translate') {
        modeHint.style.display = 'flex';
        modeHintText.textContent = '句子翻译模式：按比例均匀间隔选句，将部分英文句子替换为中文翻译';
        difficultyGroup.style.opacity = '0.4';
        difficultyGroup.style.pointerEvents = 'none';
    } else if (mode === 'smart_translate') {
        modeHint.style.display = 'flex';
        modeHintText.textContent = '智能翻译模式（推荐）：先识别生词，再将含有生词的句子整句翻译替换，听感更自然';
        difficultyGroup.style.opacity = '1';
        difficultyGroup.style.pointerEvents = '';
    } else {
        modeHint.style.display = 'none';
        difficultyGroup.style.opacity = '1';
        difficultyGroup.style.pointerEvents = '';
    }
}
