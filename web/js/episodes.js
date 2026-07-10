/* ============================================================
   BiliMix — 订阅更新 / 单集管理模块
   依赖: state.js, utils.js
   ============================================================ */

// 更新页状态
let episodesCurrentPage = 1;
let episodesStatusFilter = 'all';
let episodesRssFilter = '';
let episodesTimeRange = 'today';
let episodesExpandedId = null;
let episodesCache = [];

// ============================================================
// 页面导航
// ============================================================

function switchView(view) {
    const views = ['updates-view', 'tasks-view', 'settings-view'];
    views.forEach(v => {
        const el = document.getElementById(v);
        if (el) el.style.display = (v === view + '-view') ? '' : 'none';
    });
    // 隐藏详情页
    const detail = document.getElementById('task-detail-view');
    if (detail) detail.style.display = 'none';

    // 更新侧边栏高亮
    document.querySelectorAll('.sidebar-nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.view === view);
    });

    if (view === 'settings') {
        loadSettings();
        return;
    } else if (view === 'updates') {
        loadEpisodes();
        loadSidebarSubscriptions();
    } else if (view === 'tasks') {
        if (typeof clearUploadedFile === 'function') clearUploadedFile();
        if (typeof loadHistory === 'function') loadHistory();
    }

    // 隐藏进度/结果/确认区
    const progress = document.getElementById('progress-section');
    const result = document.getElementById('result-section');
    const confirmSec = document.getElementById('confirm-section');
    const sentConfirm = document.getElementById('sentence-confirm-section');
    if (progress) progress.classList.add('hidden');
    if (result) result.classList.add('hidden');
    if (confirmSec) confirmSec.classList.add('hidden');
    if (sentConfirm) sentConfirm.classList.add('hidden');
}

// ============================================================
// 时间范围文案映射
// ============================================================

function getTimeRangeLabel(range) {
    const labels = { today: '今日', week: '本周', month: '本月', all: '全部时间' };
    return labels[range] || '全部时间';
}

// ============================================================
// 更新页 — 单集列表
// ============================================================

async function loadEpisodes() {
    const container = document.getElementById('episodes-list');
    if (!container) return;

    // 如果是首次加载或过滤器变化导致的「硬加载」
    if (episodesCache.length === 0) {
        container.innerHTML = '<div class="episodes-loading"><div class="spinner"></div><p>加载中...</p></div>';
    }

    try {
        const params = new URLSearchParams({
            status: 'all',
            time_range: episodesTimeRange,
            page: episodesCurrentPage,
            page_size: 100,
        });
        if (episodesRssFilter) params.set('rss_url', episodesRssFilter);

        const resp = await fetch(`/api/episodes?${params}`);
        const data = await resp.json();

        episodesCache = data.episodes || [];
        renderEpisodeStats(data.total);
        renderFilteredEpisodes();  // 本地过滤渲染，不再请求
        // 后台异步刷新统计
        loadEpisodeStats();
    } catch (err) {
        container.innerHTML = `<div class="episodes-empty"><p>加载失败: ${escapeHtml(err.message)}</p></div>`;
    }
}

function renderFilteredEpisodes() {
    let filtered = episodesCache;
    if (episodesStatusFilter !== 'all') {
        filtered = filtered.filter(ep => ep.status === episodesStatusFilter);
    }
    if (episodesRssFilter) {
        filtered = filtered.filter(ep => ep.rss_url === episodesRssFilter);
    }
    renderEpisodes(filtered);
}

