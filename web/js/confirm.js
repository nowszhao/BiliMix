/* ============================================================
   BiliMix — 生词确认 + 句子翻译确认模块
   依赖: state.js, utils.js
   ============================================================ */

// ============================================================
// Word Confirmation UI
// ============================================================

async function loadConfirmationUI() {
    try {
        const resp = await fetch(`/api/task/${currentTaskId}/result`);
        const data = await resp.json();

        confirmSegments = data.segments || [];
        confirmWords = (data.difficult_words || []).map(w => ({...w}));
        originalConfirmWords = (data.difficult_words || []).map(w => ({...w}));

        await loadWordLevels();
        renderConfirmTranscript();
        renderConfirmWordsList();
        showSection('confirm');
    } catch (err) {
        console.error('Load confirmation error:', err);
        alert('加载确认页失败: ' + err.message);
    }
}

async function loadWordLevels() {
    const allWords = new Set();
    confirmSegments.forEach(seg => {
        const text = seg.text || '';
        text.split(/\s+/).forEach(token => {
            const clean = token.replace(/[^a-zA-Z'-]/g, '').toLowerCase();
            if (clean) allWords.add(clean);
        });
    });

    if (allWords.size === 0) return;

    try {
        const resp = await fetch('/api/word-levels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ words: Array.from(allWords) }),
        });
        const data = await resp.json();
        wordLevels = data.levels || {};
        levelNums = data.level_nums || {};
    } catch (err) {
        console.error('Load word levels error:', err);
    }
}

function getFreqClass(word) {
    if (!freqHighlightEnabled) return '';
    const levelSelect = document.getElementById('filter-level');
    const threshold = parseInt(levelSelect.value) || 5;
    const level = wordLevels[word.toLowerCase()];
    if (!level) return 'freq-beyond';
    const num = levelNums[level] || 0;
    if (num < threshold) return '';
    if (num <= 2) return 'freq-1k';
    if (num <= 4) return 'freq-3k';
    if (num <= 7) return 'freq-5k';
    if (num <= 9) return 'freq-8k';
    return 'freq-10k';
}

function renderConfirmTranscript() {
    const container = document.getElementById('confirm-transcript-container');
    if (!confirmSegments || confirmSegments.length === 0) {
        container.innerHTML = '<p style="color:var(--text-tertiary)">暂无转录内容</p>';
        return;
    }

    const singleWordSet = {};
    const multiWordList = [];
    confirmWords.forEach(w => {
        const key = w.english.toLowerCase();
        if (key.includes(' ')) {
            multiWordList.push({
                english: key,
                chinese: w.chinese,
                regex: new RegExp(`\\b(${escapeRegex(w.english)})\\b`, 'gi'),
            });
        } else {
            singleWordSet[key] = w.chinese;
        }
    });
    multiWordList.sort((a, b) => b.english.length - a.english.length);

    let html = '';
    confirmSegments.forEach((seg, idx) => {
        const time = formatTime(seg.start);
        let text = seg.text || '';

        const phraseMarkers = [];
        multiWordList.forEach(mw => {
            text = text.replace(mw.regex, (match) => {
                const markerId = `__PHRASE_${phraseMarkers.length}__`;
                phraseMarkers.push({
                    id: markerId,
                    html: `<span class="confirm-word marked" data-word="${escapeHtml(mw.english)}" onclick="removeMultiWordMark('${escapeAttr(mw.english)}')">${match}</span>`,
                });
                return markerId;
            });
        });

        const words = text.split(/(\s+)/);
        let wordHtml = words.map(token => {
            if (/^\s+$/.test(token)) return token;

            const phraseMarker = phraseMarkers.find(pm => token.includes(pm.id));
            if (phraseMarker) return token.replace(phraseMarker.id, phraseMarker.html);

            const cleanToken = token.replace(/[^a-zA-Z'-]/g, '').toLowerCase();
            const freqCls = cleanToken ? getFreqClass(cleanToken) : '';

            if (cleanToken && singleWordSet[cleanToken]) {
                return `<span class="confirm-word marked ${freqCls}" data-word="${escapeHtml(cleanToken)}" onclick="toggleConfirmWord(this, '${escapeAttr(cleanToken)}')">${token}</span>`;
            }
            if (cleanToken) {
                return `<span class="confirm-word ${freqCls}" data-word="${escapeHtml(cleanToken)}" onclick="toggleConfirmWord(this, '${escapeAttr(cleanToken)}')">${token}</span>`;
            }
            return token;
        }).join('');

        html += `
            <div class="confirm-segment" data-index="${idx}">
                <span class="segment-time">${time}</span>
                <span class="segment-text">${wordHtml}</span>
            </div>
        `;
    });

    container.innerHTML = html;
    container.addEventListener('mouseup', onConfirmTextSelect);
}

function removeMultiWordMark(english) {
    const idx = confirmWords.findIndex(w => w.english.toLowerCase() === english.toLowerCase());
    if (idx >= 0) confirmWords.splice(idx, 1);
    renderConfirmTranscript();
    renderConfirmWordsList();
}

function renderConfirmWordsList() {
    const list = document.getElementById('confirm-words-list');
    const countEl = document.getElementById('confirm-words-count');
    countEl.textContent = `📚 生词 (${confirmWords.length})`;

    if (confirmWords.length === 0) {
        list.innerHTML = '<p style="color:var(--text-tertiary); padding: 12px; text-align: center; font-size: 12px;">暂无生词，在左侧点击单词添加</p>';
        return;
    }

    let html = '';
    confirmWords.forEach((w, idx) => {
        const typeClass = w.type || 'word';
        const typeLabelMap = { 'word': '词', 'phrase': '短语', 'idiom': '习语', 'collocation': '搭配' };
        const typeLabel = typeLabelMap[w.type] || '词';
        html += `
            <div class="confirm-word-row" data-index="${idx}">
                <span class="confirm-word-eng" title="${escapeHtml(w.english)}">${escapeHtml(w.english)}</span>
                <span class="confirm-word-chi" title="${escapeHtml(w.chinese)}">${escapeHtml(w.chinese)}</span>
                <span class="confirm-word-type-badge ${typeClass}" onclick="toggleWordType(${idx})" title="点击切换">${typeLabel}</span>
                <button class="confirm-word-delete" onclick="removeConfirmWord(${idx})" title="移除">✕</button>
            </div>
        `;
    });
    list.innerHTML = html;
}

function toggleConfirmWord(el, word) {
    const isMarked = el.classList.contains('marked');
    if (isMarked) {
        const idx = confirmWords.findIndex(w => w.english.toLowerCase() === word.toLowerCase());
        if (idx >= 0) confirmWords.splice(idx, 1);
    } else {
        const existing = confirmWords.find(w => w.english.toLowerCase() === word.toLowerCase());
        if (!existing) {
            const segmentEl = el.closest('.confirm-segment');
            const segIdx = segmentEl ? parseInt(segmentEl.dataset.index) : -1;
            const contextSentence = (segIdx >= 0 && confirmSegments[segIdx]) ? confirmSegments[segIdx].text || '' : '';
            addWordWithTranslation(word, 'word', contextSentence);
            return;
        }
    }
    renderConfirmTranscript();
    renderConfirmWordsList();
}

async function addWordWithTranslation(english, type, contextSentence) {
    if (confirmWords.find(w => w.english.toLowerCase() === english.toLowerCase())) return;

    const tempItem = { english, chinese: '翻译中...', type };
    confirmWords.push(tempItem);
    renderConfirmTranscript();
    renderConfirmWordsList();

    try {
        const body = { english };
        if (contextSentence) body.context_sentence = contextSentence;
        const resp = await fetch('/api/translate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        tempItem.chinese = data.chinese || english;
    } catch (err) {
        tempItem.chinese = english;
    }

    renderConfirmTranscript();
    renderConfirmWordsList();
}

function removeConfirmWord(index) {
    confirmWords.splice(index, 1);
    renderConfirmTranscript();
    renderConfirmWordsList();
}

function toggleWordType(index) {
    if (confirmWords[index]) {
        const typeOrder = ['word', 'phrase', 'idiom', 'collocation'];
        const currentIdx = typeOrder.indexOf(confirmWords[index].type);
        confirmWords[index].type = typeOrder[(currentIdx + 1) % typeOrder.length];
        renderConfirmWordsList();
    }
}

function clearAllConfirmWords() {
    confirmWords = [];
    renderConfirmTranscript();
    renderConfirmWordsList();
}

async function addConfirmWordManual() {
    const input = document.getElementById('confirm-add-input');
    const english = input.value.trim();
    if (!english) return;

    const type = english.includes(' ') ? 'phrase' : 'word';
    let contextSentence = '';
    const lowerEng = english.toLowerCase();
    for (const seg of confirmSegments) {
        if ((seg.text || '').toLowerCase().includes(lowerEng)) {
            contextSentence = seg.text;
            break;
        }
    }

    input.value = '';
    await addWordWithTranslation(english, type, contextSentence);
}

// ============================================================
// Frequency Filter
// ============================================================

function onFilterSourceChange() {}

function onFilterLevelChange() {
    if (freqHighlightEnabled) renderConfirmTranscript();
    if (freqFilterApplied) {
        const removeSet = new Set(freqFilterAddedWords.map(w => w.toLowerCase()));
        confirmWords = confirmWords.filter(w => !removeSet.has(w.english.toLowerCase()));
        freqFilterAddedWords = [];
        freqFilterApplied = false;

        const btn = document.getElementById('freq-filter-btn');
        if (btn) {
            btn.classList.remove('active');
            btn.querySelector('.btn-text').textContent = '按词频标记';
        }
        renderConfirmTranscript();
        renderConfirmWordsList();
    }
}

function toggleFreqHighlight() {
    freqHighlightEnabled = !freqHighlightEnabled;
    const btn = document.getElementById('freq-highlight-btn');
    if (btn) btn.classList.toggle('active', freqHighlightEnabled);
    renderConfirmTranscript();
}

async function applyFrequencyFilter() {
    const btn = document.getElementById('freq-filter-btn');

    if (freqFilterApplied) {
        const removeSet = new Set(freqFilterAddedWords.map(w => w.toLowerCase()));
        confirmWords = confirmWords.filter(w => !removeSet.has(w.english.toLowerCase()));
        freqFilterAddedWords = [];
        freqFilterApplied = false;
        if (btn) {
            btn.classList.remove('active');
            btn.querySelector('.btn-text').textContent = '按词频标记';
        }
        renderConfirmTranscript();
        renderConfirmWordsList();
        return;
    }

    const levelSelect = document.getElementById('filter-level');
    const threshold = parseInt(levelSelect.value) || 5;

    const wordsToAdd = [];
    const wordContextMap = {};
    const existingSet = new Set(confirmWords.map(w => w.english.toLowerCase()));

    confirmSegments.forEach(seg => {
        const text = seg.text || '';
        text.split(/\s+/).forEach(token => {
            const clean = token.replace(/[^a-zA-Z'-]/g, '').toLowerCase();
            if (!clean || clean.length < 2) return;
            if (existingSet.has(clean)) return;

            const level = wordLevels[clean];
            let shouldAdd = false;
            if (!level) { shouldAdd = true; }
            else {
                const num = levelNums[level] || 0;
                if (num >= threshold) shouldAdd = true;
            }

            if (shouldAdd) {
                existingSet.add(clean);
                wordsToAdd.push(clean);
                if (!wordContextMap[clean]) wordContextMap[clean] = text;
            }
        });
    });

    if (wordsToAdd.length === 0) {
        alert(`未发现 ≥${threshold}k 的新单词`);
        return;
    }

    freqFilterAddedWords = [...wordsToAdd];

    const batchSize = 5;
    for (let i = 0; i < wordsToAdd.length; i += batchSize) {
        const batch = wordsToAdd.slice(i, i + batchSize);
        const promises = batch.map(async (word) => {
            const tempItem = { english: word, chinese: '翻译中...', type: 'word' };
            confirmWords.push(tempItem);
            try {
                const body = { english: word };
                if (wordContextMap[word]) body.context_sentence = wordContextMap[word];
                const resp = await fetch('/api/translate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const data = await resp.json();
                tempItem.chinese = data.chinese || word;
            } catch (err) {
                tempItem.chinese = word;
            }
        });

        renderConfirmTranscript();
        renderConfirmWordsList();
        await Promise.all(promises);
        renderConfirmTranscript();
        renderConfirmWordsList();
    }

    freqFilterApplied = true;
    if (btn) {
        btn.classList.add('active');
        btn.querySelector('.btn-text').textContent = '取消词频标记';
    }
}

// ============================================================
// Phrase Selection (drag to mark)
// ============================================================

function onConfirmTextSelect(e) {
    const selection = window.getSelection();
    const text = selection.toString().trim();

    if (!text || text.split(/\s+/).length < 2) {
        closePhrasePopover();
        return;
    }

    const container = document.getElementById('confirm-transcript-container');
    if (!container.contains(selection.anchorNode) || !container.contains(selection.focusNode)) return;

    pendingPhraseText = text;

    const popover = document.getElementById('phrase-popover');
    const textEl = document.getElementById('phrase-popover-text');
    textEl.textContent = `"${text}"`;

    const rect = selection.getRangeAt(0).getBoundingClientRect();
    popover.style.display = 'block';
    popover.style.left = Math.max(10, rect.left + rect.width / 2 - 120) + 'px';
    popover.style.top = (rect.top - 60 + window.scrollY) + 'px';
}

function closePhrasePopover() {
    const popover = document.getElementById('phrase-popover');
    if (popover) popover.style.display = 'none';
    pendingPhraseText = '';
}

async function confirmPhraseSelection() {
    if (!pendingPhraseText) return;
    const phrase = pendingPhraseText;

    let contextSentence = '';
    const selection = window.getSelection();
    if (selection.rangeCount > 0) {
        const range = selection.getRangeAt(0);
        const segmentEl = range.startContainer.parentElement?.closest('.confirm-segment');
        if (segmentEl) {
            const segIdx = parseInt(segmentEl.dataset.index);
            if (segIdx >= 0 && confirmSegments[segIdx]) {
                contextSentence = confirmSegments[segIdx].text || '';
            }
        }
    }

    closePhrasePopover();
    window.getSelection().removeAllRanges();
    await addWordWithTranslation(phrase, 'phrase', contextSentence);
}

function skipConfirmation() {
    confirmWords = originalConfirmWords.map(w => ({...w}));
    submitConfirmation();
}

async function submitConfirmation() {
    if (!currentTaskId) return;

    const btn = document.getElementById('confirm-continue-btn');
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '提交中...';

    try {
        const resp = await fetch(`/api/task/${currentTaskId}/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ difficult_words: confirmWords }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || '提交失败');
            btn.disabled = false;
            btn.querySelector('.btn-text').textContent = '确认并继续生成';
            return;
        }
        showSection('progress');
        resetProgressUI();
        startPolling();
    } catch (err) {
        alert('网络错误: ' + err.message);
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '确认并继续生成';
    }
}

function cancelFromConfirm() {
    resetAll();
}

// ============================================================
// Sentence Translation Confirmation UI
// ============================================================

async function loadSentenceConfirmationUI() {
    try {
        const resp = await fetch(`/api/task/${currentTaskId}/result`);
        const data = await resp.json();

        sentenceSegments = data.segments || [];
        sentenceTranslations = data.translations || {};
        sentenceTranslatedIndices = data.translated_indices || [];

        renderSentenceConfirmList();
        showSection('sentence-confirm');
    } catch (err) {
        console.error('Load sentence confirmation error:', err);
        alert('加载句子确认页失败: ' + err.message);
    }
}

function renderSentenceConfirmList() {
    const list = document.getElementById('sentence-confirm-list');
    if (!sentenceTranslatedIndices || sentenceTranslatedIndices.length === 0) {
        list.innerHTML = '<p style="color:var(--text-tertiary); text-align:center; padding:40px;">暂无翻译内容</p>';
        return;
    }

    let html = '';
    sentenceTranslatedIndices.forEach((segIdx, i) => {
        const seg = sentenceSegments[segIdx] || {};
        const english = seg.text || '';
        const chinese = sentenceTranslations[segIdx] || sentenceTranslations[String(segIdx)] || '';
        const time = formatTime(seg.start);

        html += `
            <div class="sentence-confirm-item" data-seg-idx="${segIdx}">
                <div class="sentence-confirm-header">
                    <span class="sentence-confirm-num">#${i + 1}</span>
                    <span class="sentence-confirm-time">${time}</span>
                    <label class="sentence-confirm-toggle">
                        <input type="checkbox" checked onchange="toggleSentenceTranslation(${segIdx}, this.checked)">
                        <span class="toggle-slider"></span>
                    </label>
                </div>
                <div class="sentence-confirm-eng">${escapeHtml(english)}</div>
                <div class="sentence-confirm-chi-wrap">
                    <textarea class="sentence-confirm-chi" data-seg-idx="${segIdx}"
                        rows="2" oninput="updateSentenceTranslation(${segIdx}, this.value)"
                    >${escapeHtml(chinese)}</textarea>
                </div>
            </div>
        `;
    });
    list.innerHTML = html;
}

function toggleSentenceTranslation(segIdx, enabled) {
    const item = document.querySelector(`.sentence-confirm-item[data-seg-idx="${segIdx}"]`);
    if (item) {
        item.classList.toggle('disabled', !enabled);
        const textarea = item.querySelector('textarea');
        if (textarea) textarea.disabled = !enabled;
    }
}

function updateSentenceTranslation(segIdx, value) {
    sentenceTranslations[segIdx] = value;
}

function skipSentenceConfirmation() {
    submitSentenceConfirmation();
}

async function submitSentenceConfirmation() {
    if (!currentTaskId) return;

    const btn = document.getElementById('sentence-confirm-btn');
    btn.disabled = true;
    btn.querySelector('.btn-text').textContent = '提交中...';

    const confirmedTranslations = {};
    const confirmedIndices = [];

    sentenceTranslatedIndices.forEach(segIdx => {
        const item = document.querySelector(`.sentence-confirm-item[data-seg-idx="${segIdx}"]`);
        const checkbox = item ? item.querySelector('input[type="checkbox"]') : null;
        const isEnabled = checkbox ? checkbox.checked : true;

        if (isEnabled) {
            const chinese = sentenceTranslations[segIdx] || sentenceTranslations[String(segIdx)] || '';
            if (chinese.trim()) {
                confirmedTranslations[segIdx] = chinese.trim();
                confirmedIndices.push(segIdx);
            }
        }
    });

    try {
        const resp = await fetch(`/api/task/${currentTaskId}/confirm_sentences`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                translations: confirmedTranslations,
                translated_indices: confirmedIndices,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || '提交失败');
            btn.disabled = false;
            btn.querySelector('.btn-text').textContent = '确认并继续生成';
            return;
        }
        showSection('progress');
        resetProgressUI();
        startPolling();
    } catch (err) {
        alert('网络错误: ' + err.message);
        btn.disabled = false;
        btn.querySelector('.btn-text').textContent = '确认并继续生成';
    }
}
