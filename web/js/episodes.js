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
    const views = ['updates-view', 'discover-view', 'tasks-view'];
    views.forEach(v => {
        const el = document.getElementById(v);
        if (el) el.style.display = (v === view + '-view') ? '' : 'none';
    });

    // 更新侧边栏高亮
    document.querySelectorAll('.sidebar-nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.view === view);
    });

    if (view === 'updates') {
        loadEpisodes();
        loadSidebarSubscriptions();
    } else if (view === 'discover') {
        loadSidebarSubscriptions();
    } else if (view === 'tasks') {
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

    container.innerHTML = '<div class="episodes-loading"><div class="spinner"></div><p>加载中...</p></div>';

    try {
        const params = new URLSearchParams({
            status: episodesStatusFilter,
            time_range: episodesTimeRange,
            page: episodesCurrentPage,
            page_size: 100,
        });
        if (episodesRssFilter) params.set('rss_url', episodesRssFilter);

        const resp = await fetch(`/api/episodes?${params}`);
        const data = await resp.json();

        episodesCache = data.episodes || [];
        renderEpisodeStats(data.total);
        renderEpisodes(episodesCache);
    } catch (err) {
        container.innerHTML = `<div class="episodes-empty"><p>加载失败: ${escapeHtml(err.message)}</p></div>`;
    }
}

async function loadEpisodeStats(totalCount) {
    try {
        const params = new URLSearchParams();
        if (episodesRssFilter) params.set('rss_url', episodesRssFilter);
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

    renderEpisodes(episodesCache);
}

async function markEpisode(id, status) {
    try {
        await fetch(`/api/episodes/${id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status }),
        });
        loadEpisodes();
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

    // 设置选中单集信息
    selectedEpisodeUrl = audioUrl;
    selectedEpisodeTitle = title;

    // 调用提交
    try {
        const resp = await fetch('/api/submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: audioUrl,
                title: title,
                skip_confirmation: true,
            }),
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

            showSection('progress');
            if (typeof startPolling === 'function') startPolling();
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
    episodesCurrentPage = 1;
    episodesExpandedId = null;
    loadEpisodes();
}

function setEpisodeTimeRange(range) {
    episodesTimeRange = range;
    episodesCurrentPage = 1;
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
    const clearBtn = document.getElementById('clear-rss-filter');
    if (clearBtn) clearBtn.style.display = rssUrl ? '' : 'none';
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