async function loadEpisodeStats(overrideTotal) {
    try {
        const params = new URLSearchParams();
        if (episodesRssFilter) params.set('rss_url', episodesRssFilter);
        params.set('time_range', episodesTimeRange);
        const resp = await fetch(`/api/episodes/stats?${params}`);
        const data = await resp.json();
        const stats = data.stats || {};

        // 更新副标题
        const subtitle = document.getElementById('updates-subtitle');
        if (subtitle) {
            const unread = stats.unread || 0;
            subtitle.textContent = `${getTimeRangeLabel(episodesTimeRange)} · ${unread} 条未读`;
        }

        // 更新 Segmented Control 计数
        const counts = {
            all: stats.total || 0,
            unread: stats.unread || 0,
            transcribed: stats.transcribed || 0,
            read: stats.read || 0,
        };
        document.querySelectorAll('.seg-control-item').forEach(item => {
            const key = item.dataset.status;
            const countEl = item.querySelector('.seg-count');
            if (countEl) countEl.textContent = counts[key] || 0;
            item.classList.toggle('active', episodesStatusFilter === key);
        });

        // 更新侧边栏订阅未读数
        renderSidebarSubscriptions(data.subscriptions || []);
    } catch (e) {}
}

function renderEpisodeStats(total) {
    // 后台异步同步全量统计（保持 Segmented Control 计数准确），不阻塞渲染
    loadEpisodeStats(total);
}

function renderEpisodes(episodes) {
    const container = document.getElementById('episodes-list');
    if (!container) return;

    if (episodes.length === 0) {
        container.innerHTML = `
            <div class="episodes-empty">
                <div class="episodes-empty-icon">📻</div>
                <p>暂无更新</p>
                <p class="episodes-empty-hint">尝试刷新订阅源，或切换筛选条件</p>
            </div>`;
        return;
    }

    // 按订阅源分组
    const groups = {};
    episodes.forEach(ep => {
        const key = ep.rss_url;
        if (!groups[key]) {
            groups[key] = {
                title: ep.sub_title || '未知播客',
                image: ep.sub_image || '',
                author: ep.sub_author || '',
                episodes: [],
            };
        }
        groups[key].episodes.push(ep);
    });

    let html = '';
    for (const [rssUrl, group] of Object.entries(groups)) {
        html += `<div class="episode-group">`;
        html += `<div class="episode-group-header">`;
        if (group.image) {
            html += `<img src="${escapeAttr(group.image)}" class="episode-group-img" alt="" onerror="this.style.display='none'">`;
        } else {
            html += `<div class="episode-group-img episode-group-img-placeholder">🎙️</div>`;
        }
        html += `<span class="episode-group-title">${escapeHtml(group.title)}</span>`;
        html += `<span class="episode-group-count">${group.episodes.length} 集</span>`;
        html += `</div>`;
        html += `<div class="episode-group-body">`;

        group.episodes.forEach(ep => {
            html += renderEpisodeCard(ep);
        });

        html += `</div></div>`;
    }

    container.innerHTML = html;
}

function renderEpisodeCard(ep) {
    const statusColors = {
        unread: 'unread',
        read: 'read',
        transcribed: 'transcribed',
        dismissed: 'dismissed',
    };
    const statusLabels = {
        unread: '未读',
        read: '已读',
        transcribed: '已转录',
        dismissed: '已忽略',
    };
    const statusClass = statusColors[ep.status] || 'unread';
    const statusLabel = statusLabels[ep.status] || '未读';
    const isExpanded = episodesExpandedId === ep.id;

    let html = `<div class="episode-card ${statusClass} ${isExpanded ? 'expanded' : ''}" onclick="toggleEpisode(${ep.id})">`;
    html += `<div class="episode-card-main">`;
    html += `<div class="episode-card-left">`;
    html += `<span class="episode-status-dot ${statusClass}"></span>`;
    html += `<div class="episode-card-text">`;
    html += `<div class="episode-card-title">${escapeHtml(ep.title || '未知单集')}</div>`;
    html += `<div class="episode-card-meta">`;
    if (ep.duration) html += `<span>${escapeHtml(ep.duration)}</span>`;
    if (ep.published_at) html += `<span>${escapeHtml(ep.published_at)}</span>`;
    html += `<span class="episode-status-label">${statusLabel}</span>`;
    html += `</div>`;
    html += `</div>`;
    html += `</div>`;
    html += `<div class="episode-card-actions" onclick="event.stopPropagation()">`;
    if (ep.status === 'transcribed' && ep.task_id) {
        html += `<button class="ep-btn ep-btn-primary" onclick="viewEpisodeResult('${escapeAttr(ep.task_id)}')">查看结果</button>`;
    } else if (ep.status !== 'dismissed') {
        html += `<button class="ep-btn ep-btn-primary" onclick="processEpisode(${ep.id}, '${escapeAttr(ep.audio_url)}', '${escapeAttr(ep.title)}')">转录处理</button>`;
    } else {
        html += `<button class="ep-btn ep-btn-secondary" onclick="restoreEpisode(${ep.id})">恢复</button>`;
    }
    html += `</div>`;
    html += `</div>`;

    if (isExpanded) {
        html += `<div class="episode-card-detail">`;
        if (ep.description) {
            html += `<p class="episode-detail-desc">${escapeHtml(ep.description)}</p>`;
        }
        html += `<div class="episode-detail-actions">`;
        if (ep.status === 'unread') {
            html += `<button class="ep-btn-small" onclick="markEpisode(${ep.id}, 'read')">✓ 标记已读</button>`;
            html += `<button class="ep-btn-small" onclick="markEpisode(${ep.id}, 'dismissed')">忽略</button>`;
        } else if (ep.status === 'read') {
            html += `<button class="ep-btn-small" onclick="markEpisode(${ep.id}, 'unread')">标记未读</button>`;
            html += `<button class="ep-btn-small" onclick="markEpisode(${ep.id}, 'dismissed')">忽略</button>`;
        } else if (ep.status === 'transcribed') {
            html += `<button class="ep-btn-small" onclick="markEpisode(${ep.id}, 'unread')">标记未读</button>`;
        }
        html += `</div>`;
        html += `</div>`;
    }

    html += `</div>`;
    return html;
}

