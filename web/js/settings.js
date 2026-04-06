/* ============================================================
   BiliMix — 设置 / 历史 / 页面管理 / 初始化模块
   依赖: state.js, utils.js, task.js, podcast.js
   ============================================================ */

// ============================================================
// Section Management
// ============================================================

function showSection(section) {
    const hero = document.getElementById('hero-section');
    const progress = document.getElementById('progress-section');
    const confirm = document.getElementById('confirm-section');
    const sentenceConfirm = document.getElementById('sentence-confirm-section');
    const result = document.getElementById('result-section');

    hero.style.display = 'none';
    progress.classList.add('hidden');
    confirm.classList.add('hidden');
    sentenceConfirm.classList.add('hidden');
    result.classList.add('hidden');

    if (section === 'progress') progress.classList.remove('hidden');
    else if (section === 'confirm') confirm.classList.remove('hidden');
    else if (section === 'sentence-confirm') sentenceConfirm.classList.remove('hidden');
    else if (section === 'result') result.classList.remove('hidden');
    else hero.style.display = '';

    // 结果页显示"下一集"按钮
    if (section === 'result') {
        const btn = document.getElementById('btn-next-episode');
        if (btn) btn.style.display = lastPodcastRssUrl ? 'inline-flex' : 'none';
    }

    // 切换到结果页时隐藏 Mini Player（因为结果页有完整播放器）
    if (section === 'result') {
        const mp = document.getElementById('mini-player');
        if (mp) {
            mp.style.display = 'none';
            document.body.classList.remove('mini-player-active');
        }
    }

    // 返回首页时刷新快捷面板
    if (section === 'hero') {
        loadQuickPanel();
        loadSavedSubscriptions();
    }
}

function resetProgressUI() {
    document.getElementById('progress-bar').style.width = '0%';
    document.getElementById('progress-pct').textContent = '0%';
    document.getElementById('progress-pct').style.color = '';
    document.getElementById('progress-message').textContent = '准备开始...';
    document.getElementById('progress-message').style.color = '';
    document.getElementById('progress-status-text').textContent = '处理中...';
    document.getElementById('progress-spinner').className = 'spinner';

    document.getElementById('cancel-area').innerHTML = `
        <button class="btn-cancel" id="cancel-btn" onclick="cancelTask()">
            <span class="btn-icon">⏹</span>
            <span class="btn-text">终止任务</span>
        </button>
    `;

    document.querySelectorAll('.steps-indicator .step-item').forEach(el => el.classList.remove('active', 'done'));
    document.querySelectorAll('.steps-indicator .step-line').forEach(el => el.classList.remove('done'));

    const wordSteps = document.getElementById('steps-word-replace');
    const sentSteps = document.getElementById('steps-sentence-translate');
    if (currentProcessMode === 'sentence_translate') {
        if (wordSteps) wordSteps.classList.add('hidden');
        if (sentSteps) sentSteps.classList.remove('hidden');
    } else {
        if (wordSteps) wordSteps.classList.remove('hidden');
        if (sentSteps) sentSteps.classList.add('hidden');
    }
}

function resetAll() {
    stopPolling();

    // 清理 Mini Player
    if (miniPlayerState) {
        removeMiniPlayerListeners();
        document.getElementById('mini-player').style.display = 'none';
        document.body.classList.remove('mini-player-active');
        miniPlayerState = null;
    }

    currentTaskId = null;
    currentTaskTitle = '';
    tasks_url = '';
    currentProcessMode = 'word_replace';
    selectedEpisodeUrl = '';
    selectedEpisodeTitle = '';
    rssSelectedEpisodeUrl = '';
    rssSelectedEpisodeTitle = '';

    if (isFullscreen) toggleTranscriptFullscreen();

    closePodcastResults();
    const episodesPanel = document.getElementById('episodes-panel');
    if (episodesPanel) episodesPanel.style.display = 'none';
    const selectedEp = document.getElementById('selected-episode');
    if (selectedEp) selectedEp.style.display = 'none';
    const rssEpisodesPanel = document.getElementById('rss-episodes-panel');
    if (rssEpisodesPanel) rssEpisodesPanel.style.display = 'none';
    const rssSelectedEp = document.getElementById('rss-selected-episode');
    if (rssSelectedEp) rssSelectedEp.style.display = 'none';
    const searchInput = document.getElementById('podcast-search-input');
    if (searchInput) searchInput.value = '';
    const rssInput = document.getElementById('rss-url-input');
    if (rssInput) rssInput.value = '';
    switchInputMode('search');

    segmentsData = [];
    activeSegmentIndex = -1;
    timeMappingData = [];
    fullscreenAudioSource = 'original';
    autoScrollEnabled = true;
    wordLevels = {};
    levelNums = {};
    freqHighlightEnabled = false;
    freqFilterApplied = false;
    freqFilterAddedWords = [];
    originalConfirmWords = [];
    confirmWords = [];
    confirmSegments = [];

    sentenceTranslations = {};
    sentenceTranslatedIndices = [];
    sentenceSegments = [];

    ['auto-scroll-btn', 'fs-auto-scroll-btn'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.classList.add('active');
    });

    const originalAudio = document.getElementById('original-audio');
    if (originalAudio) originalAudio.removeEventListener('timeupdate', onOriginalAudioTimeUpdate);
    const mixedAudio = document.getElementById('mixed-audio');
    if (mixedAudio) mixedAudio.removeEventListener('timeupdate', onMixedAudioTimeUpdate);

    const btn = document.getElementById('generate-btn');
    btn.disabled = false;
    btn.querySelector('.btn-text').textContent = '开始生成';

    resetProgressUI();
    switchTab('transcript');
    showSection('hero');

    const modeSelect = document.getElementById('mode-select');
    if (modeSelect) modeSelect.value = 'word_replace';
    const modeHint = document.getElementById('mode-hint');
    if (modeHint) modeHint.style.display = 'none';
    const diffGroup = document.querySelector('.input-options .option-group:first-child');
    if (diffGroup) {
        diffGroup.style.opacity = '1';
        diffGroup.style.pointerEvents = '';
    }
}

