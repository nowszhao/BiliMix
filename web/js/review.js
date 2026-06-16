/* ============================================================
   BiliMix — TTS 试听审查模块
   依赖: state.js, utils.js
   支持两种模式：
     - 实时模式：TTS 合成进行中，展示进度，禁播未完成段，持续轮询
     - 审查模式：合成完成，全部可播放，显示确认按钮
   ============================================================ */

let ttsReviewPollTimer = null;

// ============================================================
// 加载审查 UI
// ============================================================

async function loadTtsReviewUI() {
    if (!currentTaskId) return;

    try {
        const resp = await fetch(`/api/task/${currentTaskId}/tts-review`);
        const data = await resp.json();
        ttsReviewSegments = data.segments || [];

        const isComplete = data.task_status === 'awaiting_tts_review';
        const isLive = data.task_status === 'processing' && data.task_step === 'synthesize';

        showSection('tts-review');
        renderTtsReviewList(isComplete);
        updateTtsReviewInfo(data);
        updateTtsReviewControls(isComplete);

        // 初始化音频元素
        ttsReviewAudio = document.getElementById('tts-review-audio');
        if (ttsReviewAudio) {
            ttsReviewAudio.removeEventListener('ended', onTtsReviewSegmentEnded);
            ttsReviewAudio.addEventListener('ended', onTtsReviewSegmentEnded);
        }

        // 实时模式：继续轮询刷新
        if (isLive) {
            startTtsReviewPolling();
        } else if (isComplete) {
            stopTtsReviewPolling();
        }
    } catch (err) {
        console.error('加载 TTS 审查数据失败:', err);
        showToast('❌ 加载审查数据失败: ' + err.message);
    }
}

function startTtsReviewPolling() {
    stopTtsReviewPolling();
    ttsReviewPollTimer = setInterval(refreshTtsReviewData, 2000);
}

function stopTtsReviewPolling() {
    if (ttsReviewPollTimer) {
        clearInterval(ttsReviewPollTimer);
        ttsReviewPollTimer = null;
    }
}

async function refreshTtsReviewData() {
    if (!currentTaskId) {
        stopTtsReviewPolling();
        return;
    }
    try {
        const resp = await fetch(`/api/task/${currentTaskId}/tts-review`);
        const data = await resp.json();
        ttsReviewSegments = data.segments || [];

        const isComplete = data.task_status === 'awaiting_tts_review';
        renderTtsReviewList(isComplete);
        updateTtsReviewInfo(data);
        updateTtsReviewControls(isComplete);

        // 合成完成时恢复播放按钮高亮（如果正在顺序播放）
        if (isComplete) {
            stopTtsReviewPolling();
            if (ttsReviewPlayAllActive) {
                // 重新构建播放队列（可能有新完成的 segment）
                ttsReviewPlayQueue = ttsReviewSegments.filter(
                    s => s.tts_url && s.tts_status === 'completed'
                );
            }
        }
    } catch (err) {
        console.error('刷新 TTS 审查数据失败:', err);
    }
}

// ============================================================
// 渲染 Segment 列表
// ============================================================

function renderTtsReviewList(isComplete) {
    const list = document.getElementById('tts-review-list');
    if (!list) return;

    let html = '';
    ttsReviewSegments.forEach((seg, i) => {
        const idx = seg.seg_idx;
        const english = escapeHtml(seg.english_text || '').substring(0, 80);
        const chinese = escapeHtml(seg.chinese_text || '').substring(0, 60);
        const status = seg.tts_status;
        const isDone = status === 'completed';
        const hasTts = isDone && seg.tts_url;
        const hasRef = seg.ref_audio_url;
        const duration = seg.tts_duration_s ? `${seg.tts_duration_s}s` : '';
        const size = seg.tts_size_kb ? `${seg.tts_size_kb}KB` : '';

        let statusHtml = '';
        if (isDone) {
            statusHtml = `<span class="tts-review-item-status status-ok">✅ ${duration} ${size}</span>`;
        } else if (status === 'missing') {
            statusHtml = `<span class="tts-review-item-status status-err">❌ 缺失</span>`;
        } else {
            statusHtml = `<span class="tts-review-item-status status-pending">⏳ 合成中...</span>`;
        }

        html += `
        <div class="tts-review-item ${isDone ? '' : 'tts-pending'}" id="tts-review-item-${idx}" data-seg-idx="${idx}">
            <div class="tts-review-item-header">
                <span class="tts-review-item-num">#${idx + 1}</span>
                ${statusHtml}
            </div>
            <div class="tts-review-item-english">${english}</div>
            <div class="tts-review-item-chinese">${chinese}</div>
            <div class="tts-review-item-actions">
                ${hasTts ? `
                <button class="tts-play-btn tts-btn-tts" onclick="ttsReviewPlaySeg(${idx}, 'tts')"
                        id="tts-play-btn-${idx}" title="播放中文 TTS">
                    🎧 听合成
                </button>` : `<span class="tts-no-audio">${isComplete ? '无合成音频' : '等待合成...'}</span>`}
                ${hasRef ? `
                <button class="tts-play-btn tts-btn-ref" onclick="ttsReviewPlaySeg(${idx}, 'ref')"
                        id="tts-ref-btn-${idx}" title="播放英文原声（参考音频）">
                    🎙️ 听原声
                </button>` : ''}
            </div>
        </div>`;
    });

    list.innerHTML = html;
}