async function toggleEpisode(id) {
    const wasExpanded = episodesExpandedId === id;
    episodesExpandedId = wasExpanded ? null : id;

    const ep = episodesCache.find(e => e.id === id);

    // 展开时，若单集为未读状态，自动标记为已读
    if (!wasExpanded && ep && ep.status === 'unread') {
        try {
            await fetch(`/api/episodes/${id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: 'read' }),
            });
            ep.status = 'read';
            // 未读数变化，刷新统计和侧边栏角标
            loadEpisodeStats();
        } catch (e) {}
    }

    // 只更新目标卡片的状态类和展开内容，不重建整个列表
    const card = document.querySelector(`.episode-card[onclick*="${id}"]`);
    if (card) {
        if (wasExpanded) {
            card.classList.remove('expanded');
            const detail = card.querySelector('.episode-card-detail');
            if (detail) detail.remove();
        } else {
            card.classList.add('expanded');
            if (ep) {
                const newDetail = renderEpisodeDetail(ep);
                card.insertAdjacentHTML('beforeend', newDetail);
            }
        }
        // 更新状态样式
        if (ep) {
            const statusClass = {unread:'unread',read:'read',transcribed:'transcribed',dismissed:'dismissed'}[ep.status] || 'unread';
            card.className = card.className.replace(/unread|read|transcribed|dismissed/g, '');
            card.classList.add(statusClass, episodesExpandedId === id ? 'expanded' : '');
            const dot = card.querySelector('.episode-status-dot');
            if (dot) {
                dot.className = dot.className.replace(/unread|read|transcribed|dismissed/g, '');
                dot.classList.add(statusClass);
            }
            const label = card.querySelector('.episode-status-label');
            if (label) {
                label.textContent = {unread:'未读',read:'已读',transcribed:'已转录',dismissed:'已忽略'}[ep.status] || '未读';
            }
        }
    }
}

function renderEpisodeDetail(ep) {
    let html = `<div class="episode-card-detail">`;
    if (ep.description) {
        html += `<p class="episode-detail-desc">${escapeHtml(ep.description)}</p>`;
    }
    html += `<div class="episode-detail-actions">`;
    if (ep.status === 'unread') {
        html += `<button class="ep-btn-small" onclick="event.stopPropagation(); markEpisode(${ep.id}, 'read')">✓ 标记已读</button>`;
        html += `<button class="ep-btn-small" onclick="event.stopPropagation(); markEpisode(${ep.id}, 'dismissed')">忽略</button>`;
    } else if (ep.status === 'read') {
        html += `<button class="ep-btn-small" onclick="event.stopPropagation(); markEpisode(${ep.id}, 'unread')">标记未读</button>`;
        html += `<button class="ep-btn-small" onclick="event.stopPropagation(); markEpisode(${ep.id}, 'dismissed')">忽略</button>`;
    } else if (ep.status === 'transcribed') {
        html += `<button class="ep-btn-small" onclick="event.stopPropagation(); markEpisode(${ep.id}, 'unread')">标记未读</button>`;
    }
    html += `</div></div>`;
    return html;
}

async function markEpisode(id, status) {
    try {
        await fetch(`/api/episodes/${id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status }),
        });
        // 本地更新缓存，避免整表重新请求
        const ep = episodesCache.find(e => e.id === id);
        if (ep) ep.status = status;
        renderFilteredEpisodes();
        // 后台刷新统计
        loadEpisodeStats();
    } catch (err) {
        showToast('更新失败: ' + err.message);
    }
}