// ============================================================
// Tab Switching
// ============================================================

function switchTab(tabName) {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.id === 'tab-' + tabName);
    });
}

// ============================================================
// Vocabulary Drawer (全局生词库)
// ============================================================

function toggleVocabulary() {
    const overlay = document.getElementById('vocab-overlay');
    const drawer = document.getElementById('vocab-drawer');
    if (drawer.classList.contains('open')) {
        closeVocabulary();
    } else {
        overlay.classList.add('open');
        drawer.classList.add('open');
        vocabCurrentPage = 1;
        loadVocabularyStats();
        loadVocabulary();
    }
}

function closeVocabulary() {
    document.getElementById('vocab-overlay').classList.remove('open');
    document.getElementById('vocab-drawer').classList.remove('open');
}

async function loadVocabularyStats() {
    try {
        const resp = await fetch('/api/vocabulary/stats');
        const stats = await resp.json();
        document.getElementById('vocab-stat-total').textContent = stats.total || 0;
        document.getElementById('vocab-stat-unmastered').textContent = stats.unmastered || 0;
        document.getElementById('vocab-stat-mastered').textContent = stats.mastered || 0;
        document.getElementById('vocab-stat-recent').textContent = stats.recent_7days || 0;
    } catch (e) {
        console.error('加载生词库统计失败:', e);
    }
}

async function loadVocabulary() {
    const list = document.getElementById('vocab-list');
    list.innerHTML = '<div class="history-empty"><div class="spinner" style="margin:0 auto 12px;"></div><p>加载中...</p></div>';

    const sortBy = document.getElementById('vocab-sort-by').value;
    const filterMastered = document.getElementById('vocab-filter-mastered').value;
    const filterType = document.getElementById('vocab-filter-type').value;
    const search = document.getElementById('vocab-search-input').value.trim();

    const params = new URLSearchParams({
        sort_by: sortBy,
        sort_order: sortBy === 'english' ? 'asc' : 'desc',
        filter_mastered: filterMastered,
        filter_type: filterType,
        search: search,
        page: vocabCurrentPage,
        page_size: 50,
    });

    try {
        const resp = await fetch('/api/vocabulary?' + params);
        const data = await resp.json();
        const words = data.words || [];
        const total = data.total || 0;
        const pageSize = data.page_size || 50;
        vocabTotalPages = Math.max(1, Math.ceil(total / pageSize));

        if (words.length === 0) {
            list.innerHTML = '<div class="history-empty"><p>暂无匹配的生词</p></div>';
            document.getElementById('vocab-pagination').style.display = 'none';
            return;
        }

        let html = '';
        for (const w of words) {
            const masteredClass = w.mastered ? 'vocab-mastered' : '';
            const masteredIcon = w.mastered ? '✅' : '⬜';
            const masteredTitle = w.mastered ? '已掌握，点击标为未掌握' : '未掌握，点击标为已掌握';
            const typeLabel = getVocabTypeLabel(w.type);
            const freqBadge = w.frequency_level
                ? `<span class="vocab-freq-badge">${w.frequency_level}</span>`
                : '';
            const encounterBadge = w.encounter_count > 1
                ? `<span class="vocab-encounter-badge" title="遇到 ${w.encounter_count} 次">×${w.encounter_count}</span>`
                : '';

            html += `
            <div class="vocab-card ${masteredClass}" id="vocab-card-${w.id}">
                <div class="vocab-card-main">
                    <div class="vocab-card-left">
                        <span class="vocab-english">${escapeHtml(w.english)}</span>
                        <span class="vocab-chinese">${escapeHtml(w.chinese)}</span>
                    </div>
                    <div class="vocab-card-right">
                        <div class="vocab-badges">
                            <span class="vocab-type-badge vocab-type-${w.type}">${typeLabel}</span>
                            ${freqBadge}
                            ${encounterBadge}
                        </div>
                        <div class="vocab-actions">
                            <button class="vocab-action-btn" onclick="toggleVocabMastered(${w.id})"
                                    title="${masteredTitle}">${masteredIcon}</button>
                            <button class="vocab-action-btn vocab-delete-btn" onclick="deleteVocabWord(${w.id})"
                                    title="删除">🗑️</button>
                        </div>
                    </div>
                </div>
            </div>`;
        }
        list.innerHTML = html;

        // 分页
        const pagination = document.getElementById('vocab-pagination');
        if (vocabTotalPages > 1) {
            pagination.style.display = 'flex';
            document.getElementById('vocab-page-info').textContent = `${vocabCurrentPage} / ${vocabTotalPages}`;
            document.getElementById('vocab-prev-btn').disabled = vocabCurrentPage <= 1;
            document.getElementById('vocab-next-btn').disabled = vocabCurrentPage >= vocabTotalPages;
        } else {
            pagination.style.display = 'none';
        }
    } catch (e) {
        console.error('加载生词库失败:', e);
        list.innerHTML = '<div class="history-empty"><p>加载失败，请稍后重试</p></div>';
    }
}

function getVocabTypeLabel(type) {
    const map = { word: '单词', phrase: '短语', idiom: '习语', collocation: '搭配' };
    return map[type] || type || '单词';
}

function onVocabSearchInput() {
    clearTimeout(vocabSearchTimer);
    vocabSearchTimer = setTimeout(() => {
        vocabCurrentPage = 1;
        loadVocabulary();
    }, 400);
}

function vocabPrevPage() {
    if (vocabCurrentPage > 1) {
        vocabCurrentPage--;
        loadVocabulary();
    }
}

function vocabNextPage() {
    if (vocabCurrentPage < vocabTotalPages) {
        vocabCurrentPage++;
        loadVocabulary();
    }
}