// ============================================================
// 控制按钮
// ============================================================

function updateTtsReviewControls(isComplete) {
    const playAllBtn = document.getElementById('tts-review-play-all');
    const confirmBtn = document.getElementById('tts-review-confirm-btn');

    if (isComplete) {
        if (playAllBtn) playAllBtn.style.display = '';
        if (confirmBtn) {
            confirmBtn.style.display = '';
            confirmBtn.disabled = false;
            confirmBtn.querySelector('.btn-text').textContent = '确认无误，继续混音';
        }
    } else {
        // 合成中：隐藏确认按钮，隐藏播放按钮（或变灰）
        if (confirmBtn) {
            confirmBtn.style.display = 'none';
        }
        if (playAllBtn) {
            const completed = ttsReviewSegments.filter(s => s.tts_status === 'completed').length;
            if (completed > 0) {
                playAllBtn.style.display = '';
            } else {
                playAllBtn.style.display = 'none';
            }
        }
    }
}

// ============================================================
// 单句播放
// ============================================================

function ttsReviewPlaySeg(segIdx, mode) {
    const seg = ttsReviewSegments.find(s => s.seg_idx === segIdx);
    if (!seg) return;

    const url = mode === 'tts' ? seg.tts_url : seg.ref_audio_url;
    if (!url) {
        showToast('❌ 该音频尚未合成或不可用');
        return;
    }

    // 如果正在顺序播放，先停止
    if (ttsReviewPlayAllActive) {
        ttsReviewStop();
    }

    const audio = document.getElementById('tts-review-audio');
    if (!audio) return;

    // 清除之前的高亮
    clearTtsReviewHighlight();

    // 更新状态
    ttsReviewPlayMode = mode;
    ttsReviewPlayingIdx = segIdx;
    audio.src = url;
    audio.play().catch(err => {
        console.error('播放失败:', err);
        showToast('❌ 播放失败');
    });

    // 高亮当前
    highlightTtsReviewItem(segIdx, mode);
    updateTtsReviewInfo();
}

// ============================================================
// 顺序播放全部
// ============================================================

let ttsReviewPlayQueue = [];
let ttsReviewPlayQueueIdx = 0;

function ttsReviewPlayAll() {
    if (ttsReviewPlayAllActive) {
        ttsReviewStop();
        return;
    }

    // 构建播放队列（只包含已完成的 segment）
    ttsReviewPlayQueue = ttsReviewSegments.filter(
        s => s.tts_url && s.tts_status === 'completed'
    );

    if (ttsReviewPlayQueue.length === 0) {
        showToast('❌ 没有可播放的 TTS 音频');
        return;
    }

    ttsReviewPlayAllActive = true;
    ttsReviewPlayMode = 'tts';
    ttsReviewPlayQueueIdx = 0;

    // 切换按钮状态
    document.getElementById('tts-review-play-all').style.display = 'none';
    document.getElementById('tts-review-stop').style.display = '';

    // 开始播放第一个
    playNextInQueue();
}

function playNextInQueue() {
    if (!ttsReviewPlayAllActive || ttsReviewPlayQueueIdx >= ttsReviewPlayQueue.length) {
        ttsReviewPlayAllFinished();
        return;
    }

    const seg = ttsReviewPlayQueue[ttsReviewPlayQueueIdx];
    const audio = document.getElementById('tts-review-audio');
    if (!audio || !seg.tts_url) {
        ttsReviewPlayAllFinished();
        return;
    }

    clearTtsReviewHighlight();
    ttsReviewPlayingIdx = seg.seg_idx;
    audio.src = seg.tts_url;
    audio.play().catch(err => {
        console.error('播放失败:', err);
        ttsReviewPlayQueueIdx++;
        setTimeout(() => playNextInQueue(), 500);
    });

    highlightTtsReviewItem(seg.seg_idx, 'tts');
    scrollToTtsReviewItem(seg.seg_idx);
    updateTtsReviewInfo();
}