async function restoreEpisode(id) {
    await markEpisode(id, 'unread');
}

async function processEpisode(id, audioUrl, title) {
    if (!audioUrl) {
        showToast('该单集没有可用的音频 URL');
        return;
    }

    // 获取单集预知时长
    const ep = episodesCache.find(e => e.id === id);
    const duration = ep ? ep.duration : '';

    // 设置选中单集信息
    selectedEpisodeUrl = audioUrl;
    selectedEpisodeTitle = title;

    // 调用提交
    try {
        const body = {
            url: audioUrl,
            title: title,
            skip_confirmation: true,
        };
        if (duration) body.duration = duration;

        const resp = await fetch('/api/submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (resp.ok && data.task_id) {
            // 更新单集状态为已读 + 关联 task_id
            await fetch(`/api/episodes/${id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: 'read', task_id: data.task_id }),
            });

            currentTaskId = data.task_id;
            currentTaskTitle = title;
            tasks_url = audioUrl;

            // 跳转到任务页，新任务会自动出现在列表中
            showToast('✅ 任务已提交');
            switchView('tasks');
            // 稍等 loadHistory 完成后再展开该任务详情
            setTimeout(() => {
                if (typeof toggleTaskDetail === 'function') {
                    toggleTaskDetail(data.task_id, audioUrl);
                }
            }, 800);
        } else {
            showToast('提交失败: ' + (data.error || '未知错误'));
        }
    } catch (err) {
        showToast('提交失败: ' + err.message);
    }
}

function viewEpisodeResult(taskId) {
    currentTaskId = taskId;
    showSection('result');
    if (typeof loadResult === 'function') loadResult();
}

// ============================================================
// 筛选器交互
// ============================================================

function setEpisodeStatusFilter(status) {
    episodesStatusFilter = status;
    episodesExpandedId = null;
    document.querySelectorAll('.seg-control-item[data-status]').forEach(el => {
        el.classList.toggle('active', el.dataset.status === status);
    });
    renderFilteredEpisodes();
}

function setEpisodeTimeRange(range) {
    episodesTimeRange = range;
    episodesCurrentPage = 1;
    episodesExpandedId = null;
    // 更新按钮高亮
    document.querySelectorAll('.time-range-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.range === range);
    });
    // 时间范围变化需要重新请求后端（数据范围变了），但先清空 cache 再重载
    episodesCache = [];
    loadEpisodes();
}

function setEpisodeRssFilter(rssUrl) {
    episodesRssFilter = rssUrl;
    episodesCurrentPage = 1;
    episodesExpandedId = null;

    // 无论当前在哪个页面，点击订阅源都先跳转到更新页
    const updatesView = document.getElementById('updates-view');
    if (updatesView && updatesView.style.display === 'none') {
        switchView('updates'); // switchView 内部会以最新的 episodesRssFilter 重新加载列表
    } else {
        loadEpisodes();
    }

    // 立即更新侧边栏高亮
    document.querySelectorAll('.sidebar-sub-item').forEach(item => {
        item.classList.toggle('active', item.dataset.rssUrl === rssUrl);
    });

    // 更新来源筛选标签和清除按钮
    const clearBtn = document.getElementById('clear-rss-filter');
    const sourceLabel = document.getElementById('filter-source-label');
    const showClear = !!rssUrl;
    if (clearBtn) clearBtn.style.display = showClear ? '' : 'none';
    if (sourceLabel) {
        sourceLabel.style.display = showClear ? '' : 'none';
        if (showClear) {
            const sub = document.querySelector(`.sidebar-sub-item[data-rss-url="${escapeAttr(rssUrl)}"] .sidebar-sub-title`);
            sourceLabel.textContent = sub ? `来源: ${sub.textContent}` : '来源: 已筛选';
        }
    }
}

// ============================================================
// 全部已读
// ============================================================

async function markAllRead() {
    const scopeLabel = episodesRssFilter ? '当前订阅源' : '全部订阅源';
    const timeLabel = getTimeRangeLabel(episodesTimeRange);
    if (!confirm(`确定将「${scopeLabel}」「${timeLabel}」范围内的所有未读单集标记为已读吗？`)) return;

    const btn = document.getElementById('mark-all-read-btn');
    if (btn) btn.disabled = true;

    try {
        const resp = await fetch('/api/episodes/mark-all-read', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                rss_url: episodesRssFilter,
                time_range: episodesTimeRange,
            }),
        });
        const data = await resp.json();
        if (data.ok) {
            showToast(`已将 ${data.affected} 条单集标记为已读`);
            loadEpisodes();
        } else {
            showToast('操作失败');
        }
    } catch (err) {
        showToast('操作失败: ' + err.message);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ============================================================
// 刷新订阅
// ============================================================

let refreshing = false;

async function refreshAllSubscriptions() {
    if (refreshing) return;
    refreshing = true;

    const btn = document.getElementById('refresh-btn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<div class="spinner" style="width:14px;height:14px;"></div> 刷新中...';
    }

    try {
        const resp = await fetch('/api/episodes/refresh', { method: 'POST' });
        const data = await resp.json();
        if (data.ok) {
            const msg = data.new_episodes > 0
                ? `刷新完成，发现 ${data.new_episodes} 条新更新`
                : '刷新完成，暂无新内容';
            showToast(msg);
            loadEpisodes();
        } else {
            showToast('刷新失败');
        }
    } catch (err) {
        showToast('刷新失败: ' + err.message);
    } finally {
        refreshing = false;
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '⟳ 刷新';
        }
    }
}

// ============================================================
// 侧边栏订阅列表
// ============================================================

function renderSidebarSubscriptions(subs) {
    const container = document.getElementById('sidebar-subs');
    if (!container) return;

    if (!subs || subs.length === 0) {
        container.innerHTML = '<div class="sidebar-subs-empty">暂无订阅</div>';
        return;
    }

    let html = '';
    subs.forEach(s => {
        const unread = s.unread || 0;
        const isActive = episodesRssFilter === s.rss_url;
        const imgHtml = s.image
            ? `<img src="${escapeAttr(s.image)}" class="sidebar-sub-img" alt="" onerror="this.outerHTML='<div class=&quot;sidebar-sub-img sidebar-sub-img-placeholder&quot;>🎙️</div>'">`
            : `<div class="sidebar-sub-img sidebar-sub-img-placeholder">🎙️</div>`;
        html += `
            <div class="sidebar-sub-item ${isActive ? 'active' : ''}"
                 data-rss-url="${escapeAttr(s.rss_url)}"
                 onclick="setEpisodeRssFilter('${escapeAttr(s.rss_url)}')"
                 title="${escapeAttr(s.title)}">
                ${imgHtml}
                <span class="sidebar-sub-title">${escapeHtml(s.title)}</span>
                ${unread > 0 ? `<span class="sidebar-sub-badge">${unread}</span>` : ''}
                <button class="sidebar-sub-remove" onclick="event.stopPropagation(); unsubscribeFromSidebar('${escapeAttr(s.rss_url)}', '${escapeAttr(s.title)}')" title="取消订阅">✕</button>
            </div>
        `;
    });
    container.innerHTML = html;
}

async function unsubscribeFromSidebar(rssUrl, title) {
    if (!confirm(`确定取消订阅「${title}」吗？`)) return;
    if (typeof removeSubscriptionAndRefresh === 'function') {
        await removeSubscriptionAndRefresh(rssUrl);
    }
}

async function loadSidebarSubscriptions() {
    try {
        const resp = await fetch('/api/episodes/stats');
        const data = await resp.json();
        renderSidebarSubscriptions(data.subscriptions || []);
    } catch (e) {}
}

// ============================================================
// 添加订阅 Modal
// ============================================================

function openAddSubscriptionModal() {
    const overlay = document.getElementById('add-sub-modal');
    if (overlay) overlay.classList.add('open');
    // 清空搜索/RSS输入
    const searchInput = document.getElementById('modal-search-input');
    if (searchInput) searchInput.value = '';
    const rssInput = document.getElementById('modal-rss-input');
    if (rssInput) rssInput.value = '';
    const results = document.getElementById('modal-podcast-results');
    if (results) results.style.display = 'none';
    const rssPanel = document.getElementById('modal-rss-episodes-panel');
    if (rssPanel) rssPanel.style.display = 'none';
}

function closeAddSubscriptionModal() {
    const overlay = document.getElementById('add-sub-modal');
    if (overlay) overlay.classList.remove('open');
}

// Modal 内搜索（复用 podcast.js 的 searchPodcasts 能力，输出到 modal 面板）
let modalSearchTimer = null;

function debouncedModalPodcastSearch() {
    if (modalSearchTimer) clearTimeout(modalSearchTimer);
    modalSearchTimer = setTimeout(() => {
        const input = document.getElementById('modal-search-input');
        if (input && input.value.trim().length >= 2) modalSearchPodcasts();
    }, 500);
}

async function modalSearchPodcasts() {
    const input = document.getElementById('modal-search-input');
    const q = input ? input.value.trim() : '';
    if (!q) return;

    const resultsDiv = document.getElementById('modal-podcast-results');
    const listDiv = document.getElementById('modal-podcast-results-list');
    resultsDiv.style.display = 'block';
    listDiv.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-tertiary);">搜索中...</div>';

    try {
        const resp = await fetch(`/api/podcast/search?q=${encodeURIComponent(q)}`);
        const data = await resp.json();
        const podcasts = data.podcasts || [];

        if (podcasts.length === 0) {
            listDiv.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-tertiary);">未找到相关播客</div>';
            return;
        }

        listDiv.innerHTML = podcasts.map(p => {
            const imgHtml = p.image
                ? `<img src="${escapeAttr(p.image)}" class="podcast-item-img" onerror="this.style.display='none'" alt="">`
                : '<div class="podcast-item-img-placeholder">🎙️</div>';
            return `
                <div class="podcast-item">
                    ${imgHtml}
                    <div class="podcast-item-info">
                        <div class="podcast-item-title">${escapeHtml(p.title)}</div>
                        <div class="podcast-item-author">${escapeHtml(p.author || '')}</div>
                    </div>
                    <button class="podcast-sub-btn" onclick="modalToggleSubscription(event, '${escapeAttr(p.url)}', '${escapeAttr(p.title)}', '${escapeAttr(p.author || '')}', '${escapeAttr(p.image || '')}', this)">+ 订阅</button>
                </div>
            `;
        }).join('');

        // 标记已订阅的播客
        modalMarkExistingSubscriptions();
    } catch (e) {
        listDiv.innerHTML = '<div style="text-align:center;padding:16px;color:var(--error);">搜索失败</div>';
    }
}

async function modalMarkExistingSubscriptions() {
    try {
        const resp = await fetch('/api/subscriptions');
        const data = await resp.json();
        const subs = data.subscriptions || [];
        const urls = new Set(subs.map(s => s.rss_url));
        document.querySelectorAll('#modal-podcast-results-list .podcast-sub-btn').forEach(btn => {
            const onclick = btn.getAttribute('onclick') || '';
            const match = onclick.match(/'([^']*)'/);
            if (match && urls.has(match[1])) {
                btn.textContent = '✓ 已订阅';
                btn.classList.add('saved');
            }
        });
    } catch (e) {}
}

async function modalToggleSubscription(e, rssUrl, title, author, image, btnEl) {
    e.stopPropagation();
    if (typeof toggleSubscription === 'function') {
        await toggleSubscription(rssUrl, title, author, image, btnEl);
    }
}

// Modal 内 RSS 解析
async function modalLoadRssFeed() {
    const input = document.getElementById('modal-rss-input');
    const url = input ? input.value.trim() : '';
    if (!url) return;

    if (typeof loadRssFeed === 'function') {
        // 借用旧的 RSS 逻辑，临时把输入框的值指向 modal 输入框
        const originalInput = document.getElementById('rss-url-input');
        const tempSet = originalInput ? originalInput.value : '';
        if (originalInput) originalInput.value = url;

        // 创建临时 DOM 来接收结果，然后映射到 modal 面板
        try {
            const resp = await fetch(`/api/podcast/rss?url=${encodeURIComponent(url)}`);
            const data = await resp.json();
            if (data.error) {
                showToast('解析失败: ' + data.error);
                return;
            }
            renderModalRssEpisodes(data);
        } catch (e) {
            showToast('解析失败');
        }
    }
}

function renderModalRssEpisodes(data) {
    const panel = document.getElementById('modal-rss-episodes-panel');
    if (!panel) return;
    panel.style.display = 'block';

    const podcast = data.podcast || {};
    document.getElementById('modal-rss-podcast-img').src = (podcast.image || '');
    document.getElementById('modal-rss-podcast-title').textContent = podcast.title || '';
    document.getElementById('modal-rss-podcast-author').textContent = podcast.author || '';

    const listDiv = document.getElementById('modal-rss-episodes-list');
    const episodes = data.episodes || [];
    listDiv.innerHTML = episodes.map(ep => `
        <div class="episode-item" style="opacity:0.6">
            <div class="episode-item-info">
                <div class="episode-item-title">${escapeHtml(ep.title || '')}</div>
                <div class="episode-item-meta">
                    ${ep.duration ? `<span>${escapeHtml(ep.duration)}</span>` : ''}
                    ${ep.datePublished ? `<span>${escapeHtml(ep.datePublished)}</span>` : ''}
                </div>
            </div>
        </div>
    `).join('');

    // 添加订阅按钮
    const headerEl = panel.querySelector('.episodes-header');
    let subBtn = headerEl.querySelector('.modal-rss-sub-btn');
    if (!subBtn) {
        subBtn = document.createElement('button');
        subBtn.className = 'podcast-sub-btn modal-rss-sub-btn';
        subBtn.textContent = '+ 订阅';
        subBtn.onclick = async (e) => {
            e.stopPropagation();
            const rssUrl = document.getElementById('modal-rss-input').value.trim();
            if (rssUrl && typeof toggleSubscription === 'function') {
                await toggleSubscription(rssUrl, podcast.title || '', podcast.author || '', podcast.image || '', subBtn);
            }
        };
        headerEl.appendChild(subBtn);
    }
}

function closeModalRssEpisodes() {
    const panel = document.getElementById('modal-rss-episodes-panel');
    if (panel) panel.style.display = 'none';
}

// 点击 Modal 外部关闭
document.addEventListener('click', (e) => {
    const overlay = document.getElementById('add-sub-modal');
    if (overlay && overlay.classList.contains('open') && e.target === overlay) {
        closeAddSubscriptionModal();
    }
});

// ============================================================
// 初始化
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    // 默认显示更新页
    const defaultView = document.querySelector('.sidebar-nav-item.active');
    if (!defaultView) {
        const updatesItem = document.querySelector('.sidebar-nav-item[data-view="updates"]');
        if (updatesItem) updatesItem.classList.add('active');
    }
    // 加载初始数据
    setTimeout(() => {
        if (document.getElementById('updates-view') &&
            document.getElementById('updates-view').style.display !== 'none') {
            loadEpisodes();
            loadSidebarSubscriptions();
        }
    }, 100);
});