async function toggleVocabMastered(vocabId) {
    try {
        const resp = await fetch(`/api/vocabulary/${vocabId}/toggle_mastered`, { method: 'POST' });
        const data = await resp.json();
        if (data.ok && data.word) {
            const card = document.getElementById(`vocab-card-${vocabId}`);
            if (card) {
                if (data.word.mastered) {
                    card.classList.add('vocab-mastered');
                } else {
                    card.classList.remove('vocab-mastered');
                }
                // 更新按钮
                const btn = card.querySelector('.vocab-action-btn');
                if (btn) {
                    btn.textContent = data.word.mastered ? '✅' : '⬜';
                    btn.title = data.word.mastered ? '已掌握，点击标为未掌握' : '未掌握，点击标为已掌握';
                }
            }
            loadVocabularyStats();
        }
    } catch (e) {
        console.error('切换掌握状态失败:', e);
    }
}

async function deleteVocabWord(vocabId) {
    if (!confirm('确定要删除这个生词吗？')) return;
    try {
        await fetch(`/api/vocabulary/${vocabId}`, { method: 'DELETE' });
        const card = document.getElementById(`vocab-card-${vocabId}`);
        if (card) {
            card.style.transition = 'opacity 0.3s, transform 0.3s';
            card.style.opacity = '0';
            card.style.transform = 'translateX(20px)';
            setTimeout(() => card.remove(), 300);
        }
        loadVocabularyStats();
    } catch (e) {
        console.error('删除生词失败:', e);
    }
}


// ============================================================
// History Drawer
// ============================================================

function toggleHistory() {
    const overlay = document.getElementById('history-overlay');
    const drawer = document.getElementById('history-drawer');
    if (drawer.classList.contains('open')) {
        closeHistory();
    } else {
        overlay.classList.add('open');
        drawer.classList.add('open');
        loadHistory();
    }
}

function closeHistory() {
    document.getElementById('history-overlay').classList.remove('open');
    document.getElementById('history-drawer').classList.remove('open');
    stopHistoryAudio();
}

function stopHistoryAudio() {
    const audio = document.getElementById('history-audio');
    if (audio) {
        audio.pause();
        audio.removeAttribute('src');
        audio.load();
    }
    if (historyPlayingTaskId) {
        const btn = document.getElementById(`history-play-${historyPlayingTaskId}`);
        if (btn) { btn.textContent = '▶'; btn.title = '播放原始音频'; }
    }
    historyPlayingTaskId = null;
}

function toggleHistoryPlay(taskId, basename, audioUrl) {
    const audio = document.getElementById('history-audio');
    const btn = document.getElementById(`history-play-${taskId}`);
    if (!audio || !btn) return;

    if (historyPlayingTaskId === taskId && !audio.paused) {
        audio.pause(); btn.textContent = '▶'; btn.title = '播放原始音频'; return;
    }
    if (historyPlayingTaskId === taskId && audio.paused && audio.src) {
        audio.play().catch(() => {}); btn.textContent = '⏸'; btn.title = '暂停'; return;
    }

    stopHistoryAudio();

    let ext = '.mp3';
    if (audioUrl) {
        const m = audioUrl.match(/\.(mp3|wav|m4a|ogg|flac|aac)(\?|$)/i);
        if (m) ext = '.' + m[1].toLowerCase();
    }
    const src = `/api/audio/${basename}${ext}`;

    audio.src = src;
    audio.play().catch(err => { console.error('历史音频播放失败:', err); btn.textContent = '▶'; });

    historyPlayingTaskId = taskId;
    btn.textContent = '⏸'; btn.title = '暂停';

    audio.onended = () => { btn.textContent = '▶'; btn.title = '播放原始音频'; historyPlayingTaskId = null; };
    audio.onerror = () => { btn.textContent = '▶'; btn.title = '播放原始音频'; historyPlayingTaskId = null; };
}

async function loadHistory() {
    const list = document.getElementById('history-list');
    list.innerHTML = '<div class="history-empty"><div class="spinner" style="margin: 0 auto 12px;"></div><p>加载中...</p></div>';

    try {
        const resp = await fetch('/api/tasks');
        const data = await resp.json();
        const tasks = data.tasks || [];

        if (tasks.length === 0) {
            list.innerHTML = '<div class="history-empty"><p>暂无历史任务</p></div>';
            return;
        }

        let html = '';
        tasks.forEach(t => {
            const statusMap = {
                'completed': '已完成', 'processing': '处理中', 'downloading': '下载中',
                'awaiting_confirmation': '待确认', 'awaiting_sentence_confirmation': '待确认翻译',
                'error': '出错', 'cancelled': '已终止',
            };
            const statusLabel = statusMap[t.status] || t.status;
            const statusClass = t.status || 'processing';

            let displayUrl = t.url || '--';
            try {
                const u = new URL(displayUrl);
                displayUrl = u.hostname + u.pathname.substring(u.pathname.lastIndexOf('/'));
            } catch(e) {}

            // 优先使用任务标题（播客单集名称），fallback 到 URL 截取
            const displayName = t.title || displayUrl;

            let statsHtml = '';
            if (t.status === 'completed' && (t.total_words || t.mixed_duration)) {
                statsHtml = `
                    <div class="history-card-stats">
                        ${t.total_words ? `<span class="history-stat"><strong>${t.total_words}</strong> 生词</span>` : ''}
                        ${t.total_replacements ? `<span class="history-stat"><strong>${t.total_replacements}</strong> 处替换</span>` : ''}
                        ${t.mixed_duration ? `<span class="history-stat"><strong>${t.mixed_duration}</strong>s 混合音频</span>` : ''}
                    </div>
                `;
            }

            html += `
                <div class="history-card" onclick="viewHistoryTask('${t.task_id}', '${escapeAttr(t.url)}')" title="${escapeAttr(displayName)}">
                    <div class="history-card-header">
                        <div class="history-card-url">${escapeHtml(displayName)}</div>
                        <div class="history-card-actions">
                            ${t.basename ? `
                            <button class="history-play-btn" id="history-play-${t.task_id}"
                                onclick="event.stopPropagation(); toggleHistoryPlay('${t.task_id}', '${escapeAttr(t.basename)}', '${escapeAttr(t.url)}')"
                                title="播放原始音频">▶</button>
                            ` : ''}
                            <button class="history-delete-btn" onclick="event.stopPropagation(); confirmDeleteTask('${t.task_id}', '${escapeAttr(displayName)}')" title="删除任务">
                                🗑
                            </button>
                        </div>
                    </div>
                    <div class="history-card-meta">
                        <span class="history-status ${statusClass}">${statusLabel}</span>
                        <span class="history-card-info">${t.difficulty || ''}</span>
                        <span class="history-card-info">${t.created_at || ''}</span>
                    </div>
                    ${statsHtml}
                </div>
            `;
        });

        list.innerHTML = html;
    } catch (err) {
        list.innerHTML = '<div class="history-empty"><p>加载失败: ' + err.message + '</p></div>';
    }
}

