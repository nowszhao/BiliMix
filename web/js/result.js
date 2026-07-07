/* ============================================================
   BiliMix — 结果渲染模块
   依赖: state.js, utils.js, audio-sync.js
   ============================================================ */

function getAudioExt() {
    const url = tasks_url || '';
    const match = url.match(/\.(mp3|wav|m4a|ogg|flac|aac)(\?|$)/i);
    return match ? '.' + match[1].toLowerCase() : '.mp3';
}

/**
 * 仅在 src 真正改变时才设置 audio.src，
 * 避免正在播放时重新加载导致进度重置和播放停止。
 */
function setAudioSrcIfChanged(audioId, newSrc) {
    const audio = document.getElementById(audioId);
    if (!audio) return;
    // 浏览器会将相对路径规范化为绝对路径存储在 audio.src 中
    // 用 URL 对象统一比较
    try {
        const currentUrl = audio.src ? new URL(audio.src).pathname : '';
        const newUrl = new URL(newSrc, window.location.origin).pathname;
        if (currentUrl === newUrl) return; // src 相同，跳过
    } catch (e) {
        // URL 解析失败，直接比较
        if (audio.src === newSrc) return;
    }
    audio.src = newSrc;
}

// ============================================================
// Load & Render Result
// ============================================================

async function loadResult() {
    try {
        const resp = await fetch(`/api/task/${currentTaskId}/result`);
        const data = await resp.json();

        // 保存任务标题到全局变量（用于 Mini Player 等场景）
        if (data.title) {
            currentTaskTitle = data.title;
        }

        renderResult(data);

        const resultSection = document.getElementById('result-section');
        if (resultSection && resultSection.classList.contains('hidden')) {
            showSection('result');
        }
    } catch (err) {
        console.error('Load result error:', err);
    }
}

function renderResult(data) {
    const result = data.result;

    const streamPlayer = document.getElementById('stream-player');
    const mixedItem = document.getElementById('mixed-audio-item');
    if (streamPlayer) streamPlayer.style.display = 'none';
    if (mixedItem) mixedItem.style.display = '';
    stopStreamPolling();

    timeMappingData = data.time_mapping || [];

    // 兼容老数据：sentence_pairs 缺失时，从 translations + translated_indices 现场重建
    let sentencePairs = data.sentence_pairs || [];
    if (sentencePairs.length === 0 && data.translations && data.translated_indices) {
        const segments = data.segments || [];
        const sortedIdx = [...data.translated_indices].sort((a, b) => a - b);
        sentencePairs = sortedIdx
            .filter(idx => idx < segments.length && data.translations[idx])
            .map(idx => ({
                index: idx,
                english: (segments[idx].text || '').trim(),
                chinese: data.translations[idx],
                start: segments[idx].start || 0,
                end: segments[idx].end || 0,
            }));
    }

    const translatedCount = result?.translated_segments || sentencePairs.length;

    document.getElementById('badge-words').textContent = translatedCount + ' 句翻译';
    document.getElementById('badge-duration').textContent =
        (result?.mixed_duration || '--') + ' 秒';

    if (result) {
        const basename = result.basename;
        const ext = getAudioExt();
        setAudioSrcIfChanged('original-audio', `/api/audio/${basename}${ext}`);
        setAudioSrcIfChanged('mixed-audio', `/api/audio/${basename}/${basename}_sentence.mp3`);
    }

    const mixedLabel = document.querySelector('.audio-item:last-child .audio-label');
    if (mixedLabel) mixedLabel.innerHTML = '<span class="label-dot mixed"></span>中文配音音频';

    if (sentencePairs.length === 0) {
        // 完全无翻译数据：显示原版英文转录 + 提示
        const container = document.getElementById('transcript-container');
        if (container) {
            container.innerHTML = '<p style="color:var(--text-tertiary);padding:24px 0;">'
                + '⚠️ 此任务无翻译数据（可能为旧任务或翻译失败）。<br>'
                + '请在历史记录中重新提交或使用「重试」按钮重新处理。'
                + '</p>';
        }
        segmentsData = [];
        return;
    }

    renderTranscriptSentenceMode(data.segments, sentencePairs, null);
}

// ============================================================
// Transcript Rendering — 单词替换模式
// ============================================================

