/* ============================================================
   BiliMix — 音频同步 & 全屏转录模块
   依赖: state.js, utils.js
   ============================================================ */

// 流式播放轮询（预留接口）
function stopStreamPolling() { /* no-op */ }

// ============================================================
// Auto-scroll & Audio Sync
// ============================================================

function toggleAutoScroll() {
    autoScrollEnabled = !autoScrollEnabled;
    ['auto-scroll-btn', 'fs-auto-scroll-btn'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.classList.toggle('active', autoScrollEnabled);
    });
}

function setupAudioSync() {
    const originalAudio = document.getElementById('original-audio');
    const mixedAudio = document.getElementById('mixed-audio');

    if (originalAudio) {
        originalAudio.removeEventListener('timeupdate', onOriginalAudioTimeUpdate);
        originalAudio.addEventListener('timeupdate', onOriginalAudioTimeUpdate);
    }
    if (mixedAudio) {
        mixedAudio.removeEventListener('timeupdate', onMixedAudioTimeUpdate);
        mixedAudio.addEventListener('timeupdate', onMixedAudioTimeUpdate);
    }
}

function onOriginalAudioTimeUpdate(e) {
    if (mixedSegmentsMode) return;
    const currentTime = e.target.currentTime;
    const newIndex = findSegmentByOriginalTime(currentTime);
    if (newIndex !== activeSegmentIndex && newIndex >= 0) {
        activeSegmentIndex = newIndex;
        highlightSegment(newIndex);
    }
}

function onMixedAudioTimeUpdate(e) {
    const mixedTime = e.target.currentTime;
    const newIndex = mixedSegmentsMode
        ? findSegmentByOriginalTime(mixedTime)
        : findSegmentByOriginalTime(mixedTimeToOriginalTime(mixedTime));
    if (newIndex !== activeSegmentIndex && newIndex >= 0) {
        activeSegmentIndex = newIndex;
        highlightSegment(newIndex);
    }
}

/**
 * 将混合音频的播放时间映射回原始音频的时间。
 *
 * 使用左闭右开区间 [mixed_start, mixed_end) 匹配，避免边界处
 * 优先命中前一个 gap 条目而非当前段落条目。
 * 最后一个条目使用闭区间以覆盖尾部边界。
 */
function mixedTimeToOriginalTime(mixedTime) {
    if (!timeMappingData || timeMappingData.length === 0) return mixedTime;

    const lastIdx = timeMappingData.length - 1;
    for (let i = 0; i <= lastIdx; i++) {
        const m = timeMappingData[i];
        const inRange = (i === lastIdx)
            ? (mixedTime >= m.mixed_start && mixedTime <= m.mixed_end)
            : (mixedTime >= m.mixed_start && mixedTime < m.mixed_end);
        if (inRange) {
            const mixedDuration = m.mixed_end - m.mixed_start;
            const origDuration = m.orig_end - m.orig_start;
            if (mixedDuration <= 0) return m.orig_start;
            const ratio = (mixedTime - m.mixed_start) / mixedDuration;
            return m.orig_start + ratio * origDuration;
        }
    }

    if (timeMappingData.length > 0) {
        const last = timeMappingData[lastIdx];
        if (mixedTime >= last.mixed_end) return last.orig_end;
    }
    return mixedTime;
}

/**
 * 将原始音频时间映射到混合音频时间。
 *
 * 使用左闭右开区间 [orig_start, orig_end) 匹配，避免边界处
 * segment.start 恰好等于前一个 gap 的 orig_end 时命中 gap。
 * 最后一个条目使用闭区间以覆盖尾部边界。
 */
function originalTimeToMixedTime(originalTime) {
    if (!timeMappingData || timeMappingData.length === 0) return originalTime;

    const lastIdx = timeMappingData.length - 1;
    for (let i = 0; i <= lastIdx; i++) {
        const m = timeMappingData[i];
        const inRange = (i === lastIdx)
            ? (originalTime >= m.orig_start && originalTime <= m.orig_end)
            : (originalTime >= m.orig_start && originalTime < m.orig_end);
        if (inRange) {
            const origDuration = m.orig_end - m.orig_start;
            const mixedDuration = m.mixed_end - m.mixed_start;
            if (origDuration <= 0) return m.mixed_start;
            const ratio = (originalTime - m.orig_start) / origDuration;
            return m.mixed_start + ratio * mixedDuration;
        }
    }

    if (timeMappingData.length > 0) {
        const last = timeMappingData[lastIdx];
        if (originalTime >= last.orig_end) return last.mixed_end;
    }
    return originalTime;
}

/**
 * 根据原始音频时间查找 segment 索引。
 */
function findSegmentByOriginalTime(originalTime) {
    if (!segmentsData || segmentsData.length === 0) return -1;

    let newIndex = -1;
    for (let i = 0; i < segmentsData.length; i++) {
        const seg = segmentsData[i];
        if (originalTime >= seg.start && originalTime <= seg.end) {
            newIndex = i;
            break;
        }
        if (i > 0 && originalTime > segmentsData[i - 1].end && originalTime < seg.start) {
            newIndex = i - 1;
            break;
        }
    }

    if (newIndex === -1 && segmentsData.length > 0) {
        const last = segmentsData[segmentsData.length - 1];
        if (originalTime >= last.start) newIndex = segmentsData.length - 1;
    }
    return newIndex;
}

function highlightSegment(index) {
    const inlineContainer = document.getElementById('transcript-container');
    updateSegmentHighlight(inlineContainer, index);

    const fsContainer = document.getElementById('fullscreen-transcript');
    if (fsContainer) updateSegmentHighlight(fsContainer, index);
}