function onTtsReviewSegmentEnded() {
    if (ttsReviewPlayAllActive) {
        ttsReviewPlayQueueIdx++;
        // 句间加 300ms 停顿模拟最终效果
        setTimeout(() => playNextInQueue(), 300);
    } else {
        clearTtsReviewHighlight();
        ttsReviewPlayingIdx = -1;
        updateTtsReviewInfo();
    }
}

function ttsReviewPlayAllFinished() {
    ttsReviewPlayAllActive = false;
    ttsReviewPlayingIdx = -1;
    clearTtsReviewHighlight();
    document.getElementById('tts-review-play-all').style.display = '';
    document.getElementById('tts-review-stop').style.display = 'none';
    updateTtsReviewInfo();
}

function ttsReviewStop() {
    const audio = document.getElementById('tts-review-audio');
    if (audio) {
        audio.pause();
        audio.removeAttribute('src');
    }
    ttsReviewPlayAllActive = false;
    ttsReviewPlayQueue = [];
    ttsReviewPlayQueueIdx = 0;
    ttsReviewPlayingIdx = -1;
    clearTtsReviewHighlight();
    document.getElementById('tts-review-play-all').style.display = '';
    document.getElementById('tts-review-stop').style.display = 'none';
    updateTtsReviewInfo();
}

// ============================================================
// UI 辅助
// ============================================================

function highlightTtsReviewItem(segIdx, mode) {
    const item = document.getElementById(`tts-review-item-${segIdx}`);
    if (!item) return;
    item.classList.add('playing');
    if (mode === 'ref') item.classList.add('playing-ref');

    const ttsBtn = document.getElementById(`tts-play-btn-${segIdx}`);
    const refBtn = document.getElementById(`tts-ref-btn-${segIdx}`);
    if (mode === 'tts' && ttsBtn) ttsBtn.classList.add('active');
    if (mode === 'ref' && refBtn) refBtn.classList.add('active');
}

function clearTtsReviewHighlight() {
    document.querySelectorAll('.tts-review-item.playing').forEach(el => {
        el.classList.remove('playing', 'playing-ref');
    });
    document.querySelectorAll('.tts-play-btn.active').forEach(el => {
        el.classList.remove('active');
    });
}

function scrollToTtsReviewItem(segIdx) {
    const item = document.getElementById(`tts-review-item-${segIdx}`);
    if (item) {
        item.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
}

function updateTtsReviewInfo(data) {
    const info = document.getElementById('tts-review-info');
    if (!info) return;

    const total = ttsReviewSegments.length;
    const completed = ttsReviewSegments.filter(s => s.tts_status === 'completed').length;

    if (data) {
        if (data.task_status === 'processing') {
            info.textContent = `⏳ 合成中: ${completed} / ${total} 句已完成`;
            return;
        }
    }

    if (ttsReviewPlayAllActive) {
        info.textContent = `顺序播放: ${ttsReviewPlayQueueIdx + 1} / ${ttsReviewPlayQueue.length}`;
    } else if (ttsReviewPlayingIdx >= 0) {
        const modeLabel = ttsReviewPlayMode === 'ref' ? '原声' : 'TTS';
        info.textContent = `正在播放 #${ttsReviewPlayingIdx + 1} (${modeLabel})`;
    } else {
        info.textContent = `共 ${total} 句，${completed} 句已合成`;
    }
}

// ============================================================
// 确认 / 取消
// ============================================================

async function confirmTtsReview() {
    const btn = document.getElementById('tts-review-confirm-btn');
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '提交中...';

    // 停止播放和轮询
    ttsReviewStop();
    stopTtsReviewPolling();

    try {
        const resp = await fetch(`/api/task/${currentTaskId}/confirm_tts`, {
            method: 'POST',
        });
        const data = await resp.json();
        if (!resp.ok) {
            showToast('❌ ' + (data.error || '确认失败'));
            btn.disabled = false;
            btn.querySelector('.btn-text').textContent = '确认无误，继续混音';
            return;
        }

        showToast('✅ ' + (data.message || '已确认'));
        showSection('progress');
        resetProgressUI();
        startPolling();
    } catch (err) {
        showToast('❌ 网络错误: ' + err.message);
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '确认无误，继续混音';
    }
}

function cancelFromTtsReview() {
    ttsReviewStop();
    stopTtsReviewPolling();
    resetAll();
}