async function viewHistoryTask(taskId, url) {
    closeHistory();

    // 如果有 Mini Player 在播放，先关闭
    if (miniPlayerState) {
        miniPlayerClose();
    }

    currentTaskId = taskId;
    tasks_url = url || '';

    try {
        const resp = await fetch(`/api/task/${taskId}`);
        const data = await resp.json();

        // 保存任务标题（从历史记录进入时也能获取正确标题）
        if (data.title) {
            currentTaskTitle = data.title;
        }

        if (data.status === 'completed') {
            await loadResult();
        } else if (data.status === 'awaiting_confirmation') {
            await loadConfirmationUI();
        } else if (data.status === 'awaiting_sentence_confirmation') {
            await loadSentenceConfirmationUI();
        } else if (data.status === 'processing' || data.status === 'downloading') {
            showSection('progress');
            resetProgressUI();
            startPolling();
        } else {
            showSection('progress');
            updateProgress(data);
            if (data.status === 'cancelled') showCancelledUI();
            else { showError(data.message); showCancelledUI(); }
        }
    } catch (err) {
        alert('加载任务失败: ' + err.message);
    }
}

// ============================================================
// Delete Task Confirmation
// ============================================================

function confirmDeleteTask(taskId, displayUrl) {
    pendingDeleteTaskId = taskId;

    let overlay = document.getElementById('confirm-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'confirm-overlay';
        overlay.className = 'confirm-overlay';
        overlay.innerHTML = `
            <div class="confirm-dialog">
                <div class="confirm-icon">🗑️</div>
                <div class="confirm-title">确认删除</div>
                <div class="confirm-message" id="confirm-message"></div>
                <div class="confirm-actions">
                    <button class="confirm-cancel" onclick="closeConfirm()">取消</button>
                    <button class="confirm-delete" onclick="executeDelete()">确认删除</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
    }

    document.getElementById('confirm-message').textContent =
        `将删除该任务及其所有关联文件（音频、转录、结果等），此操作不可恢复。`;
    setTimeout(() => overlay.classList.add('open'), 10);
}

function closeConfirm() {
    const overlay = document.getElementById('confirm-overlay');
    if (overlay) overlay.classList.remove('open');
    pendingDeleteTaskId = null;
}

async function executeDelete() {
    if (!pendingDeleteTaskId) return;
    const taskId = pendingDeleteTaskId;
    closeConfirm();

    try {
        const resp = await fetch(`/api/task/${taskId}`, { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok) { alert(data.error || '删除失败'); return; }
        if (taskId === currentTaskId) resetAll();
        loadHistory();
    } catch (err) {
        alert('网络错误: ' + err.message);
    }
}

// ============================================================
// Settings Drawer
// ============================================================

function toggleSettings() {
    const overlay = document.getElementById('settings-overlay');
    const drawer = document.getElementById('settings-drawer');
    if (drawer.classList.contains('open')) {
        closeSettings();
    } else {
        overlay.classList.add('open');
        drawer.classList.add('open');
        loadSettings();
    }
}

function closeSettings() {
    document.getElementById('settings-overlay').classList.remove('open');
    document.getElementById('settings-drawer').classList.remove('open');
}

async function loadSettings() {
    const body = document.getElementById('settings-body');
    const footer = document.getElementById('settings-footer');
    body.innerHTML = '<div class="settings-loading"><div class="spinner" style="margin: 0 auto 12px;"></div><p>加载配置中...</p></div>';
    footer.style.display = 'none';

    try {
        const resp = await fetch('/api/config');
        settingsData = await resp.json();
        renderSettingsForm(settingsData);
        footer.style.display = '';
    } catch (err) {
        body.innerHTML = `<div class="settings-loading"><p>加载失败: ${err.message}</p></div>`;
    }
}

function renderSettingsForm(cfg) {
    const body = document.getElementById('settings-body');
    body.innerHTML = `
        <!-- 处理模式 -->
        <div class="settings-group">
            <div class="settings-group-title">🎯 处理模式</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">默认模式</span>
                    <span class="settings-item-desc">新任务默认使用的处理模式</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-process_mode">
                        <option value="word_replace" ${cfg.process_mode === 'word_replace' ? 'selected' : ''}>🔤 生词替换</option>
                        <option value="smart_translate" ${cfg.process_mode === 'smart_translate' ? 'selected' : ''}>🧠 智能翻译</option>
                        <option value="sentence_translate" ${cfg.process_mode === 'sentence_translate' ? 'selected' : ''}>🔄 句子翻译</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">难度等级</span>
                    <span class="settings-item-desc">生词识别的难度阈值</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-difficulty">
                        <option value="CET-4" ${cfg.difficulty === 'CET-4' ? 'selected' : ''}>CET-4 四级</option>
                        <option value="CET-6" ${cfg.difficulty === 'CET-6' ? 'selected' : ''}>CET-6 六级</option>
                        <option value="IELTS-6" ${cfg.difficulty === 'IELTS-6' ? 'selected' : ''}>雅思 6 分</option>
                        <option value="IELTS-7" ${cfg.difficulty === 'IELTS-7' ? 'selected' : ''}>雅思 7 分</option>
                        <option value="ADVANCED" ${cfg.difficulty === 'ADVANCED' ? 'selected' : ''}>高级</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">跳过确认</span>
                    <span class="settings-item-desc">自动跳过生词/翻译确认环节</span>
                </div>
                <div class="settings-item-control">
                    <label class="settings-toggle">
                        <input type="checkbox" id="cfg-skip_confirmation" ${cfg.skip_confirmation ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                </div>
            </div>
        </div>

        <!-- 智能翻译 -->
        <div class="settings-group">
            <div class="settings-group-title">🧠 智能翻译</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">最大翻译占比</span>
                    <span class="settings-item-desc">含生词句子超此比例时按密度截断</span>
                </div>
                <div class="settings-item-control">
                    <div class="settings-range-wrap">
                        <input type="range" class="settings-range" id="cfg-smart_max_translate_ratio"
                            min="0.1" max="1.0" step="0.05" value="${cfg.smart_max_translate_ratio}"
                            oninput="document.getElementById('val-smart_ratio').textContent = Math.round(this.value*100)+'%'">
                        <span class="settings-range-val" id="val-smart_ratio">${Math.round(cfg.smart_max_translate_ratio * 100)}%</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- 句子翻译 -->
        <div class="settings-group">
            <div class="settings-group-title">🔄 句子翻译</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">翻译比例</span>
                    <span class="settings-item-desc">均匀选句的替换比例</span>
                </div>
                <div class="settings-item-control">
                    <div class="settings-range-wrap">
                        <input type="range" class="settings-range" id="cfg-sentence_cn_ratio"
                            min="0.1" max="1.0" step="0.05" value="${cfg.sentence_cn_ratio}"
                            oninput="document.getElementById('val-cn_ratio').textContent = Math.round(this.value*100)+'%'">
                        <span class="settings-range-val" id="val-cn_ratio">${Math.round(cfg.sentence_cn_ratio * 100)}%</span>
                    </div>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">句间静音</span>
                    <span class="settings-item-desc">中英交替之间的间隔（毫秒）</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-sentence_gap_ms"
                        value="${cfg.sentence_gap_ms}" min="0" max="2000" step="50">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">克隆原声</span>
                    <span class="settings-item-desc">翻译 TTS 是否克隆原音频说话人声音</span>
                </div>
                <div class="settings-item-control">
                    <label class="settings-toggle">
                        <input type="checkbox" id="cfg-sentence_tts_voice_clone" ${cfg.sentence_tts_voice_clone ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                </div>
            </div>
        </div>

        <!-- TTS -->
        <div class="settings-group">
            <div class="settings-group-title">🔊 TTS 语音合成</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">TTS 引擎</span>
                    <span class="settings-item-desc">edge-tts 在线合成 / qwen3-tts 本地克隆</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-tts_engine">
                        <option value="edge-tts" ${cfg.tts_engine === 'edge-tts' ? 'selected' : ''}>Edge-TTS</option>
                        <option value="qwen3-tts" ${cfg.tts_engine === 'qwen3-tts' ? 'selected' : ''}>Qwen3-TTS</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">Edge-TTS 声音</span>
                    <span class="settings-item-desc">中文语音角色选择</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-tts_voice">
                        <option value="zh-CN-XiaoxiaoNeural" ${cfg.tts_voice === 'zh-CN-XiaoxiaoNeural' ? 'selected' : ''}>晓晓（活泼女声）</option>
                        <option value="zh-CN-XiaoyiNeural" ${cfg.tts_voice === 'zh-CN-XiaoyiNeural' ? 'selected' : ''}>晓伊（温暖女声）</option>
                        <option value="zh-CN-YunxiNeural" ${cfg.tts_voice === 'zh-CN-YunxiNeural' ? 'selected' : ''}>云希（阳光男声）</option>
                        <option value="zh-CN-YunjianNeural" ${cfg.tts_voice === 'zh-CN-YunjianNeural' ? 'selected' : ''}>云健（沉稳男声）</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">语速调整</span>
                    <span class="settings-item-desc">Edge-TTS 语速偏移量</span>
                </div>
                <div class="settings-item-control">
                    <input type="text" class="settings-input" id="cfg-tts_rate"
                        value="${cfg.tts_rate}" placeholder="+8%">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">合成文本格式</span>
                    <span class="settings-item-desc">mixed: 英+中混合 / chinese_only: 纯中文</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-tts_text_format">
                        <option value="chinese_only" ${cfg.tts_text_format === 'chinese_only' ? 'selected' : ''}>纯中文</option>
                        <option value="mixed" ${cfg.tts_text_format === 'mixed' ? 'selected' : ''}>英+中混合</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">Qwen3-TTS 设备</span>
                    <span class="settings-item-desc">本地 TTS 推理设备</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-qwen3_tts_device">
                        <option value="cpu" ${cfg.qwen3_tts_device === 'cpu' ? 'selected' : ''}>CPU</option>
                        <option value="cuda:0" ${cfg.qwen3_tts_device === 'cuda:0' ? 'selected' : ''}>CUDA:0</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">参考音频时长</span>
                    <span class="settings-item-desc">声音克隆参考片段长度（秒）</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-qwen3_tts_ref_duration"
                        value="${cfg.qwen3_tts_ref_duration}" min="3" max="30" step="1">
                </div>
            </div>
        </div>

        <!-- WhisperX -->
        <div class="settings-group">
            <div class="settings-group-title">🎙️ WhisperX 转录</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">模型大小</span>
                    <span class="settings-item-desc">越大越精准但越慢</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-whisperx_model">
                        <option value="tiny" ${cfg.whisperx_model === 'tiny' ? 'selected' : ''}>tiny</option>
                        <option value="base" ${cfg.whisperx_model === 'base' ? 'selected' : ''}>base</option>
                        <option value="small" ${cfg.whisperx_model === 'small' ? 'selected' : ''}>small</option>
                        <option value="medium" ${cfg.whisperx_model === 'medium' ? 'selected' : ''}>medium</option>
                        <option value="large-v2" ${cfg.whisperx_model === 'large-v2' ? 'selected' : ''}>large-v2</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">推理设备</span>
                    <span class="settings-item-desc">WhisperX 运行设备</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-whisperx_device">
                        <option value="cpu" ${cfg.whisperx_device === 'cpu' ? 'selected' : ''}>CPU</option>
                        <option value="cuda" ${cfg.whisperx_device === 'cuda' ? 'selected' : ''}>CUDA</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">语言</span>
                    <span class="settings-item-desc">音频语言</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-whisperx_language">
                        <option value="en" ${cfg.whisperx_language === 'en' ? 'selected' : ''}>English</option>
                        <option value="zh" ${cfg.whisperx_language === 'zh' ? 'selected' : ''}>中文</option>
                        <option value="ja" ${cfg.whisperx_language === 'ja' ? 'selected' : ''}>日本語</option>
                    </select>
                </div>
            </div>
        </div>

        <!-- Ollama -->
        <div class="settings-group">
            <div class="settings-group-title">🤖 Ollama LLM</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">API 地址</span>
                    <span class="settings-item-desc">Ollama 服务端地址</span>
                </div>
                <div class="settings-item-control">
                    <input type="text" class="settings-input" id="cfg-ollama_base_url"
                        value="${cfg.ollama_base_url}" style="width:180px;">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">模型名称</span>
                    <span class="settings-item-desc">用于生词识别和翻译的模型</span>
                </div>
                <div class="settings-item-control">
                    <input type="text" class="settings-input" id="cfg-ollama_model"
                        value="${cfg.ollama_model}" style="width:150px;">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">批处理大小</span>
                    <span class="settings-item-desc">每批合并的句子数量 (5~15)</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-llm_batch_size"
                        value="${cfg.llm_batch_size}" min="1" max="30" step="1">
                </div>
            </div>
        </div>

        <!-- 音频输出 -->
        <div class="settings-group">
            <div class="settings-group-title">💿 音频输出</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">输出格式</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-output_format">
                        <option value="mp3" ${cfg.output_format === 'mp3' ? 'selected' : ''}>MP3</option>
                        <option value="wav" ${cfg.output_format === 'wav' ? 'selected' : ''}>WAV</option>
                        <option value="ogg" ${cfg.output_format === 'ogg' ? 'selected' : ''}>OGG</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">比特率</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-output_bitrate">
                        <option value="128k" ${cfg.output_bitrate === '128k' ? 'selected' : ''}>128k</option>
                        <option value="192k" ${cfg.output_bitrate === '192k' ? 'selected' : ''}>192k</option>
                        <option value="256k" ${cfg.output_bitrate === '256k' ? 'selected' : ''}>256k</option>
                        <option value="320k" ${cfg.output_bitrate === '320k' ? 'selected' : ''}>320k</option>
                    </select>
                </div>
            </div>
        </div>

        <!-- 登录认证 -->
        <div class="settings-group">
            <div class="settings-group-title">🔐 登录认证</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">启用登录</span>
                    <span class="settings-item-desc">启用后需要登录才能访问</span>
                </div>
                <div class="settings-item-control">
                    <label class="settings-toggle">
                        <input type="checkbox" id="cfg-auth_enabled" ${cfg.auth_enabled ? 'checked' : ''}>
                        <span class="settings-toggle-slider"></span>
                    </label>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">用户名</span>
                </div>
                <div class="settings-item-control">
                    <input type="text" class="settings-input" id="cfg-auth_username"
                           value="${escapeAttr(cfg.auth_username || 'admin')}"
                           placeholder="admin" autocomplete="off">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">密码</span>
                </div>
                <div class="settings-item-control">
                    <input type="text" class="settings-input" id="cfg-auth_password"
                           value="${escapeAttr(cfg.auth_password || '')}"
                           placeholder="请设置密码" autocomplete="off">
                </div>
            </div>
        </div>
    `;
}

async function saveSettings() {
    const btn = document.getElementById('settings-save-btn');
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '保存中...';

    const getValue = (id) => {
        const el = document.getElementById(id);
        if (!el) return undefined;
        if (el.type === 'checkbox') return el.checked;
        if (el.type === 'range' || el.type === 'number') return parseFloat(el.value);
        return el.value;
    };

    const payload = {
        process_mode: getValue('cfg-process_mode'),
        difficulty: getValue('cfg-difficulty'),
        skip_confirmation: getValue('cfg-skip_confirmation'),
        smart_max_translate_ratio: getValue('cfg-smart_max_translate_ratio'),
        sentence_cn_ratio: getValue('cfg-sentence_cn_ratio'),
        sentence_gap_ms: getValue('cfg-sentence_gap_ms'),
        sentence_tts_voice_clone: getValue('cfg-sentence_tts_voice_clone'),
        tts_engine: getValue('cfg-tts_engine'),
        tts_voice: getValue('cfg-tts_voice'),
        tts_rate: getValue('cfg-tts_rate'),
        tts_text_format: getValue('cfg-tts_text_format'),
        whisperx_model: getValue('cfg-whisperx_model'),
        whisperx_device: getValue('cfg-whisperx_device'),
        whisperx_language: getValue('cfg-whisperx_language'),
        ollama_base_url: getValue('cfg-ollama_base_url'),
        ollama_model: getValue('cfg-ollama_model'),
        llm_batch_size: getValue('cfg-llm_batch_size'),
        output_format: getValue('cfg-output_format'),
        output_bitrate: getValue('cfg-output_bitrate'),
        qwen3_tts_device: getValue('cfg-qwen3_tts_device'),
        qwen3_tts_ref_duration: getValue('cfg-qwen3_tts_ref_duration'),
        auth_enabled: getValue('cfg-auth_enabled'),
        auth_username: getValue('cfg-auth_username'),
        auth_password: getValue('cfg-auth_password'),
    };

    try {
        const resp = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.ok || data.updated) {
            showToast(`✅ 已保存 ${data.updated?.length || 0} 项配置`);
            initPageConfig();
        } else {
            showToast('❌ ' + (data.error || '保存失败'));
        }
    } catch (err) {
        showToast('❌ 网络错误: ' + err.message);
    } finally {
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '保存设置';
    }
}

async function resetSettingsToDefault() {
    if (!confirm('确定恢复所有配置为默认值？')) return;
    await loadSettings();
    showToast('🔄 已恢复为当前服务端配置');
}

// ============================================================
// Go Home
// ============================================================

function goHome() {
    const heroSection = document.getElementById('hero-section');
    if (heroSection.style.display !== 'none') return;

    // 检查是否有音频正在播放
    const originalAudio = document.getElementById('original-audio');
    const mixedAudio = document.getElementById('mixed-audio');
    const resultSection = document.getElementById('result-section');
    const isOnResult = resultSection && !resultSection.classList.contains('hidden');
    const isPlaying = (originalAudio && !originalAudio.paused) || (mixedAudio && !mixedAudio.paused);

    if (isOnResult && isPlaying) {
        // 有音频播放 → 保存状态并激活 Mini Player
        activateMiniPlayer();
        showSection('hero');
        initPageConfig();
    } else {
        // 没有播放 → 常规 resetAll
        resetAll();
        initPageConfig();
    }
}

/**
 * 从结果页点击"处理新的音频"时调用。
 * 如果有音频在播，激活 Mini Player 保持后台播放。
 */
function goHomeFromResult() {
    const originalAudio = document.getElementById('original-audio');
    const mixedAudio = document.getElementById('mixed-audio');
    const isPlaying = (originalAudio && !originalAudio.paused) || (mixedAudio && !mixedAudio.paused);

    if (isPlaying) {
        activateMiniPlayer();
        showSection('hero');
        initPageConfig();
    } else {
        resetAll();
        initPageConfig();
    }
}

// ============================================================
// Keyboard Shortcuts
// ============================================================

document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        const urlInput = document.getElementById('audio-url');
        const searchInput = document.getElementById('podcast-search-input');
        if (document.activeElement === urlInput) {
            e.preventDefault();
            submitTask();
        } else if (document.activeElement === searchInput) {
            e.preventDefault();
            searchPodcasts();
        }
    }
    if (e.key === 'Escape') {
        if (isFullscreen) toggleTranscriptFullscreen();
        else { closeConfirm(); closeHistory(); closeSettings(); }
    }
});

// ============================================================
// Page Initialization
// ============================================================

async function initPageConfig() {
    try {
        const resp = await fetch('/api/config');
        const cfg = await resp.json();

        const modeSelect = document.getElementById('mode-select');
        if (modeSelect && cfg.process_mode) {
            modeSelect.value = cfg.process_mode;
            currentProcessMode = cfg.process_mode;
            onModeChange();
        }

        const diffSelect = document.getElementById('difficulty-select');
        if (diffSelect && cfg.difficulty) diffSelect.value = cfg.difficulty;

        const skipCheck = document.getElementById('skip-confirm-check');
        if (skipCheck && cfg.skip_confirmation !== undefined) skipCheck.checked = cfg.skip_confirmation;
    } catch (err) {
        console.warn('Failed to load config:', err);
    }
}

// ============================================================
// Mini Player
// ============================================================

/**
 * 激活 Mini Player：保存当前播放状态，显示底部播放条。
 * 调用时机：在结果页有音频播放时点击 goHome。
 */
function activateMiniPlayer() {
    const originalAudio = document.getElementById('original-audio');
    const mixedAudio = document.getElementById('mixed-audio');

    // 判断当前正在播放的是哪个音频
    let activeAudio = null;
    let audioType = 'original';
    if (mixedAudio && !mixedAudio.paused) {
        activeAudio = mixedAudio;
        audioType = 'mixed';
    } else if (originalAudio && !originalAudio.paused) {
        activeAudio = originalAudio;
        audioType = 'original';
    }

    if (!activeAudio) return;

    // 构造任务标题：优先使用真实播客标题，fallback 到 URL 截取
    let title = currentTaskTitle || selectedEpisodeTitle || rssSelectedEpisodeTitle || '';
    if (!title) {
        try {
            if (tasks_url) {
                const u = new URL(tasks_url);
                title = decodeURIComponent(u.pathname.substring(u.pathname.lastIndexOf('/') + 1)) || u.hostname;
            }
        } catch (e) {
            title = tasks_url ? tasks_url.substring(0, 60) : '';
        }
    }

    // 保存状态（segments/timeMapping 回来时由 loadResult 从服务端获取，无需保存）
    miniPlayerState = {
        taskId: currentTaskId,
        title: title || '未知音频',
        audioType: audioType,
        url: tasks_url,
        savedProcessMode: currentProcessMode,
    };

    // 显示 Mini Player
    const mp = document.getElementById('mini-player');
    mp.style.display = 'block';
    document.body.classList.add('mini-player-active');

    // 设置信息
    document.getElementById('mini-player-title').textContent = miniPlayerState.title;
    const badge = document.getElementById('mini-player-badge');
    if (audioType === 'mixed') {
        badge.textContent = '中英混合';
        badge.className = 'mini-player-badge badge-mixed';
    } else {
        badge.textContent = '原始音频';
        badge.className = 'mini-player-badge badge-original';
    }
    // SVG 图标：显示暂停图标
    document.getElementById('mini-player-icon-pause').style.display = '';
    document.getElementById('mini-player-icon-play').style.display = 'none';

    // 绑定 timeupdate 到 Mini Player 进度条
    activeAudio.addEventListener('timeupdate', onMiniPlayerTimeUpdate);
    activeAudio.addEventListener('ended', onMiniPlayerEnded);
    activeAudio.addEventListener('pause', onMiniPlayerPauseEvent);
    activeAudio.addEventListener('play', onMiniPlayerPlayEvent);

    // 立即更新一次进度
    updateMiniPlayerProgress(activeAudio);
}

function onMiniPlayerTimeUpdate(e) {
    updateMiniPlayerProgress(e.target);
}

function onMiniPlayerEnded() {
    document.getElementById('mini-player-icon-pause').style.display = 'none';
    document.getElementById('mini-player-icon-play').style.display = '';
}

function onMiniPlayerPauseEvent() {
    document.getElementById('mini-player-icon-pause').style.display = 'none';
    document.getElementById('mini-player-icon-play').style.display = '';
}

function onMiniPlayerPlayEvent() {
    document.getElementById('mini-player-icon-pause').style.display = '';
    document.getElementById('mini-player-icon-play').style.display = 'none';
}

function updateMiniPlayerProgress(audio) {
    if (!audio || !audio.duration) return;
    const pct = (audio.currentTime / audio.duration) * 100;
    const fill = document.getElementById('mini-player-progress-fill');
    if (fill) fill.style.width = pct + '%';

    const timeEl = document.getElementById('mini-player-time');
    if (timeEl) {
        timeEl.textContent = formatTime(audio.currentTime) + ' / ' + formatTime(audio.duration);
    }
}

/** Mini Player 播放/暂停切换 */
function miniPlayerTogglePlay() {
    if (!miniPlayerState) return;
    const audio = getActiveMiniPlayerAudio();
    if (!audio) return;

    if (audio.paused) {
        audio.play().catch(() => {});
    } else {
        audio.pause();
    }
}

/** 获取 Mini Player 对应的 <audio> 元素 */
function getActiveMiniPlayerAudio() {
    if (!miniPlayerState) return null;
    if (miniPlayerState.audioType === 'mixed') {
        return document.getElementById('mixed-audio');
    }
    return document.getElementById('original-audio');
}

/** 点击 Mini Player 主体 → 跳回结果详情页 */
async function miniPlayerGoBack() {
    if (!miniPlayerState) return;

    const audio = getActiveMiniPlayerAudio();

    // 先保存当前播放进度和状态
    const savedTime = audio ? audio.currentTime : 0;
    const wasPlaying = audio ? !audio.paused : false;

    // 先清理 Mini Player 的事件监听
    removeMiniPlayerListeners();

    // 恢复保存的状态
    currentTaskId = miniPlayerState.taskId;
    tasks_url = miniPlayerState.url;
    currentProcessMode = miniPlayerState.savedProcessMode;

    // 隐藏 Mini Player
    document.getElementById('mini-player').style.display = 'none';
    document.body.classList.remove('mini-player-active');
    miniPlayerState = null;

    // 通过 loadResult 重新渲染结果页（包含 showSection、setupAudioSync）
    // 注意：setAudioSrcIfChanged 会确保 src 相同时不重新加载，保留播放状态
    await loadResult();

    // loadResult 后确保播放进度恢复（以防 src 被重置）
    if (audio) {
        // 等待音频元素就绪后恢复
        if (audio.readyState >= 1) {
            // 元数据已加载，直接恢复
            audio.currentTime = savedTime;
            if (wasPlaying) audio.play().catch(() => {});
        } else if (audio.src) {
            // 需要等音频加载，监听 loadedmetadata
            const restorePlayback = () => {
                audio.currentTime = savedTime;
                if (wasPlaying) audio.play().catch(() => {});
                audio.removeEventListener('loadedmetadata', restorePlayback);
            };
            audio.addEventListener('loadedmetadata', restorePlayback);
        }
    }

    // 恢复高亮
    const currentTime = savedTime;
    let origTime = currentTime;
    if (audio && audio.id === 'mixed-audio') {
        origTime = mixedTimeToOriginalTime(currentTime);
    }
    const idx = findSegmentByOriginalTime(origTime);
    if (idx >= 0) {
        activeSegmentIndex = idx;
        highlightSegment(idx);
    }
}

/** 点击 Mini Player 关闭按钮 → 停止播放，销毁一切 */
function miniPlayerClose() {
    if (!miniPlayerState) return;

    const audio = getActiveMiniPlayerAudio();

    // 清理事件监听
    removeMiniPlayerListeners();

    // 停止播放
    if (audio) {
        audio.pause();
        audio.removeEventListener('timeupdate', onOriginalAudioTimeUpdate);
        audio.removeEventListener('timeupdate', onMixedAudioTimeUpdate);
    }

    // 隐藏 Mini Player
    document.getElementById('mini-player').style.display = 'none';
    document.body.classList.remove('mini-player-active');
    miniPlayerState = null;

    // 重置全部状态（不切换 section，因为已经在首页）
    currentTaskId = null;
    currentTaskTitle = '';
    tasks_url = '';
    segmentsData = [];
    activeSegmentIndex = -1;
    timeMappingData = [];
}

function removeMiniPlayerListeners() {
    const originalAudio = document.getElementById('original-audio');
    const mixedAudio = document.getElementById('mixed-audio');

    [originalAudio, mixedAudio].forEach(audio => {
        if (audio) {
            audio.removeEventListener('timeupdate', onMiniPlayerTimeUpdate);
            audio.removeEventListener('ended', onMiniPlayerEnded);
            audio.removeEventListener('pause', onMiniPlayerPauseEvent);
            audio.removeEventListener('play', onMiniPlayerPlayEvent);
        }
    });
}

/** Mini Player 进度条点击 seek */
function initMiniPlayerProgressSeek() {
    const bar = document.getElementById('mini-player-progress-bar');
    if (!bar) return;

    bar.addEventListener('click', (e) => {
        e.stopPropagation();
        const audio = getActiveMiniPlayerAudio();
        if (!audio || !audio.duration) return;

        const rect = bar.getBoundingClientRect();
        const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        audio.currentTime = pct * audio.duration;
    });
}

document.addEventListener('DOMContentLoaded', async () => {
    await initPageConfig();
    loadQuickPanel();
    loadSavedSubscriptions();
    initMiniPlayerProgressSeek();
});
