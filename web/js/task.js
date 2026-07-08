/* ============================================================
   BiliMix — 任务提交 / 取消 / 轮询 / 进度模块
   依赖: state.js, utils.js, audio-sync.js
   ============================================================ */

// ============================================================
// Input Mode Switching
// ============================================================

function switchTopMode(mode) {
    currentTopMode = mode;
    document.querySelectorAll('.top-mode-tabs .input-mode-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.topmode === mode);
    });
    document.querySelectorAll('.top-mode-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `topmode-${mode}`);
    });
    // 切换时更新按钮文案
    const btn = document.getElementById('generate-btn');
    if (btn) {
        btn.querySelector('.btn-text').textContent = mode === 'video' ? '🎬 开始配音' : '开始生成';
    }
}

function switchInputMode(mode) {
    currentInputMode = mode;
    document.querySelectorAll('#topmode-audio .input-mode-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.mode === mode);
    });
    document.querySelectorAll('#topmode-audio .input-mode-content').forEach(content => {
        content.classList.toggle('active', content.id === `input-mode-${mode}`);
    });
}

function switchVideoMode(mode) {
    currentVideoMode = mode;
    document.querySelectorAll('#topmode-video .input-mode-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.vmode === mode);
    });
    document.querySelectorAll('#topmode-video .input-mode-content').forEach(content => {
        content.classList.toggle('active', content.id === `video-mode-${mode}`);
    });
}

function updateVideoOptions() {
    // 占位：视频选项变更时更新内部状态
    // 实际值在 submitTask 时读取
}

// ============================================================
// Video File Upload
// ============================================================