function renderTranscript(segments, difficultWords) {
    const container = document.getElementById('transcript-container');
    if (!segments || segments.length === 0) {
        container.innerHTML = '<p style="color:var(--text-tertiary)">暂无转录内容</p>';
        segmentsData = [];
        return;
    }

    segmentsData = segments;

    const dwSet = {};
    if (difficultWords) {
        difficultWords.forEach(w => { dwSet[w.english.toLowerCase()] = w.chinese; });
    }

    const infoEl = document.getElementById('transcript-info');
    if (infoEl) infoEl.textContent = `共 ${segments.length} 个句子`;

    let html = '';
    segments.forEach((seg, idx) => {
        const time = formatTime(seg.start);
        let text = seg.text || '';

        Object.keys(dwSet).sort((a, b) => b.length - a.length).forEach(eng => {
            const regex = new RegExp(`\\b(${escapeRegex(eng)})\\b`, 'gi');
            text = text.replace(regex, `<span class="highlight-word" data-chinese="${dwSet[eng]}">$1</span>`);
        });

        html += `
            <div class="transcript-segment" data-index="${idx}" data-start="${seg.start}" data-end="${seg.end}" onclick="seekToSegment(${seg.start})">
                <span class="segment-time">${time}</span>
                <span class="segment-text">${text}</span>
            </div>
        `;
    });

    container.innerHTML = html;
    setupAudioSync();
}

// ============================================================
// Transcript Rendering — 句子翻译模式
// ============================================================

function renderTranscriptSentenceMode(segments, sentencePairs, difficultWords) {
    const container = document.getElementById('transcript-container');
    if (!segments || segments.length === 0) {
        container.innerHTML = '<p style="color:var(--text-tertiary)">暂无转录内容</p>';
        segmentsData = [];
        return;
    }

    segmentsData = segments.map(s => ({ ...s }));  // 浅拷贝避免覆盖原文

    const translationMap = {};
    if (sentencePairs) {
        sentencePairs.forEach(p => { translationMap[p.index] = p.chinese; });
    }

    const dwSet = {};
    if (difficultWords && difficultWords.length > 0) {
        difficultWords.forEach(w => { dwSet[w.english.toLowerCase()] = w.chinese; });
    }
    const hasDW = Object.keys(dwSet).length > 0;

    const infoEl = document.getElementById('transcript-info');
    if (infoEl) {
        infoEl.textContent = `共 ${segments.length} 个句子，${sentencePairs?.length || 0} 句翻译`;
    }

    let html = '';
    segments.forEach((seg, idx) => {
        const time = formatTime(seg.start);
        let text = seg.text || '';
        const chinese = translationMap[idx];
        segmentsData[idx]._chinese = chinese;  // 供 copyTranscript 使用

        if (hasDW) {
            const escaped = escapeHtml(text);
            let highlighted = escaped;
            Object.keys(dwSet).sort((a, b) => b.length - a.length).forEach(eng => {
                const regex = new RegExp(`\\b(${escapeRegex(eng)})\\b`, 'gi');
                highlighted = highlighted.replace(regex, `<span class="highlight-word" data-chinese="${dwSet[eng]}">$1</span>`);
            });
            text = highlighted;
        } else {
            text = escapeHtml(text);
        }

        html += `
            <div class="transcript-segment ${chinese ? 'has-translation' : ''}" data-index="${idx}" data-start="${seg.start}" data-end="${seg.end}" onclick="seekToSegment(${seg.start})">
                <span class="segment-time">${time}</span>
                <div class="segment-content">
                    <span class="segment-text">${text}</span>
                    ${chinese ? `<span class="segment-chinese">${escapeHtml(chinese)}</span>` : ''}
                </div>
            </div>
        `;
    });

    container.innerHTML = html;
    setupAudioSync();
}

// ============================================================
// Vocabulary Rendering
// ============================================================

function renderVocabulary(words) {
    const grid = document.getElementById('vocab-grid');
    if (!words || words.length === 0) {
        grid.innerHTML = '<p style="color:var(--text-tertiary)">暂无生词</p>';
        return;
    }

    let html = '';
    words.forEach(w => {
        const typeClass = w.type || 'word';
        const typeLabelMap = { 'word': '单词', 'phrase': '短语', 'idiom': '习语', 'collocation': '搭配' };
        const typeLabel = typeLabelMap[w.type] || '单词';
        html += `
            <div class="vocab-card">
                <div class="vocab-english">${w.english}</div>
                <div class="vocab-chinese">${w.chinese}</div>
                <span class="vocab-type ${typeClass}">${typeLabel}</span>
            </div>
        `;
    });
    grid.innerHTML = html;
}

// ============================================================
// Replacements Rendering
// ============================================================

