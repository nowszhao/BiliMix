/* ============================================================
   BiliMix — 播客搜索 / RSS / 收藏 / 订阅 / 搜索建议模块
   依赖: state.js, utils.js
   ============================================================ */

// ============================================================
// Podcast Search
// ============================================================

function debouncedPodcastSearch() {
    if (podcastSearchTimer) clearTimeout(podcastSearchTimer);
    podcastSearchTimer = setTimeout(() => {
        const input = document.getElementById('podcast-search-input');
        if (input.value.trim().length >= 2) searchPodcasts();
    }, 500);
}

async function searchPodcasts() {
    const input = document.getElementById('podcast-search-input');
    const q = input.value.trim();
    if (!q) return;

    const resultsDiv = document.getElementById('podcast-results');
    const listDiv = document.getElementById('podcast-results-list');
    const countSpan = document.getElementById('podcast-results-count');

    document.getElementById('episodes-panel').style.display = 'none';
    resultsDiv.style.display = 'block';
    listDiv.innerHTML = '<div class="podcast-loading"><div class="spinner" style="margin:0 auto 8px;width:16px;height:16px;"></div><p>搜索中...</p></div>';

    try {
        const resp = await fetch(`/api/podcast/search?q=${encodeURIComponent(q)}`);
        const data = await resp.json();

        if (!resp.ok) {
            listDiv.innerHTML = `<div class="podcast-empty"><p>❌ ${escapeHtml(data.error || '搜索失败')}</p></div>`;
            countSpan.textContent = '搜索失败';
            return;
        }

        const podcasts = data.podcasts || [];
        countSpan.textContent = `找到 ${podcasts.length} 个播客`;

        if (podcasts.length === 0) {
            listDiv.innerHTML = '<div class="podcast-empty"><p>未找到相关播客，试试其他关键词</p></div>';
            return;
        }

        let html = '';
        podcasts.forEach(p => {
            const img = p.image
                ? `<img src="${escapeAttr(p.image)}" alt="" class="podcast-item-img" onerror="this.style.display='none'">`
                : '<div class="podcast-item-img-placeholder">🎙️</div>';
            html += `
                <div class="podcast-item" onclick="selectPodcast('${escapeAttr(p.url)}', '${escapeAttr(p.title)}', '${escapeAttr(p.author)}', '${escapeAttr(p.image || '')}')">
                    ${img}
                    <div class="podcast-item-info">
                        <div class="podcast-item-title">${escapeHtml(p.title)}</div>
                        <div class="podcast-item-author">${escapeHtml(p.author || '未知作者')}</div>
                    </div>
                </div>
            `;
        });

        listDiv.innerHTML = html;

        // Favorites removed

    } catch (err) {
        listDiv.innerHTML = `<div class="podcast-empty"><p>网络错误: ${escapeHtml(err.message)}</p></div>`;
        countSpan.textContent = '搜索失败';
    }
}

function closePodcastResults() {
    const resultsDiv = document.getElementById('podcast-results');
    if (resultsDiv) resultsDiv.style.display = 'none';
}

