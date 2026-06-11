"""
全局配置文件 - 英文播客生词替换工具
支持三种处理模式：
  - word_replace: 单词替换模式，将生词替换为中文 TTS
  - sentence_translate: 句子翻译模式，按比例均匀间隔选句，中英交替播放
  - smart_translate: 智能翻译模式（推荐），先识别生词，再整句翻译含生词的句子
"""
import os

# ========================
# 路径配置
# ========================
# BASE_DIR 指向项目根目录（core/ 的父目录）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 统一数据目录
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(DATA_DIR, "transcripts")
TTS_CACHE_DIR = os.path.join(DATA_DIR, "tts_cache")
RESULT_DIR = os.path.join(DATA_DIR, "results")
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")

# SQLite 数据库路径
DB_PATH = os.path.join(DATA_DIR, "bilimix.db")

# 自动确保数据目录存在
for _d in (DATA_DIR, OUTPUT_DIR, TTS_CACHE_DIR, RESULT_DIR, DOWNLOAD_DIR):
    os.makedirs(_d, exist_ok=True)

# ========================
# WhisperX 配置
# ========================
# whisperx 可执行文件路径（conda 环境中的绝对路径，避免 PATH 找不到）
WHISPERX_BIN = "/root/miniconda3/envs/whisperx_new/bin/whisperx"
WHISPERX_MODEL = "base"
WHISPERX_DEVICE = "cpu"
WHISPERX_COMPUTE_TYPE = "int8"
WHISPERX_BATCH_SIZE = 10
WHISPERX_LANGUAGE = "en"

# ========================
# Ollama 配置
# ========================
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "translategemma:12b"
# 生词识别时每批合并的句子数量（越大越快，但过大可能降低识别精度）
# 建议范围: 5~15，默认 8
LLM_BATCH_SIZE = 5
# 每次 LLM 调用最大输出 token 数
# qwen3.5 是推理模型，即使 think=False 仍可能输出大量推理 token
# 批量翻译多句时需更大额度，调小可降低单次响应时间但可能导致输出截断
LLM_NUM_PREDICT = 8192

# ========================
# 难度等级配置
# ========================
# 支持: CET-4, CET-6, IELTS-6, IELTS-7, ADVANCED
DIFFICULTY_LEVEL = "CET-4"

# ========================
# TTS 配置
# ========================
# TTS 引擎选择: "edge-tts" | "qwen3-tts" | "fish-speech"
#   edge-tts:     微软在线 TTS，免部署，音色统一
#   qwen3-tts:    本地声音克隆（x-vector），需 GPU/CPU 推理
#   fish-speech:  Fish Speech S2 Pro (s2.cpp HTTP 服务)，音色克隆最强，需先启动 s2.cpp
TTS_ENGINE = "qwen3-tts"

# --- Edge-TTS 配置 ---
# edge-tts 中文女声
# XiaoxiaoNeural: 活泼、有表现力，适合讲故事
# XiaoyiNeural: 温暖、亲切
# YunxiNeural: 男声、阳光
TTS_VOICE = "zh-CN-XiaoxiaoNeural"
# 语速调整: 略微加快以配合英文播客节奏 (范围: -50% ~ +100%)
TTS_RATE = "+8%"
# 音高调整: 稍微提高，让语气更生动 (范围: -100Hz ~ +100Hz)
TTS_PITCH = "+2Hz"
# TTS 输出采样率
TTS_SAMPLE_RATE = 24000

# --- Qwen3-TTS 配置（声音克隆模式） ---
# Qwen3-TTS 模型路径（本地已下载）
QWEN3_TTS_MODEL_PATH = "/root/Qwen3-TTS/models/Qwen/Qwen3-TTS-12Hz-1.7B-Base"
# Qwen3-TTS 所在 conda 环境的 Python 解释器
QWEN3_TTS_PYTHON = "/root/miniconda3/envs/qwen3-tts/bin/python"
# Qwen3-TTS 推理设备: "cuda:0" 或 "cpu"
QWEN3_TTS_DEVICE = "cpu"
# 参考音频时长（秒）：从原音频中截取多长的片段作为说话人参考
# 越长的参考音频能提供更丰富的音色特征，x-vector 推荐 5-10s
QWEN3_TTS_REF_DURATION = 10
# 自定义参考音频路径（可选）：手动指定一个高质量 WAV 文件作为声音克隆参考
# 设置后所有 TTS 句段共用此参考音频，不再从原始音频中自动提取
# 示例: QWEN3_TTS_CUSTOM_REF_AUDIO = "/path/to/speaker_ref.wav"
QWEN3_TTS_CUSTOM_REF_AUDIO = ""
# Segment 级别参考的最小时长（秒）：segment 短于此值时向前后扩展
# 降低到 1.5s，减少需要扩展的 segment，避免跨说话人音色污染
# x-vector 在 1-2s 音频上效果已足够，ICL 模式下则需更长时间（但需配合边界保护）
SEGMENT_REF_MIN_DURATION = 1.5
# 同一说话人连续句段的最大间隔（秒）：相邻 segment 间隔小于此值视为同一说话人
# 用于将连续短句分组，组内共享最长 segment 的参考音频，保证音色一致
# 单人演讲/播客：建议 0.8~1.2s（演讲者自然停顿）; 多人对话：建议 0.2~0.4s
SAME_SPEAKER_GAP = 0.8
# 声音克隆模式: False = x-vector 仅音色（默认），True = ICL 保留音色+语气/韵律
# x-vector 模式仅克隆音色，中文发音由模型独立生成，跨语言场景下更稳定、不产生乱码
# ICL 模式会提取参考音频的韵律特征（音高、语速、情感），但英语→中文时会冲突
QWEN3_TTS_ICL_MODE = False
# TTS 合成语言
QWEN3_TTS_LANGUAGE = "Chinese"
# TTS 合成失败时自动重试次数
QWEN3_TTS_RETRY_MAX = 3
# 全局自动重试次数（LLM 翻译 / 识别生词失败自动重试）
AUTO_RETRY_MAX = 3

