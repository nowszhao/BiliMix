/* ============================================================
   BiliMix — 全局状态模块
   ============================================================ */

let currentTaskId = null;
let currentTaskTitle = '';
let currentProcessMode = 'sentence_translate';
let pollTimer = null;
let tasks_url = '';
let currentInputMode = 'search';
let currentTopMode = 'audio';         // 'audio' | 'video'  顶层模式
let currentVideoMode = 'youtube';     // 'youtube' | 'local' 视频子模式
let selectedEpisodeUrl = '';
let selectedEpisodeTitle = '';
let rssSelectedEpisodeUrl = '';
let rssSelectedEpisodeTitle = '';

// 转录跟随状态
let autoScrollEnabled = true;
let isFullscreen = false;
let segmentsData = [];
let activeSegmentIndex = -1;
let timeMappingData = [];
let fullscreenAudioSource = 'original';

// 生词确认状态
let confirmWords = [];
let confirmSegments = [];
let pendingPhraseText = '';
let wordLevels = {};
let levelNums = {};
let freqHighlightEnabled = false;
let originalConfirmWords = [];
let freqFilterApplied = false;
let freqFilterAddedWords = [];

// 句子翻译确认状态
let sentenceTranslations = {};
let sentenceTranslatedIndices = [];
let sentenceSegments = [];

// TTS 审查状态


// 历史音频状态
let historyPlayingTaskId = null;

// 删除确认状态
let pendingDeleteTaskId = null;

// 设置缓存
let settingsData = null;

// 播客搜索状态
let podcastSearchTimer = null;
let currentPodcastFeedId = null;

// 播客管理增强状态
let lastPodcastRssUrl = '';
let lastPodcastTitle = '';
let lastPodcastAuthor = '';
let lastPodcastImage = '';

// 搜索建议
let searchSuggestionTimer = null;

// Mini Player 播放状态
let miniPlayerState = null;
// { taskId, title, audioType: 'original'|'mixed', url, savedProcessMode }

// 视频任务状态
let isVideoTask = false;               // 当前任务是否为视频模式
let videoUploadedPath = '';
let videoUploadedName = '';

// 词频筛选策略
const filterStrategies = {
    'bnc_coca': {
        name: 'BNC/COCA 词频',
        getLevel: (word) => wordLevels[word.toLowerCase()] || null,
    },
};

// 全局生词库状态
let vocabCurrentPage = 1;
let vocabTotalPages = 1;
let vocabSearchTimer = null;

// ============================================================
// 步骤配置 —— 唯一权威来源
// task.js 和 settings.js 统一引用此对象
// ============================================================
const STEP_CONFIG = {
    // 完整步骤名列表（包含音频+视频所有步骤）
    names:     ['download','separate','transcribe','translate','confirm','synthesize','merge','subtitle','assemble'],
    // 步骤显示标签（与后端 step 值一一对应）
    labels:    {download:'下载',separate:'分离',transcribe:'转录',translate:'翻译',confirm:'确认',synthesize:'合成',merge:'混合',subtitle:'字幕',assemble:'组装'},
    // 顺序索引（separate 与 download 同 index=0，merge 与 mix 同 index=5）
    order:     {download:0, separate:0, transcribe:1, translate:2, confirm:3, synthesize:4, merge:5, subtitle:6, assemble:7, done:8},
    // 音频流水线步骤
    audio:     ['download','transcribe','translate','confirm','synthesize','merge'],
    // 视频流水线步骤
    video:     ['download','separate','transcribe','translate','confirm','synthesize','merge','subtitle','assemble'],
    // 后端字段标准化（confirm_sentence → confirm, mix → merge）
    normalize: function(step) {
        if (step === 'confirm_sentence') return 'confirm';
        if (step === 'mix' || step === 'vocabulary') return 'merge';
        return step;
    },
    // 获取步骤顺序索引（自动标准化）
    indexOf: function(step) {
        const ns = this.normalize(step);
        const idx = this.order[ns];
        return (idx !== undefined && idx >= 0) ? idx : 0;
    },
};