async function selectPodcast(rssUrl, title, author, image) {
    currentPodcastFeedId = rssUrl;

    // 记录当前播客信息（用于"下一集"功能）
    lastPodcastRssUrl = rssUrl;
    lastPodcastTitle = title;
    lastPodcastAuthor = author;
    lastPodcastImage = image;

    document.getElementById('podcast-results').style.display = 'none';
    const panel = document.getElementById('episodes-panel');
    panel.style.display = 'block';

    document.getElementById('episodes-podcast-title').textContent = title;
    document.getElementById('episodes-podcast-author').textContent = author || '未知作者';
    const imgEl = document.getElementById('episodes-podcast-img');
    if (image) { imgEl.src = image; imgEl.style.display = 'block'; }
    else { imgEl.style.display = 'none'; }

    const listDiv = document.getElementById('episodes-list');
    listDiv.innerHTML = '<div class="podcast-loading"><div class="spinner" style="margin:0 auto 8px;width:16px;height:16px;"></div><p>加载单集...</p></div>';

    // 添加订阅按钮到 header（每次重新创建）
    const header = document.querySelector('#episodes-panel .episodes-header');
    if (header) {
        const oldBtn = header.querySelector('.episodes-save-btn');
        if (oldBtn) oldBtn.remove();

        let isSubscribed = false;
        try {
            const resp = await fetch('/api/subscriptions');
            const data = await resp.json();
            isSubscribed = (data.subscriptions || []).some(s => s.rss_url === rssUrl);
        } catch (e) {}

        const subBtn = document.createElement('button');
        subBtn.className = 'episodes-save-btn' + (isSubscribed ? ' saved' : '');
        subBtn.textContent = isSubscribed ? '✓ 已订阅' : '+ 订阅';
        subBtn.onclick = (e) => {
            e.stopPropagation();
            toggleSubscription(rssUrl, title, author, image, subBtn);
        };
        header.appendChild(subBtn);
    }

    try {
        const resp = await fetch(`/api/podcast/rss?url=${encodeURIComponent(rssUrl)}`);
        const data = await resp.json();

        if (!resp.ok) {
            listDiv.innerHTML = `<div class="podcast-empty"><p>❌ ${escapeHtml(data.error || '获取失败')}</p></div>`;
            return;
        }

        const podcast = data.podcast || {};
        if (podcast.title && podcast.title !== title) {
            document.getElementById('episodes-podcast-title').textContent = podcast.title;
        }
        if (podcast.author) {
            document.getElementById('episodes-podcast-author').textContent = podcast.author;
        }
        if (podcast.image && !image) {
            imgEl.src = podcast.image;
            imgEl.style.display = 'block';
        }

        const episodes = data.episodes || [];
        if (episodes.length === 0) {
            listDiv.innerHTML = '<div class="podcast-empty"><p>暂无单集</p></div>';
            return;
        }

        let html = '';
        episodes.forEach(ep => {
            if (!ep.enclosureUrl) return;
            html += `
                <div class="episode-item" onclick="selectEpisode('${escapeAttr(ep.enclosureUrl)}', '${escapeAttr(ep.title)}', '${escapeAttr(ep.duration)}', '${escapeAttr(ep.datePublished)}')">
                    <div class="episode-item-main">
                        <div class="episode-item-title">${escapeHtml(ep.title)}</div>
                        <div class="episode-item-meta">
                            ${ep.datePublished ? `<span>${escapeHtml(ep.datePublished)}</span>` : ''}
                            ${ep.duration ? `<span>⏱ ${escapeHtml(ep.duration)}</span>` : ''}
                        </div>
                    </div>
                    <button class="episode-select-btn" title="选择此集">▶</button>
                </div>
            `;
        });

        listDiv.innerHTML = html || '<div class="podcast-empty"><p>所有单集均无音频链接</p></div>';
    } catch (err) {
        listDiv.innerHTML = `<div class="podcast-empty"><p>网络错误: ${escapeHtml(err.message)}</p></div>`;
    }
}

function backToPodcastList() {
    document.getElementById('episodes-panel').style.display = 'none';
    document.getElementById('podcast-results').style.display = 'block';
}

function selectEpisode(url, title, duration, date) {
    selectedEpisodeUrl = url;
    selectedEpisodeTitle = title || '';
    document.getElementById('episodes-panel').style.display = 'none';
    const selectedDiv = document.getElementById('selected-episode');
    selectedDiv.style.display = 'flex';

    document.getElementById('selected-episode-title').textContent = title || '未命名';
    const details = [date, duration ? `⏱ ${duration}` : ''].filter(Boolean).join(' · ');
    document.getElementById('selected-episode-detail').textContent = details || url.substring(0, 60);
}

function clearSelectedEpisode() {
    selectedEpisodeUrl = '';
    selectedEpisodeTitle = '';
    document.getElementById('selected-episode').style.display = 'none';
}

// ============================================================
// RSS Feed
// ============================================================