async function onVideoFileSelected(input) {
    const file = input.files[0];
    if (!file) return;

    const allowed = ['.mp4', '.mkv', '.mov', '.avi', '.webm'];
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    if (!allowed.includes(ext)) {
        showToast('❌ 不支持的视频格式，支持: ' + allowed.join(', '));
        input.value = '';
        return;
    }

    const infoEl = document.getElementById('video-upload-info');
    const nameEl = document.getElementById('video-upload-name');
    const sizeEl = document.getElementById('video-upload-size');
    const labelEl = document.getElementById('video-upload-label');

    infoEl.style.display = 'flex';
    nameEl.textContent = '⏳ 上传中...';
    sizeEl.textContent = formatFileSize(file.size);
    labelEl.textContent = '正在上传...';

    try {
        const formData = new FormData();
        formData.append('file', file);

        const resp = await fetch('/api/upload', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();

        if (!resp.ok || !data.ok) {
            showToast('❌ ' + (data.error || '上传失败'));
            infoEl.style.display = 'none';
            labelEl.textContent = '选择或拖拽视频文件';
            input.value = '';
            return;
        }

        videoUploadedPath = data.local_path;
        videoUploadedName = data.filename;
        nameEl.textContent = '✅ ' + data.filename;
        sizeEl.textContent = formatFileSize(file.size);
        labelEl.textContent = '上传完成，点击可重新选择';
        showToast('✅ 上传成功 (' + data.size_mb + ' MB)');
    } catch (err) {
        showToast('❌ 上传失败: ' + err.message);
        infoEl.style.display = 'none';
        labelEl.textContent = '选择或拖拽视频文件';
        input.value = '';
    }
}

function clearVideoUpload() {
    videoUploadedPath = '';
    videoUploadedName = '';
    document.getElementById('video-upload-info').style.display = 'none';
    document.getElementById('video-upload-input').value = '';
    document.getElementById('video-upload-label').textContent = '选择或拖拽视频文件';
}

// ============================================================
// File Upload
// ============================================================

let uploadedFilePath = '';
let uploadedFileName = '';

async function onFileSelected(input) {
    const file = input.files[0];
    if (!file) return;

    // 校验文件类型
    const allowed = ['.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac'];
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    if (!allowed.includes(ext)) {
        showToast('❌ 不支持的文件格式，支持: ' + allowed.join(', '));
        input.value = '';
        return;
    }

    // 显示上传中
    const infoEl = document.getElementById('file-upload-info');
    const nameEl = document.getElementById('file-upload-name');
    const sizeEl = document.getElementById('file-upload-size');
    const labelEl = document.getElementById('file-upload-label');

    infoEl.style.display = 'flex';
    nameEl.textContent = '⏳ 上传中...';
    sizeEl.textContent = formatFileSize(file.size);
    labelEl.textContent = '正在上传...';

    // 上传文件
    try {
        const formData = new FormData();
        formData.append('file', file);

        const resp = await fetch('/api/upload', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();

        if (!resp.ok || !data.ok) {
            showToast('❌ ' + (data.error || '上传失败'));
            infoEl.style.display = 'none';
            labelEl.textContent = '选择或拖拽音频文件';
            input.value = '';
            return;
        }

        uploadedFilePath = data.local_path;
        uploadedFileName = data.filename;
        nameEl.textContent = '✅ ' + data.filename;
        sizeEl.textContent = formatFileSize(file.size);
        labelEl.textContent = '上传完成，点击可重新选择';
        showToast('✅ 上传成功 (' + data.size_mb + ' MB)');
    } catch (err) {
        showToast('❌ 上传失败: ' + err.message);
        infoEl.style.display = 'none';
        labelEl.textContent = '选择或拖拽音频文件';
        input.value = '';
    }
}

function clearUploadedFile() {
    uploadedFilePath = '';
    uploadedFileName = '';
    document.getElementById('file-upload-info').style.display = 'none';
    document.getElementById('file-upload-input').value = '';
    document.getElementById('file-upload-label').textContent = '选择或拖拽音频文件';
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ============================================================
// Submit Task
// ============================================================

async function submitTask() {
    const btn = document.getElementById('generate-btn');
    if (!btn) return;

    // 如果有 Mini Player 在播放，先关闭
    if (miniPlayerState) {
        miniPlayerClose();
    }

    const isVideo = currentTopMode === 'video';
    isVideoTask = isVideo;

    if (isVideo) {
        // ========== 视频配音模式 ==========
        await submitVideoTask(btn);
    } else {
        // ========== 音频转录模式 ==========
        await submitAudioTask(btn);
    }
}

async function submitAudioTask(btn) {
    let url = '';
    let title = '';
    let localPath = '';

    if (currentInputMode === 'file') {
        if (!uploadedFilePath) {
            showToast('❗ 请先选择一个音频文件上传');
            return;
        }
        localPath = uploadedFilePath;
        title = uploadedFileName;
    } else {
        const urlInput = document.getElementById('audio-url');
        if (!urlInput) { showToast('❗ 请输入音频 URL'); return; }
        url = urlInput.value.trim();
        if (!url) { shakeElement(urlInput); urlInput.focus(); return; }
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            showToast('❗ 请输入有效的 HTTP/HTTPS 链接');
            return;
        }
    }

    tasks_url = url || ('file://' + localPath);
    currentTaskTitle = title;
    currentProcessMode = 'sentence_translate';
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '提交中...';

    try {
        const body = {
            title: title,
            skip_confirmation: true,
            type: 'audio',
        };
        if (localPath) {
            body.local_path = localPath;
        } else {
            body.url = url;
        }

        const resp = await fetch('/api/submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || '提交失败');
            btn.disabled = false;
            btn.querySelector('.btn-text').textContent = '开始生成';
            return;
        }
        currentTaskId = data.task_id;
        if (currentInputMode === 'file') clearUploadedFile();
        showToast('✅ 任务已提交');
        if (typeof switchView === 'function') switchView('tasks');
        setTimeout(() => {
            if (typeof toggleTaskDetail === 'function') {
                toggleTaskDetail(data.task_id, tasks_url);
            }
        }, 800);
    } catch (err) {
        alert('网络错误: ' + err.message);
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '开始生成';
    }
}

async function submitVideoTask(btn) {
    let url = '';
    let title = '';
    let localPath = '';

    if (currentVideoMode === 'local') {
        if (!videoUploadedPath) {
            showToast('❗ 请先选择一个视频文件上传');
            return;
        }
        localPath = videoUploadedPath;
        title = videoUploadedName;
    } else {
        const urlInput = document.getElementById('video-url');
        if (!urlInput) { showToast('❗ 请输入视频 URL'); return; }
        url = urlInput.value.trim();
        if (!url) { shakeElement(urlInput); urlInput.focus(); return; }
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            showToast('❗ 请输入有效的 HTTP/HTTPS 链接');
            return;
        }
    }

    // 读取字幕选项
    const subModeEl = document.querySelector('input[name="sub_mode"]:checked');
    const subMode = subModeEl ? subModeEl.value : 'bilingual';
    const fontSizeEl = document.getElementById('sub-font-size');
    const fontSize = fontSizeEl ? fontSizeEl.value : '20';

    tasks_url = url || ('file://' + localPath);
    currentTaskTitle = title;
    currentProcessMode = 'sentence_translate';
    isVideoTask = true;
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '提交中...';

    try {
        const body = {
            title: title,
            skip_confirmation: true,
            type: 'video',
            subtitle_mode: subMode,
            subtitle_font_size: parseInt(fontSize),
        };
        if (localPath) {
            body.local_path = localPath;
        } else {
            body.video_url = url;
        }

        const resp = await fetch('/api/submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || '提交失败');
            btn.disabled = false;
            btn.querySelector('.btn-text').textContent = '🎬 开始配音';
            return;
        }
        currentTaskId = data.task_id;
        if (currentVideoMode === 'local') clearVideoUpload();
        showToast('✅ 视频任务已提交');
        if (typeof switchView === 'function') switchView('tasks');
        setTimeout(() => {
            if (typeof toggleTaskDetail === 'function') {
                toggleTaskDetail(data.task_id, tasks_url);
            }
        }, 800);
    } catch (err) {
        alert('网络错误: ' + err.message);
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '🎬 开始配音';
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

function showCancelledUI(errorMsg) {
    const statusText = document.getElementById('progress-status-text');
    const spinner = document.getElementById('progress-spinner');
    const pct = document.getElementById('progress-pct');
    const cancelArea = document.getElementById('cancel-area');

    statusText.textContent = '已终止';
    spinner.className = 'spinner error';
    pct.style.color = 'var(--warning)';

    // 任何错误都显示断点续传按钮
    cancelArea.innerHTML = `
        <button class="btn-primary" onclick="retryTask()" style="margin-top: 8px; width: 100%;">
            🔄 从出错位置重试
        </button>
        <button class="btn-secondary" onclick="resetAll()" style="margin-top: 8px; width: 100%;">
            ← 返回首页
        </button>
    `;
}

async function retryTask() {
    if (!currentTaskId) return;

    const btn = document.querySelector('#cancel-area .btn-primary');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 重试中...'; }

    // 切回进度界面
    document.getElementById('progress-message').style.color = '';
    document.getElementById('progress-spinner').className = 'spinner';
    document.getElementById('progress-status-text').textContent = '处理中...';
    document.getElementById('cancel-area').innerHTML = '';

    try {
        const resp = await fetch(`/api/task/${currentTaskId}/retry`, {
            method: 'POST',
        });
        const data = await resp.json();
        if (!resp.ok) {
            showError(data.error || '重试失败');
            showCancelledUI(data.error);
            return;
        }
        startPolling();
    } catch (err) {
        showError('重试请求失败: ' + err.message);
        showCancelledUI(err.message);
    }
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
            showCancelledUI(data.message);
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

    // 步骤条 - always sentence_translate
    const sentSteps = document.getElementById('steps-sentence-translate');
    if (sentSteps) sentSteps.classList.remove('hidden');

    // 视频步骤显示/隐藏
    const videoSteps = document.querySelectorAll('.video-step');
    if (isVideoTask) {
        videoSteps.forEach(el => el.classList.add('show'));
    } else {
        videoSteps.forEach(el => el.classList.remove('show'));
    }

    const baseSteps = isVideoTask
        ? ['download', 'transcribe', 'translate', 'confirm', 'synthesize', 'mix', 'subtitle', 'assemble']
        : ['download', 'transcribe', 'translate', 'confirm', 'synthesize', 'mix'];

    const stepOrder = isVideoTask
        ? { 'download': 0, 'downloading': 0, 'transcribe': 1, 'translate': 2,
            'confirm_sentence': 3, 'confirm': 3, 'synthesize': 4,
            'merge': 5, 'mix': 5, 'subtitle': 6, 'assemble': 7, 'done': 8 }
        : { 'download': 0, 'downloading': 0, 'transcribe': 1, 'translate': 2,
            'confirm_sentence': 3, 'confirm': 3, 'synthesize': 4,
            'merge': 5, 'vocabulary': 5, 'done': 6 };

    const currentStep = stepOrder[data.step] ?? -1;
    baseSteps.forEach((step, i) => {
        const el = document.getElementById('st-step-' + step);
        if (!el) return;
        el.classList.remove('active', 'done');
        if (i < currentStep) el.classList.add('done');
        else if (i === currentStep) el.classList.add('active');
    });

    if (sentSteps) {
        sentSteps.querySelectorAll('.step-line').forEach((line, i) => {
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