# --- Fish Speech S2 Pro 配置（s2.cpp HTTP 服务） ---
# Fish Speech s2.cpp HTTP 服务器地址
FISH_SPEECH_HOST = "127.0.0.1"
# Fish Speech s2.cpp HTTP 服务器端口
FISH_SPEECH_PORT = 3030
# Fish Speech HTTP 请求超时（秒），CPU 推理较慢需更多时间
FISH_SPEECH_TIMEOUT = 120

# ========================
# 相邻词合并配置
# ========================
# 相邻替换点之间的最大间隔时间（秒）
# 如果两个替换点间隔小于此值，则认为它们紧挨着，会合并成一句话一起合成 TTS
# 设置为 0.0 表示只合并真正无间隔的相邻词，建议 0.0~0.3
ADJACENT_MERGE_GAP = 0.12

# ========================
# TTS 合成文本格式配置
# ========================
# 合成文本格式: "mixed" 或 "chinese_only"
#   "mixed": 英文+中文混合格式，如 "villain，反派"（推荐，语调更连贯，音色更一致）
#   "chinese_only": 纯中文格式，如 "反派"（旧模式）
TTS_TEXT_FORMAT = "chinese_only"

# ========================
# 处理模式
# ========================
# 处理模式: "word_replace" / "sentence_translate" / "smart_translate"
#   "word_replace": 识别生词，在原音频中将生词替换为中文TTS
#   "sentence_translate": 按比例均匀间隔选句，整句翻译，中英交替播放
#   "smart_translate": （推荐）先识别生词，再将含生词的句子整句翻译替换
PROCESS_MODE = "smart_translate"

# ========================
# 确认环节配置
# ========================
# 是否跳过确认环节（生词确认 / 翻译确认）
# True: 自动跳过确认，直接用 AI 识别/翻译结果继续处理（默认）
# False: 暂停等待用户在 Web 页面上手动确认后再继续
SKIP_CONFIRMATION = True

# ========================
# 智能翻译模式配置 (仅 smart_translate 模式生效)
# ========================
# 最大翻译句子占比上限: 0.0 ~ 1.0（安全阀）
# 当含生词的句子超过此比例时，优先选择生词密度最高的句子
# 例如：0.7 表示最多翻译 70% 的句子，避免几乎全篇翻译失去练听力意义
SMART_MAX_TRANSLATE_RATIO = 0.7

# ========================
# 句子翻译模式配置 (仅 sentence_translate 模式生效)
# ========================
# 中文句子替换占比: 0.0 ~ 1.0
#   1.0 = 每句英文都替换为中文翻译（纯中文）
#   0.5 = 每隔一句替换为中文（英文→中文→英文→中文交替）
#   0.33 = 每三句替换一句为中文
#   0.0 = 不替换（纯英文原声）
# 效果：被选中的句子用中文TTS替换英文原声，形成中英交替讲述
SENTENCE_CN_RATIO = 0.55
# 句子之间的静音间隔（毫秒），中英交替模式使用
SENTENCE_GAP_MS = 400
# 全翻译模式（100%）句间静音间隔（毫秒）
# 全翻译时所有句子都是中文 TTS，用短间隔保证连贯性
SENTENCE_FULL_GAP_MS = 250
# 中文翻译 TTS 是否克隆原声（否则用默认中文语音）
SENTENCE_TTS_VOICE_CLONE = True

# ========================
# 登录认证配置
# ========================
# 是否启用登录认证（False 则所有人可直接访问，无需登录）
AUTH_ENABLED = True
# 登录用户名
AUTH_USERNAME = "admin"
# 登录密码（明文存储在配置文件中，部署时请修改为强密码）
AUTH_PASSWORD = "bilimix2024"
# Flask session 密钥（用于加密 cookie，建议修改为随机字符串）
SECRET_KEY = "bilimix-secret-key-change-me"

# ========================
# 音频处理配置
# ========================
# 输出音频格式
OUTPUT_FORMAT = "mp3"
# 输出音频比特率
OUTPUT_BITRATE = "192k"