async function loadRssFeed() {
    const input = document.getElementById('rss-url-input');
    const feedUrl = input.value.trim();
    if (!feedUrl) { shakeElement(input.parentElement); input.focus(); return; }

    const panel = document.getElementById('rss-episodes-panel');
    const listDiv = document.getElementById('rss-episodes-list');
    panel.style.display = 'block';
    listDiv.innerHTML = '<div class="podcast-loading"><div class="spinner" style="margin:0 auto 8px;width:16px;height:16px;"></div><p>解析 RSS 中...</p></div>';

    // 记录 RSS URL
    lastPodcastRssUrl = feedUrl;

    try {
        const resp = await fetch(`/api/podcast/rss?url=${encodeURIComponent(feedUrl)}`);
        const data = await resp.json();

        if (!resp.ok) {
            listDiv.innerHTML = `<div class="podcast-empty"><p>❌ ${escapeHtml(data.error || '解析失败')}</p></div>`;
            return;
        }

        const podcast = data.podcast || {};
        document.getElementById('rss-podcast-title').textContent = podcast.title || '未知播客';
        document.getElementById('rss-podcast-author').textContent = podcast.author || '未知作者';
        const imgEl = document.getElementById('rss-podcast-img');
        if (podcast.image) { imgEl.src = podcast.image; imgEl.style.display = 'block'; }
        else { imgEl.style.display = 'none'; }

        lastPodcastTitle = podcast.title || '';
        lastPodcastAuthor = podcast.author || '';
        lastPodcastImage = podcast.image || '';

        // 添加订阅按钮到 RSS episodes header（每次重新创建）
        const header = document.querySelector('#rss-episodes-panel .episodes-header');
        if (header) {
            const oldBtn = header.querySelector('.episodes-save-btn');
            if (oldBtn) oldBtn.remove();

            let isSubscribed = false;
            try {
                const subResp = await fetch('/api/subscriptions');
                const subData = await subResp.json();
                isSubscribed = (subData.subscriptions || []).some(s => s.rss_url === feedUrl);
            } catch (e) {}

            const subBtn = document.createElement('button');
            subBtn.className = 'episodes-save-btn' + (isSubscribed ? ' saved' : '');
            subBtn.textContent = isSubscribed ? '✓ 已订阅' : '+ 订阅';
            subBtn.onclick = (e) => {
                e.stopPropagation();
                toggleSubscription(feedUrl, lastPodcastTitle, lastPodcastAuthor, lastPodcastImage, subBtn);
            };
            header.appendChild(subBtn);
        }

        const episodes = data.episodes || [];
        if (episodes.length === 0) {
            listDiv.innerHTML = '<div class="podcast-empty"><p>暂无单集</p></div>';
            return;
        }

        let html = '';
        episodes.forEach(ep => {
            if (!ep.enclosureUrl) return;
            html += `
                <div class="episode-item" onclick="selectRssEpisode('${escapeAttr(ep.enclosureUrl)}', '${escapeAttr(ep.title)}', '${escapeAttr(ep.duration)}', '${escapeAttr(ep.datePublished)}')">
                    <div class="episode-item-main">
                        <div class="episode-item-title">${escapeHtml(ep.title)}</div>
                        <div class="episode-item-meta">
                            ${ep.datePublished ? `<span>${escapeHtml(ep.datePublished)}</span>` : ''}
                            ${ep.duration ? `<span>⏱ ${escapeHtml(ep.duration)}</span>` : ''}
                        </div>
                    </div>
                    <button class="episode-select-btn" title="选择此集">▶</button>
                </div>
            `;
        });

        listDiv.innerHTML = html || '<div class="podcast-empty"><p>所有单集均无音频链接</p></div>';
    } catch (err) {
        listDiv.innerHTML = `<div class="podcast-empty"><p>网络错误: ${escapeHtml(err.message)}</p></div>`;
    }
}

