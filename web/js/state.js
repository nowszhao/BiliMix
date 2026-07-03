/* ============================================================
   BiliMix — 全局状态模块
   ============================================================ */

let currentTaskId = null;
let currentTaskTitle = '';
let pollTimer = null;
let tasks_url = '';
let currentProcessMode = 'word_replace';
let currentInputMode = 'search';
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