function updateSegmentHighlight(container, index) {
    if (!container) return;

    const segments = container.querySelectorAll('.transcript-segment');
    segments.forEach((seg, i) => {
        seg.classList.toggle('active-segment', i === index);
    });

    if (autoScrollEnabled && segments[index]) {
        const target = segments[index];
        const scrollParent = container.closest('.fullscreen-transcript') ||
                             container.closest('.tab-content') ||
                             container;
        const parentRect = scrollParent.getBoundingClientRect();
        const targetRect = target.getBoundingClientRect();
        const isVisible = (
            targetRect.top >= parentRect.top &&
            targetRect.bottom <= parentRect.bottom
        );
        if (!isVisible) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }
}

function seekToSegment(startTime) {
    const mixedTime = mixedSegmentsMode ? startTime : originalTimeToMixedTime(startTime);
    if (isFullscreen) {
        const fsAudio = document.getElementById('fullscreen-audio');
        if (fsAudio) {
            if (fullscreenAudioSource === 'mixed') {
                fsAudio.currentTime = mixedTime;
            } else {
                fsAudio.currentTime = startTime;
            }
            if (fsAudio.paused) fsAudio.play().catch(() => {});
        }
    } else {
        const originalAudio = document.getElementById('original-audio');
        const mixedAudio = document.getElementById('mixed-audio');
        if (mixedAudio && !mixedAudio.paused) {
            mixedAudio.currentTime = mixedTime;
        } else if (originalAudio) {
            originalAudio.currentTime = startTime;
            if (originalAudio.paused) originalAudio.play().catch(() => {});
        }
    }
}

// ============================================================
// Fullscreen Transcript
// ============================================================

function toggleTranscriptFullscreen() {
    const overlay = document.getElementById('fullscreen-overlay');
    isFullscreen = !isFullscreen;

    if (isFullscreen) {
        const srcContainer = document.getElementById('transcript-container');
        const fsContainer = document.getElementById('fullscreen-transcript');
        fsContainer.innerHTML = srcContainer.innerHTML;

        const originalAudio = document.getElementById('original-audio');
        const mixedAudio = document.getElementById('mixed-audio');
        const fsAudio = document.getElementById('fullscreen-audio');

        const mixedIsActive = mixedAudio && !mixedAudio.paused;
        const originalIsActive = originalAudio && !originalAudio.paused;

        if (mixedIsActive || (!originalIsActive && mixedAudio && mixedAudio.currentTime > 0)) {
            fullscreenAudioSource = 'mixed';
            if (mixedAudio.src) {
                fsAudio.src = mixedAudio.src;
                fsAudio.currentTime = mixedAudio.currentTime;
                if (!mixedAudio.paused) {
                    fsAudio.play().catch(() => {});
                    mixedAudio.pause();
                }
            }
            fsAudio.addEventListener('timeupdate', onMixedAudioTimeUpdate);
        } else {
            fullscreenAudioSource = 'original';
            if (originalAudio.src) {
                fsAudio.src = originalAudio.src;
                fsAudio.currentTime = originalAudio.currentTime;
                if (!originalAudio.paused) {
                    fsAudio.play().catch(() => {});
                    originalAudio.pause();
                }
            }
            fsAudio.addEventListener('timeupdate', onOriginalAudioTimeUpdate);
        }

        const fsTitle = document.getElementById('fullscreen-title');
        if (fsTitle) {
            fsTitle.textContent = fullscreenAudioSource === 'mixed'
                ? '📝 原文转录（中英混合音频）'
                : '📝 原文转录（原始音频）';
        }

        fsContainer.querySelectorAll('.transcript-segment').forEach(seg => {
            seg.addEventListener('click', () => {
                const start = parseFloat(seg.dataset.start);
                seekToSegment(start);
            });
        });

        overlay.classList.add('open');
        document.body.style.overflow = 'hidden';

        const btn = document.getElementById('fullscreen-btn');
        if (btn) {
            btn.querySelector('.toolbar-btn-icon').textContent = '⊗';
            btn.querySelector('.toolbar-btn-text').textContent = '退出全屏';
        }

        if (activeSegmentIndex >= 0) highlightSegment(activeSegmentIndex);

    } else {
        const fsAudio = document.getElementById('fullscreen-audio');
        const originalAudio = document.getElementById('original-audio');
        const mixedAudio = document.getElementById('mixed-audio');

        if (fsAudio.src) {
            if (fullscreenAudioSource === 'mixed') {
                if (mixedAudio) {
                    mixedAudio.currentTime = fsAudio.currentTime;
                    if (!fsAudio.paused) mixedAudio.play().catch(() => {});
                }
                fsAudio.removeEventListener('timeupdate', onMixedAudioTimeUpdate);
            } else {
                originalAudio.currentTime = fsAudio.currentTime;
                if (!fsAudio.paused) originalAudio.play().catch(() => {});
                fsAudio.removeEventListener('timeupdate', onOriginalAudioTimeUpdate);
            }
            fsAudio.pause();
        }

        overlay.classList.remove('open');
        document.body.style.overflow = '';

        const btn = document.getElementById('fullscreen-btn');
        if (btn) {
            btn.querySelector('.toolbar-btn-icon').textContent = '⛶';
            btn.querySelector('.toolbar-btn-text').textContent = '全屏';
        }

        if (activeSegmentIndex >= 0) highlightSegment(activeSegmentIndex);
    }
}