function selectRssEpisode(url, title, duration, date) {
    rssSelectedEpisodeUrl = url;
    rssSelectedEpisodeTitle = title || '';
    document.getElementById('rss-episodes-panel').style.display = 'none';
    const selectedDiv = document.getElementById('rss-selected-episode');
    selectedDiv.style.display = 'flex';

    document.getElementById('rss-selected-episode-title').textContent = title || '未命名';
    const details = [date, duration ? `⏱ ${duration}` : ''].filter(Boolean).join(' · ');
    document.getElementById('rss-selected-episode-detail').textContent = details || url.substring(0, 60);
}

function clearRssSelectedEpisode() {
    rssSelectedEpisodeUrl = '';
    rssSelectedEpisodeTitle = '';
    document.getElementById('rss-selected-episode').style.display = 'none';
}

// ============================================================
// Quick Panel — 收藏 & 订阅
// ============================================================

async function loadQuickPanel() {
    const panel = document.getElementById('quick-panel');
    if (!panel) return; // 新布局无 quick-panel
    let hasContent = false;

    // Favorites removed - skip favorites loading

    try {
        const resp = await fetch('/api/subscriptions');
        const data = await resp.json();
        const subs = data.subscriptions || [];
        const subSection = document.getElementById('subscriptions-section');
        const subList = document.getElementById('subscriptions-list');
        const subCount = document.getElementById('subscriptions-count');

        if (subs.length > 0) {
            subSection.style.display = 'block';
            subCount.textContent = subs.length;
            subList.innerHTML = subs.map(s => `
                <div class="quick-podcast-card" onclick="quickOpenPodcast('${escapeAttr(s.rss_url)}', '${escapeAttr(s.title)}', '${escapeAttr(s.author)}', '${escapeAttr(s.image || '')}')">
                    ${s.image ? `<img src="${escapeAttr(s.image)}" class="quick-podcast-img" onerror="this.style.display='none'" alt="">` : '<div class="quick-podcast-img-placeholder">📡</div>'}
                    <div class="quick-podcast-info">
                        <div class="quick-podcast-title">${escapeHtml(s.title)}</div>
                        <div class="quick-podcast-author">${escapeHtml(s.author || '')}</div>
                    </div>
                    <button class="quick-podcast-remove" onclick="event.stopPropagation(); removeSubscriptionAndRefresh('${escapeAttr(s.rss_url)}')" title="取消订阅">✕</button>
                </div>
            `).join('');
            hasContent = true;
        } else {
            subSection.style.display = 'none';
        }
    } catch (e) {
        console.warn('Failed to load subscriptions:', e);
    }

    panel.style.display = hasContent ? 'flex' : 'none';
}

function quickOpenPodcast(rssUrl, title, author, image) {
    switchInputMode('search');
    selectPodcast(rssUrl, title, author, image);
}

async function removeFavoriteAndRefresh(rssUrl) {
    try {
        await fetch('/api/favorites', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rss_url: rssUrl }),
        });
        showToast('⭐ 已取消收藏');
        loadQuickPanel();
    } catch (e) {
        showToast('❌ 操作失败');
    }
}

async function removeSubscriptionAndRefresh(rssUrl) {
    try {
        await fetch('/api/subscriptions', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rss_url: rssUrl }),
        });
        showToast('📡 已取消订阅');
        loadQuickPanel();
        loadSavedSubscriptions();
        // 如果取消的正是当前筛选的订阅源，重置为全部来源
        if (typeof episodesRssFilter !== 'undefined' && episodesRssFilter === rssUrl) {
            episodesRssFilter = '';
        }
        if (typeof loadSidebarSubscriptions === 'function') loadSidebarSubscriptions();
        if (typeof loadEpisodes === 'function') loadEpisodes();
    } catch (e) {
        showToast('❌ 操作失败');
    }
}

// ============================================================
// Favorite Stars on Search Results
// ============================================================

