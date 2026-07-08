/* ============================================================
   BiliMix — 设置 / 历史 / 页面管理 / 初始化模块
   依赖: state.js, utils.js, task.js, podcast.js
   ============================================================ */

// ============================================================
// Section Management
// ============================================================

function showSection(section) {
    const progress = document.getElementById('progress-section');
    const result = document.getElementById('result-section');
    const confirmSec = document.getElementById('confirm-section');
    const sentConfirm = document.getElementById('sentence-confirm-section');

    // 隐藏进度/结果/确认区
    if (progress) progress.classList.add('hidden');
    if (result) result.classList.add('hidden');
    if (confirmSec) confirmSec.classList.add('hidden');
    if (sentConfirm) sentConfirm.classList.add('hidden');

    if (section === 'progress' && progress) {
        // 隐藏内容视图
        document.querySelectorAll('.content-view').forEach(v => v.style.display = 'none');
        progress.classList.remove('hidden');
    } else if (section === 'result' && result) {
        document.querySelectorAll('.content-view').forEach(v => v.style.display = 'none');
        result.classList.remove('hidden');
    } else if (section === 'confirm' && confirmSec) {
        document.querySelectorAll('.content-view').forEach(v => v.style.display = 'none');
        confirmSec.classList.remove('hidden');
    } else if (section === 'sentence-confirm' && sentConfirm) {
        document.querySelectorAll('.content-view').forEach(v => v.style.display = 'none');
        sentConfirm.classList.remove('hidden');
    } else {
        // 返回内容视图（更新/发现/任务）
        document.querySelectorAll('.content-view').forEach(v => {
            if (v.id !== 'updates-view') v.style.display = 'none';
        });
        const updatesView = document.getElementById('updates-view');
        if (updatesView) updatesView.style.display = '';
        // 刷新数据
        if (typeof loadEpisodes === 'function') loadEpisodes();
        if (typeof loadSidebarSubscriptions === 'function') loadSidebarSubscriptions();
    }

    // 切换到结果页时隐藏 Mini Player
    if (section === 'result') {
        const mp = document.getElementById('mini-player');
        if (mp) {
            mp.style.display = 'none';
            document.body.classList.remove('mini-player-active');
        }
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
    selectedEpisodeUrl = '';
    selectedEpisodeTitle = '';
    rssSelectedEpisodeUrl = '';
    rssSelectedEpisodeTitle = '';

    if (isFullscreen) toggleTranscriptFullscreen();

    if (typeof closePodcastResults === 'function') closePodcastResults();
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
    if (typeof switchInputMode === 'function') switchInputMode('url');

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
    if (btn) {
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '开始生成';
    }

    resetProgressUI();
    if (typeof switchTab === 'function') switchTab('transcript');
    showSection('hero');

    const modeSelect = document.getElementById('mode-select');
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
// History Drawer
// ============================================================

function toggleHistory() {
    const overlay = document.getElementById('history-overlay');
    const drawer = document.getElementById('history-drawer');
    if (!overlay || !drawer) {
        // 新布局：切换到任务页
        switchView('tasks');
        return;
    }
    if (drawer.classList.contains('open')) {
        closeHistory();
    } else {
        overlay.classList.add('open');
        drawer.classList.add('open');
        loadHistory();
    }
}

function closeHistory() {
    const overlay = document.getElementById('history-overlay');
    const drawer = document.getElementById('history-drawer');
    if (overlay) overlay.classList.remove('open');
    if (drawer) drawer.classList.remove('open');
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

// 任务展开状态
let taskExpandedId = null;
let taskPollInterval = null;

// 任务列表筛选/排序状态
let taskFilterStatus = 'all';       // all | processing | completed | error | cancelled
let taskFilterType = 'all';         // all | audio | video
let taskSearchQuery = '';
let taskSortField = 'created_at';   // created_at | title | status | duration
let taskSortDir = 'desc';           // asc | desc
let allTasksCache = [];             // 全部任务缓存

// ============================================================
// 任务列表 — 表格式视图 + 筛选 / 排序 / 搜索
// ============================================================

async function loadHistory() {
    const container = document.getElementById('tasks-list-container');
    if (!container) return;
    container.innerHTML = '<div class="history-empty"><div class="spinner" style="margin: 0 auto 12px;"></div><p>加载中...</p></div>';

    try {
        const resp = await fetch('/api/tasks?limit=50');
        const data = await resp.json();
        allTasksCache = data.tasks || [];
        renderTaskTable();
    } catch (err) {
        container.innerHTML = '<div class="history-empty"><p>加载失败: ' + err.message + '</p></div>';
    }
}

function getFilteredTasks() {
    let tasks = [...allTasksCache];

    // 状态筛选
    if (taskFilterStatus !== 'all') {
        tasks = tasks.filter(t => t.status === taskFilterStatus);
    }

    // 类型筛选
    if (taskFilterType !== 'all') {
        tasks = tasks.filter(t => {
            const pm = t.process_mode || '';
            if (taskFilterType === 'video') return pm === 'video';
            return pm !== 'video';
        });
    }

    // 搜索
    if (taskSearchQuery) {
        const q = taskSearchQuery.toLowerCase();
        tasks = tasks.filter(t =>
            (t.title || '').toLowerCase().includes(q) ||
            (t.url || '').toLowerCase().includes(q)
        );
    }

    // 排序
    tasks.sort((a, b) => {
        let va, vb;
        switch (taskSortField) {
            case 'title':
                va = (a.title || a.url || '').toLowerCase();
                vb = (b.title || b.url || '').toLowerCase();
                break;
            case 'status':
                const order = { processing: 0, downloading: 0, queued: 1, completed: 2, error: 3, cancelled: 4 };
                va = order[a.status] ?? 9; vb = order[b.status] ?? 9;
                break;
            case 'duration':
                va = a.original_duration || 0; vb = b.original_duration || 0;
                break;
            default: // created_at
                va = a.created_at || ''; vb = b.created_at || '';
        }
        if (va < vb) return taskSortDir === 'asc' ? -1 : 1;
        if (va > vb) return taskSortDir === 'asc' ? 1 : -1;
        return 0;
    });

    return tasks;
}

function renderTaskTable() {
    const container = document.getElementById('tasks-list-container');
    if (!container) return;

    const tasks = getFilteredTasks();

    // 统计
    const counts = { all: allTasksCache.length, processing: 0, completed: 0, error: 0, cancelled: 0 };
    allTasksCache.forEach(t => {
        if (t.status === 'processing' || t.status === 'downloading' || t.status === 'queued') counts.processing++;
        else if (t.status in counts) counts[t.status]++;
    });

    let html = '';

    // ---- 筛选栏 ----
    html += '<div class="task-filter-bar">';
    html += '<div class="task-filter-tabs">';
    const statuses = [
        { key: 'all', label: '全部', count: counts.all },
        { key: 'processing', label: '处理中', count: counts.processing },
        { key: 'completed', label: '已完成', count: counts.completed },
        { key: 'error', label: '出错', count: counts.error },
        { key: 'cancelled', label: '已终止', count: counts.cancelled },
    ];
    statuses.forEach(s => {
        const active = taskFilterStatus === s.key ? ' active' : '';
        html += `<button class="task-filter-btn${active}" onclick="taskFilterStatus='${s.key}';renderTaskTable()">${s.label}<span class="task-filter-count">${s.count}</span></button>`;
    });
    html += '</div>';

    html += '<div class="task-filter-right">';
    // 类型下拉
    html += '<select class="task-filter-select" onchange="taskFilterType=this.value;renderTaskTable()">';
    html += `<option value="all"${taskFilterType === 'all' ? ' selected' : ''}>全部类型</option>`;
    html += `<option value="audio"${taskFilterType === 'audio' ? ' selected' : ''}>音频转录</option>`;
    html += `<option value="video"${taskFilterType === 'video' ? ' selected' : ''}>视频配音</option>`;
    html += '</select>';
    // 搜索
    html += `<input type="text" class="task-filter-search" placeholder="搜索标题或URL..." value="${escapeAttr(taskSearchQuery)}" oninput="taskSearchQuery=this.value;renderTaskTable()">`;
    html += '</div>';
    html += '</div>';

    // ---- 空态 ----
    if (tasks.length === 0) {
        html += '<div class="task-table-empty">暂无匹配的任务</div>';
        container.innerHTML = html;
        return;
    }

    // ---- 表格 ----
    html += '<div class="task-table-wrap"><table class="task-table">';

    // 表头
    const cols = [
        { key: 'title', label: '名称', class: 'col-title' },
        { key: 'status', label: '状态', class: 'col-status' },
        { key: 'duration', label: '时长', class: 'col-dur' },
        { key: 'created_at', label: '创建时间', class: 'col-time' },
    ];
    html += '<thead><tr>';
    cols.forEach(c => {
        const active = taskSortField === c.key ? ` sorted-${taskSortDir}` : '';
        html += `<th class="${c.class}${active}" onclick="toggleTaskSort('${c.key}')">${c.label}<span class="sort-arrow"></span></th>`;
    });
    html += '<th class="col-actions"></th></tr></thead>';

    // 表体
    html += '<tbody>';
    tasks.forEach(t => {
        const status = t.status || '';
        const statusMap = {
            'processing': '处理中', 'downloading': '下载中', 'queued': '排队中',
            'completed': '已完成', 'error': '出错', 'cancelled': '已终止',
            'awaiting_confirmation': '待确认', 'awaiting_sentence_confirmation': '待确认翻译',
        };
        const statusLabel = statusMap[status] || status;
        const statusClass = status || 'processing';
        const isExpanded = taskExpandedId === t.task_id;

        // 名称+类型
        let nameHtml = '';
        const displayName = t.title || t.url || '--';
        const typeIcon = (t.process_mode === 'video') ? ' 🎬' : ' 🎵';
        const typeLabel = (t.process_mode === 'video') ? '<span class="task-type-badge video">视频</span>'
                       : '<span class="task-type-badge audio">音频</span>';
        nameHtml = `${typeLabel} <span class="task-name-text">${escapeHtml(displayName)}</span>`;

        // 时长
        let durHtml = '--';
        if (t.original_duration) durHtml = formatTime(t.original_duration);
        else if (t.status === 'completed' && t.mixed_duration) durHtml = formatTime(t.mixed_duration);

        // 时间
        let timeHtml = t.created_at || '--';
        if (timeHtml.length > 16) timeHtml = timeHtml.substring(0, 16);

        html += `<tr class="task-row ${statusClass} ${isExpanded ? 'expanded' : ''}" onclick="toggleTaskDetail('${t.task_id}', '${escapeAttr(t.url)}')">`;
        html += `<td class="col-title">${nameHtml}</td>`;
        html += `<td class="col-status"><span class="task-status-dot ${statusClass}"></span>${statusLabel}</td>`;
        html += `<td class="col-dur">${durHtml}</td>`;
        html += `<td class="col-time">${timeHtml}</td>`;
        html += `<td class="col-actions" onclick="event.stopPropagation()">`;

        if (t.basename) {
            html += `<button class="task-row-btn" onclick="event.stopPropagation(); toggleHistoryPlay('${t.task_id}', '${escapeAttr(t.basename)}', '${escapeAttr(t.url)}')" title="播放">▶</button>`;
        }
        html += `<button class="task-row-btn del" onclick="event.stopPropagation(); confirmDeleteTask('${t.task_id}', '${escapeAttr(displayName)}')" title="删除">✕</button>`;

        html += '</td></tr>';

        // 展开详情行
        html += `<tr class="task-detail-row" id="task-detail-row-${t.task_id}" style="${isExpanded ? '' : 'display:none'}">`;
        html += `<td colspan="5"><div class="task-detail-inner" id="task-detail-${t.task_id}">`;
        if (isExpanded) html += '<div class="task-detail-loading">加载中...</div>';
        html += '</div></td></tr>';
    });
    html += '</tbody></table></div>';

    container.innerHTML = html;
}

function toggleTaskSort(field) {
    if (taskSortField === field) {
        taskSortDir = taskSortDir === 'asc' ? 'desc' : 'asc';
    } else {
        taskSortField = field;
        taskSortDir = 'desc';
    }
    renderTaskTable();
}

async function toggleTaskDetail(taskId, url) {
    const prevExpanded = taskExpandedId;
    taskExpandedId = (taskExpandedId === taskId) ? null : taskId;

    // 折叠上一个展开的
    if (prevExpanded && prevExpanded !== taskId) {
        const prevDetailRow = document.getElementById(`task-detail-row-${prevExpanded}`);
        if (prevDetailRow) prevDetailRow.style.display = 'none';
        const prevRow = document.querySelector(`.task-row[onclick*="${prevExpanded}"]`);
        if (prevRow) prevRow.classList.remove('expanded');
        stopTaskPoll();
    }

    if (!taskExpandedId) {
        stopTaskPoll();
        return;
    }

    // 展开当前行
    const detailRow = document.getElementById(`task-detail-row-${taskId}`);
    const taskRow = document.querySelector(`.task-row[onclick*="${taskId}"]`);
    if (taskRow) taskRow.classList.add('expanded');
    if (detailRow) detailRow.style.display = '';

    // 加载并渲染详情
    const detailEl = document.getElementById(`task-detail-${taskId}`);
    if (!detailEl) return;

    detailEl.innerHTML = '<div class="task-detail-loading">加载中...</div>';

    try {
        const resp = await fetch(`/api/task/${taskId}`);
        const task = await resp.json();

        currentTaskId = taskId;
        tasks_url = url || task.url || '';

        renderTaskDetail(detailEl, task);

        // 进行中 / 下载中：自动轮询刷新
        if (task.status === 'processing' || task.status === 'downloading') {
            startTaskPoll(taskId, url);
        }
    } catch (err) {
        detailEl.innerHTML = `<div style="text-align:center;padding:16px;color:var(--error);">加载失败</div>`;
    }
}

function renderTaskDetail(container, task) {
    const status = task.status || '';
    let html = '';

    if (status === 'completed') {
        html = renderCompletedTaskDetail(task);
    } else if (status === 'processing' || status === 'downloading') {
        html = renderProcessingTaskDetail(task);
    } else if (status === 'cancelled' || status === 'error') {
        html = `<div class="task-detail-end">${status === 'cancelled' ? '已终止' : '出错'}: ${escapeHtml(task.message || '')}</div>`;
    } else {
        html = `<div class="task-detail-end">状态: ${status}</div>`;
    }

    container.innerHTML = html;
}

function openTaskResultView(taskId, url) {
    // 切换到完整结果页（带回播放器+字幕同步）
    currentTaskId = taskId;
    tasks_url = url || '';
    if (typeof loadResult === 'function') loadResult();
}

function renderProcessingTaskDetail(task) {
    const progress = task.progress || 0;
    const message = task.message || '';
    const step = task.step || '';

    const steps = [
        {key: 'download', label: '下载'},
        {key: 'transcribe', label: '转录'},
        {key: 'translate', label: '翻译'},
        {key: 'confirm', label: '确认'},
        {key: 'synthesize', label: '合成'},
        {key: 'mix', label: '拼接'},
    ];

    const stepIdx = steps.findIndex(s => s.key === step);
    let stepsHtml = '<div class="task-steps">';
    steps.forEach((s, i) => {
        const cls = i < stepIdx ? 'done' : i === stepIdx ? 'active' : '';
        stepsHtml += `<span class="task-step ${cls}">${s.label}</span>`;
        if (i < steps.length - 1) stepsHtml += '<span class="task-step-line"></span>';
    });
    stepsHtml += '</div>';

    return `
        <div class="task-progress-bar">
            <div class="task-progress-fill" style="width:${progress}%"></div>
        </div>
        <div class="task-progress-info">${(progress || 0).toFixed(0)}% · ${escapeHtml(message)}</div>
        ${stepsHtml}
        <div class="task-detail-actions">
            <button class="btn-cancel task-cancel-btn" onclick="event.stopPropagation(); cancelTaskInline('${currentTaskId}')">终止任务</button>
        </div>
    `;
}

function _resolveAudioUrl(serverPath) {
    if (!serverPath) return '';
    // 服务端返回的绝对路径 → 映射为 /api/audio/ 端点
    const markers = ['/data/results/', '/data/downloads/', '\\data\\results\\', '\\data\\downloads\\'];
    for (const m of markers) {
        const idx = serverPath.indexOf(m);
        if (idx !== -1) {
            const rel = serverPath.substring(idx + m.length).replace(/\\/g, '/');
            return `/api/audio/${rel}`;
        }
    }
    // fallback: 直接拼 basename
    const basename = serverPath.split('/').pop().split('\\').pop();
    return `/api/audio/${basename}`;
}

function renderCompletedTaskDetail(task) {
    const basename = task.basename || (task.result && task.result.basename) || '';
    const mixedAudio = task.result && task.result.mixed_audio ? task.result.mixed_audio : '';
    const originalAudio = task.result && task.result.original_audio ? task.result.original_audio : '';

    let playerHtml = '';
    if (basename) {
        playerHtml = `<div class="task-audio-inline">`;
        if (mixedAudio) {
            const mixedUrl = _resolveAudioUrl(mixedAudio);
            playerHtml += `<div class="task-audio-item"><span class="task-audio-label">混合</span><audio controls preload="none" src="${escapeAttr(mixedUrl)}"></audio></div>`;
        }
        if (originalAudio) {
            const origUrl = _resolveAudioUrl(originalAudio);
            playerHtml += `<div class="task-audio-item"><span class="task-audio-label">原始</span><audio controls preload="none" src="${escapeAttr(origUrl)}"></audio></div>`;
        }
        playerHtml += `</div>`;
    }

    const taskId = task.task_id || currentTaskId;
    const url = task.url || tasks_url || '';

    return `
        ${playerHtml}
        <div class="task-detail-end">✅ 已完成 · ${(task.mixed_duration || 0)}s 混合音频</div>
        <div class="task-detail-actions" style="margin-top:10px;">
            <button class="ep-btn-primary ep-btn" onclick="event.stopPropagation(); openTaskResultView('${taskId}', '${escapeAttr(url)}')">查看字幕</button>
        </div>
    `;
}

function startTaskPoll(taskId, url) {
    stopTaskPoll();
    taskPollInterval = setInterval(async () => {
        try {
            const resp = await fetch(`/api/task/${taskId}`);
            const task = await resp.json();
            const detailEl = document.getElementById(`task-detail-${taskId}`);
            if (detailEl) {
                renderTaskDetail(detailEl, task);
            }
            // 如果终态，停止轮询并刷新列表
            if (task.status === 'completed' || task.status === 'cancelled' || task.status === 'error') {
                stopTaskPoll();
                // 稍后刷新任务列表（更新卡片元数据）
                setTimeout(() => loadHistory(), 500);
            }
        } catch (e) {}
    }, 2000);
}

function stopTaskPoll() {
    if (taskPollInterval) {
        clearInterval(taskPollInterval);
        taskPollInterval = null;
    }
}

async function cancelTaskInline(taskId) {
    if (!confirm('确定终止该任务吗？')) return;
    try {
        const resp = await fetch(`/api/task/${taskId}/cancel`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            showToast('已发送终止请求');
        } else {
            showToast(data.error || '终止失败');
        }
    } catch (e) {
        showToast('网络错误');
    }
    // 刷新详情
    try {
        const resp = await fetch(`/api/task/${taskId}`);
        const task = await resp.json();
        const detailEl = document.getElementById(`task-detail-${taskId}`);
        if (detailEl) renderTaskDetail(detailEl, task);
    } catch (e) {}
}

async function viewHistoryTask(taskId, url) {
    // 兼容旧调用：直接展开任务
    toggleTaskDetail(taskId, url);
}

// 切换到任务页时停止轮询
const _origSwitchView = typeof switchView === 'function' ? switchView : null;
// (stopTaskPoll already safe to call via existing code)

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
                    <span class="settings-item-name">句子翻译</span>
                    <span class="settings-item-desc">100% 全文翻译为中英交替音频</span>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">跳过确认</span>
                    <span class="settings-item-desc">自动跳过翻译确认环节</span>
                </div>
                <div class="settings-item-control">
                    <label class="settings-toggle">
                        <input type="checkbox" id="cfg-skip_confirmation" ${cfg.skip_confirmation ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                </div>
            </div>
        </div>
        <!-- Confucius4-TTS (CPU) --></div>
        </div>
        <!-- Confucius4-TTS (CPU) --></div>
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
                    <span class="settings-item-name">全翻译句间间隔</span>
                    <span class="settings-item-desc">100% 翻译模式下的句间间隔（毫秒）</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-sentence_full_gap_ms"
                        value="${cfg.sentence_full_gap_ms}" min="0" max="2000" step="50">
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
                    <span class="settings-item-desc">Confucius4-TTS CPU 本地合成</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-tts_engine" onchange="onTTSEngineChange()">
                        <option value="confucius-tts" ${cfg.tts_engine === 'confucius-tts' ? 'selected' : ''}>Confucius4-TTS (CPU)</option>
                    </select>
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
                    <span class="settings-item-name">说话人间隔阈值</span>
                    <span class="settings-item-desc">同说话人最大间隔（秒），单人演讲建议 0.8</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-same_speaker_gap"
                        value="${cfg.same_speaker_gap}" min="0.1" max="3.0" step="0.1">
                </div>
            </div>
        </div>

        <!-- Confucius4-TTS-CPU -->
        <div class="settings-group" id="settings-group-confucius-tts">
            <div class="settings-group-title">🎵 Confucius4-TTS (CPU)</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">推理设备</span>
                    <span class="settings-item-desc">TTS 模型运行设备</span>
                </div>
                <div class="settings-item-control">
                    <select class="settings-select" id="cfg-confucius_tts_device">
                        <option value="cpu" ${cfg.confucius_tts_device === 'cpu' ? 'selected' : ''}>CPU</option>
                        <option value="cuda" ${cfg.confucius_tts_device === 'cuda' ? 'selected' : ''}>CUDA</option>
                    </select>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">采样温度</span>
                    <span class="settings-item-desc">T2S 采样温度，越高输出越多样</span>
                </div>
                <div class="settings-item-control">
                    <div class="settings-range-wrap">
                        <input type="range" class="settings-range" id="cfg-confucius_tts_temperature"
                            min="0.1" max="1.5" step="0.05" value="${cfg.confucius_tts_temperature}"
                            oninput="document.getElementById('val-conf_temp').textContent = this.value">
                        <span class="settings-range-val" id="val-conf_temp">${cfg.confucius_tts_temperature}</span>
                    </div>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">Top-P 采样</span>
                    <span class="settings-item-desc">核采样概率阈值</span>
                </div>
                <div class="settings-item-control">
                    <div class="settings-range-wrap">
                        <input type="range" class="settings-range" id="cfg-confucius_tts_top_p"
                            min="0.1" max="1.0" step="0.05" value="${cfg.confucius_tts_top_p}"
                            oninput="document.getElementById('val-conf_topp').textContent = this.value">
                        <span class="settings-range-val" id="val-conf_topp">${cfg.confucius_tts_top_p}</span>
                    </div>
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">Top-K 采样</span>
                    <span class="settings-item-desc">采样候选词数</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-confucius_tts_top_k"
                        value="${cfg.confucius_tts_top_k}" min="1" max="100" step="1">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">束搜索宽度</span>
                    <span class="settings-item-desc">Beam search 宽度，1=贪心解码</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-confucius_tts_num_beams"
                        value="${cfg.confucius_tts_num_beams}" min="1" max="10" step="1">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">重复惩罚</span>
                    <span class="settings-item-desc">Repetition penalty，越高重复越少</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-confucius_tts_repetition_penalty"
                        value="${cfg.confucius_tts_repetition_penalty}" min="1.0" max="20.0" step="0.5">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">扩散步数</span>
                    <span class="settings-item-desc">S2A 扩散步数，步数越多质量越高但越慢</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-confucius_tts_n_timesteps"
                        value="${cfg.confucius_tts_n_timesteps}" min="5" max="50" step="5">
                </div>
            </div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">CFG 引导强度</span>
                    <span class="settings-item-desc">无分类器引导，越高条件控制越强</span>
                </div>
                <div class="settings-item-control">
                    <div class="settings-range-wrap">
                        <input type="range" class="settings-range" id="cfg-confucius_tts_inference_cfg_rate"
                            min="0.1" max="1.0" step="0.05" value="${cfg.confucius_tts_inference_cfg_rate}"
                            oninput="document.getElementById('val-conf_cfg').textContent = this.value">
                        <span class="settings-range-val" id="val-conf_cfg">${cfg.confucius_tts_inference_cfg_rate}</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- ⚙️ 重试 -->
        <div class="settings-group">
            <div class="settings-group-title">⚙️ 重试设置</div>
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">全局重试次数</span>
                    <span class="settings-item-desc">LLM 翻译/识词失败自动重试次数</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-auto_retry_max"
                        value="${cfg.auto_retry_max}" min="0" max="10" step="1">
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
            <div class="settings-item">
                <div class="settings-item-label">
                    <span class="settings-item-name">CPU 线程数</span>
                    <span class="settings-item-desc">CPU 推理线程，默认 4，多核机器调大可显著加速</span>
                </div>
                <div class="settings-item-control">
                    <input type="number" class="settings-input" id="cfg-whisperx_threads"
                        value="${cfg.whisperx_threads ?? 4}" min="0" max="64" step="1">
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

    // 根据当前选中的模式和 TTS 引擎，联动显示/隐藏对应配置组
    onSettingsModeChange();
    onTTSEngineChange();
}

function onTTSEngineChange() {
    // 当前仅支持 Confucius4-TTS，确保其配置组始终可见
    const confGroup = document.getElementById('settings-group-confucius-tts');
    if (confGroup) {
        confGroup.classList.remove('hidden');
    }
}

function onSettingsModeChange() { /* Always sentence_translate mode */ }
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

    const payload = {skip_confirmation: getValue('cfg-skip_confirmation'),
        sentence_gap_ms: getValue('cfg-sentence_gap_ms'),
        sentence_full_gap_ms: getValue('cfg-sentence_full_gap_ms'),
        sentence_tts_voice_clone: getValue('cfg-sentence_tts_voice_clone'),
        tts_engine: getValue('cfg-tts_engine'),
        tts_text_format: getValue('cfg-tts_text_format'),
        whisperx_model: getValue('cfg-whisperx_model'),
        whisperx_device: getValue('cfg-whisperx_device'),
        whisperx_language: getValue('cfg-whisperx_language'),
        whisperx_threads: getValue('cfg-whisperx_threads'),
        ollama_base_url: getValue('cfg-ollama_base_url'),
        ollama_model: getValue('cfg-ollama_model'),
        llm_batch_size: getValue('cfg-llm_batch_size'),
        output_format: getValue('cfg-output_format'),
        output_bitrate: getValue('cfg-output_bitrate'),
        confucius_tts_device: getValue('cfg-confucius_tts_device'),
        confucius_tts_temperature: getValue('cfg-confucius_tts_temperature'),
        confucius_tts_top_p: getValue('cfg-confucius_tts_top_p'),
        confucius_tts_top_k: getValue('cfg-confucius_tts_top_k'),
        confucius_tts_num_beams: getValue('cfg-confucius_tts_num_beams'),
        confucius_tts_repetition_penalty: getValue('cfg-confucius_tts_repetition_penalty'),
        confucius_tts_n_timesteps: getValue('cfg-confucius_tts_n_timesteps'),
        confucius_tts_inference_cfg_rate: getValue('cfg-confucius_tts_inference_cfg_rate'),
        same_speaker_gap: getValue('cfg-same_speaker_gap'),
        auto_retry_max: getValue('cfg-auto_retry_max'),
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
    const updatesView = document.getElementById('updates-view');
    if (updatesView && updatesView.style.display !== 'none') return;

    // 检查是否有音频正在播放
    const originalAudio = document.getElementById('original-audio');
    const mixedAudio = document.getElementById('mixed-audio');
    const resultSection = document.getElementById('result-section');
    const isOnResult = resultSection && !resultSection.classList.contains('hidden');
    const isPlaying = (originalAudio && !originalAudio.paused) || (mixedAudio && !mixedAudio.paused);

    if (isOnResult && isPlaying) {
        activateMiniPlayer();
        showSection('hero');
    } else {
        resetAll();
    }
}

function goHomeFromResult() {
    const originalAudio = document.getElementById('original-audio');
    const mixedAudio = document.getElementById('mixed-audio');
    const isPlaying = (originalAudio && !originalAudio.paused) || (mixedAudio && !mixedAudio.paused);

    if (isPlaying) {
        activateMiniPlayer();
        showSection('hero');
    } else {
        resetAll();
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
    loadSavedSubscriptions();
    initMiniPlayerProgressSeek();
});