function renderReplacements(replacements) {
    const list = document.getElementById('replacements-list');
    if (!list) return;
    if (!replacements || replacements.length === 0) {
        list.innerHTML = '<p style="color:var(--text-tertiary)">暂无替换记录</p>';
        return;
    }

    let html = `
        <div class="replacement-header">
            <span>时间</span><span>英文</span><span>中文</span><span>类型</span>
        </div>
    `;

    replacements.forEach(r => {
        const time = formatTime(r.start) + ' - ' + formatTime(r.end);
        const typeClass = r.type || 'word';
        const typeLabelMap = { 'word': '单词', 'phrase': '短语', 'idiom': '习语', 'collocation': '搭配' };
        const typeLabel = typeLabelMap[r.type] || '单词';
        html += `
            <div class="replacement-row">
                <span class="time">${time}</span>
                <span class="eng">${r.english}</span>
                <span class="chi">${r.chinese}</span>
                <span class="type-tag ${typeClass}">${typeLabel}</span>
            </div>
        `;
    });
    list.innerHTML = html;
}

// ============================================================
// Sentence Pairs Rendering
// ============================================================

function renderSentencePairs(sentencePairs, targetContainerId) {
    const containerId = targetContainerId || 'vocab-grid';
    const container = document.getElementById(containerId);
    if (!container) return;
    if (!sentencePairs || sentencePairs.length === 0) {
        container.innerHTML = '<p style="color:var(--text-tertiary)">暂无翻译对照</p>';
        return;
    }

    let html = '';
    if (containerId === 'replacements-list') html = '<div class="vocab-grid">';

    sentencePairs.forEach((p, i) => {
        const time = formatTime(p.start);
        html += `
            <div class="sentence-pair-card">
                <div class="sentence-pair-header">
                    <span class="sentence-pair-num">#${i + 1}</span>
                    <span class="sentence-pair-time">${time}</span>
                </div>
                <div class="sentence-pair-eng">${escapeHtml(p.english)}</div>
                <div class="sentence-pair-chi">${escapeHtml(p.chinese)}</div>
            </div>
        `;
    });

    if (containerId === 'replacements-list') html += '</div>';
    container.innerHTML = html;
}

// ============================================================
// Copy Transcript
// ============================================================

function copyTranscript() {
    if (!segmentsData || segmentsData.length === 0) {
        showToast('⚠️ 暂无字幕可复制');
        return;
    }
    // 提取纯文本：时间戳 + 英文 + 中文翻译（如有）
    const lines = segmentsData.map((seg, i) => {
        const time = formatTime(seg.start);
        const english = (seg.text || '').trim();
        const chinese = seg._chinese || '';  // renderTranscriptSentenceMode 可注入
        if (chinese) {
            return `[${time}] ${english}\n       ${chinese}`;
        }
        return `[${time}] ${english}`;
    });
    const text = lines.join('\n');

    copyTextToClipboard(text).then(ok => {
        if (ok) {
            showToast('✅ 已复制 ' + lines.length + ' 行字幕');
        } else {
            showToast('❌ 复制失败，请手动选择文本');
        }
    });
}

/**
 * 兼容性复制文本到剪贴板：
 * - 优先使用 Clipboard API（需安全上下文 HTTPS / localhost）
 * - 回退到 document.execCommand('copy')（HTTP 局域网访问时使用）
 * 返回 Promise<boolean>，true 表示复制成功。
 */
function copyTextToClipboard(text) {
    // 路径1：Clipboard API（安全上下文可用）
    if (navigator.clipboard && window.isSecureContext && typeof navigator.clipboard.writeText === 'function') {
        return navigator.clipboard.writeText(text)
            .then(() => true)
            .catch(() => copyViaTextarea(text));
    }
    // 路径2：execCommand 回退（非安全上下文，如 http://192.168.x.x）
    return Promise.resolve(copyViaTextarea(text));
}

function copyViaTextarea(text) {
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        // 避免页面滚动跳动
        ta.style.position = 'fixed';
        ta.style.top = '0';
        ta.style.left = '0';
        ta.style.width = '2em';
        ta.style.height = '2em';
        ta.style.padding = '0';
        ta.style.border = 'none';
        ta.style.outline = 'none';
        ta.style.boxShadow = 'none';
        ta.style.background = 'transparent';
        ta.setAttribute('readonly', '');
        document.body.appendChild(ta);

        // iOS Safari 需要先创建 Range 选中
        if (navigator.userAgent.match(/ipad|iphone/i)) {
            const range = document.createRange();
            range.selectNodeContents(ta);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            ta.setSelectionRange(0, text.length);
        } else {
            ta.select();
        }

        let ok = false;
        try {
            ok = document.execCommand('copy');
        } catch (e) {
            ok = false;
        }
        document.body.removeChild(ta);
        return ok;
    } catch (e) {
        return false;
    }
}