async function addFavoriteStarsToResults() {
    const listDiv = document.getElementById('podcast-results-list');
    const items = listDiv.querySelectorAll('.podcast-item');

    for (const item of items) {
        const onclick = item.getAttribute('onclick');
        if (!onclick) continue;

        const match = onclick.match(/selectPodcast\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'\)/);
        if (!match) continue;

        const [_, rssUrl, title, author, image] = match;

        let isFav = false;
        try {
            const resp = await fetch(`/api/favorites/check?rss_url=${encodeURIComponent(rssUrl)}`);
            const data = await resp.json();
            isFav = data.is_favorite;
        } catch (e) {}

        if (!item.querySelector('.podcast-fav-btn')) {
            const favBtn = document.createElement('button');
            favBtn.className = 'podcast-fav-btn' + (isFav ? ' favorited' : '');
            favBtn.textContent = isFav ? '⭐' : '☆';
            favBtn.title = isFav ? '取消收藏' : '收藏此播客';
            favBtn.onclick = (e) => {
                e.stopPropagation();
                toggleFavorite(rssUrl, title, author, image, favBtn);
            };
            item.appendChild(favBtn);
        }
    }
}

async function toggleFavorite(rssUrl, title, author, image, btnEl) {
    try {
        const checkResp = await fetch(`/api/favorites/check?rss_url=${encodeURIComponent(rssUrl)}`);
        const checkData = await checkResp.json();

        if (checkData.is_favorite) {
            await fetch('/api/favorites', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ rss_url: rssUrl }),
            });
            btnEl.classList.remove('favorited');
            btnEl.textContent = '☆';
            showToast('已取消收藏');
        } else {
            await fetch('/api/favorites', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title, author, image, rss_url: rssUrl }),
            });
            btnEl.classList.add('favorited');
            btnEl.textContent = '⭐';
            showToast('⭐ 已收藏');
        }
        loadQuickPanel();
    } catch (e) {
        showToast('❌ 操作失败');
    }
}

// ============================================================
// Subscription Toggle
// ============================================================

async function toggleSubscription(rssUrl, title, author, image, btnEl) {
    try {
        const resp = await fetch('/api/subscriptions');
        const data = await resp.json();
        const exists = (data.subscriptions || []).some(s => s.rss_url === rssUrl);

        if (exists) {
            await fetch('/api/subscriptions', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ rss_url: rssUrl }),
            });
            btnEl.textContent = '+ 订阅';
            btnEl.classList.remove('saved');
            showToast('📡 已取消订阅');
        } else {
            await fetch('/api/subscriptions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title, author, image, rss_url: rssUrl }),
            });
            btnEl.textContent = '✓ 已订阅';
            btnEl.classList.add('saved');
            showToast('📡 已订阅');
            // 新订阅后自动刷新该源的单集，立即填充更新列表
            _autoRefreshNewFeed(rssUrl);
        }
        loadQuickPanel();
        loadSavedSubscriptions();
    } catch (e) {
        showToast('❌ 操作失败');
    }
}

async function _autoRefreshNewFeed(rssUrl) {
    try {
        const resp = await fetch(`/api/episodes/refresh/${encodeURIComponent(rssUrl)}`, { method: 'POST' });
        const data = await resp.json();
        if (data.ok && data.new_episodes > 0) {
            showToast(`📡 已拉取 ${data.new_episodes} 条新单集`);
        }
    } catch (e) {}
    // 刷新侧边栏和更新列表
    if (typeof loadSidebarSubscriptions === 'function') loadSidebarSubscriptions();
    if (typeof loadEpisodes === 'function') loadEpisodes();
}

// ============================================================
// Saved Subscriptions in RSS Tab
// ============================================================

async function loadSavedSubscriptions() {
    // 新布局：订阅列表统一在左侧边栏展示，此函数保留兼容旧调用但不再渲染。
    const panel = document.getElementById('rss-saved-subscriptions');
    if (!panel) return;
    try {
        const resp = await fetch('/api/subscriptions');
        const data = await resp.json();
        const subs = data.subscriptions || [];
        const listDiv = document.getElementById('rss-saved-subscriptions-list');

        if (subs.length === 0) { panel.style.display = 'none'; return; }

        panel.style.display = 'block';
        listDiv.innerHTML = subs.map(s => `
            <div class="saved-sub-item" onclick="useSubscriptionRss('${escapeAttr(s.rss_url)}')">
                ${s.image ? `<img src="${escapeAttr(s.image)}" class="saved-sub-img" onerror="this.style.display='none'" alt="">` : ''}
                <div class="saved-sub-info">
                    <div class="saved-sub-title">${escapeHtml(s.title)}</div>
                    <div class="saved-sub-author">${escapeHtml(s.author || '')}</div>
                </div>
                <button class="saved-sub-remove" onclick="event.stopPropagation(); removeSubscriptionAndRefresh('${escapeAttr(s.rss_url)}')" title="取消订阅">✕</button>
            </div>
        `).join('');
    } catch (e) {
        console.warn('Failed to load subscriptions:', e);
    }
}

function useSubscriptionRss(rssUrl) {
    const input = document.getElementById('rss-url-input');
    input.value = rssUrl;
    loadRssFeed();
}

// ============================================================
// 订阅刷新
// ============================================================

async function refreshAllSubscriptions() {
    const btn = document.querySelector('.sidebar-subs-refresh');
    if (btn) btn.classList.add('spinning');
    try {
        await fetch('/api/subscriptions/refresh', { method: 'POST' });
    } catch (e) {
        console.warn('Refresh failed:', e);
    }
    // 延迟 3 秒后刷新侧边栏（给后端拉取 RSS 的时间）
    setTimeout(() => {
        if (btn) btn.classList.remove('spinning');
        // 重新加载订阅列表和单集
        if (typeof loadSubscriptions === 'function') loadSubscriptions();
        if (typeof loadEpisodes === 'function') loadEpisodes();
    }, 3000);
}

// ============================================================
// Search Suggestions
// ============================================================

async function showSearchSuggestions() {
    if (searchSuggestionTimer) clearTimeout(searchSuggestionTimer);
    searchSuggestionTimer = setTimeout(async () => {
        const input = document.getElementById('podcast-search-input');
        const prefix = input.value.trim();
        const container = document.getElementById('search-suggestions');

        try {
            const resp = await fetch(`/api/search-history/suggestions?q=${encodeURIComponent(prefix)}`);
            const data = await resp.json();
            const suggestions = data.suggestions || [];

            if (suggestions.length === 0) { container.style.display = 'none'; return; }

            container.innerHTML = suggestions.map(s => `
                <div class="search-suggestion-item" onclick="useSearchSuggestion('${escapeAttr(s)}')">
                    <span class="search-suggestion-icon">🕐</span>
                    <span class="search-suggestion-text">${escapeHtml(s)}</span>
                </div>
            `).join('');
            container.style.display = 'block';
        } catch (e) {
            container.style.display = 'none';
        }
    }, 200);
}

function useSearchSuggestion(keyword) {
    const input = document.getElementById('podcast-search-input');
    input.value = keyword;
    document.getElementById('search-suggestions').style.display = 'none';
    searchPodcasts();
}

function handleSearchKeydown(event) {
    if (event.key === 'Enter') {
        document.getElementById('search-suggestions').style.display = 'none';
        searchPodcasts();
    } else if (event.key === 'Escape') {
        document.getElementById('search-suggestions').style.display = 'none';
    }
}

// 点击外部关闭搜索建议
document.addEventListener('click', (e) => {
    const container = document.getElementById('search-suggestions');
    const input = document.getElementById('podcast-search-input');
    if (container && input && !container.contains(e.target) && e.target !== input) {
        container.style.display = 'none';
    }
});

// ============================================================
// Next Episode
// ============================================================

async function loadNextEpisode() {
    if (!lastPodcastRssUrl) { showToast('❗ 未找到关联的播客 RSS'); return; }
    resetAll();
    switchInputMode('search');
    selectPodcast(lastPodcastRssUrl, lastPodcastTitle, lastPodcastAuthor, lastPodcastImage);
}
