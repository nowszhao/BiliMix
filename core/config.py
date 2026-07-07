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
WHISPERX_MODEL = "medium"
WHISPERX_DEVICE = "cpu"
WHISPERX_COMPUTE_TYPE = "int8"
WHISPERX_BATCH_SIZE = 10
WHISPERX_LANGUAGE = "en"
# CPU 推理线程数：whisperx 默认只用 4 个线程，多核机器应调大
# 建议设为物理核数或略少（留 1-2 核给系统），0 表示用 whisperx 默认值(4)
# 注意：超过物理核数收益递减，且会增加内存占用
WHISPERX_THREADS = 12

# --- WhisperX 说话人分离（Diarization）---
# 启用后每个 segment 会带 speaker 标签，用于精确匹配克隆音色
# 需要: conda 环境中安装 pyannote.audio
#       设置 HF_TOKEN 环境变量或下方 WHISPERX_HF_TOKEN
WHISPERX_DIARIZE = True
# HuggingFace Token（需同意 pyannote/speaker-diarization-3.1 模型协议）
# https://huggingface.co/pyannote/speaker-diarization-3.1
WHISPERX_HF_TOKEN = ""
# 最少说话人数（0=自动）
WHISPERX_MIN_SPEAKERS = 0
# 最多说话人数（0=自动）
WHISPERX_MAX_SPEAKERS = 0

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
# LLM 推理温度（0.0~1.0）
# 生词识别任务（JSON 输出）: 偏低更稳定，避免输出格式错误
# 翻译任务（口语地道性）: 偏高让译文更自然灵活，但过高会引入幻觉
LLM_IDENTIFY_TEMPERATURE = 0.3   # LLM 推理用
LLM_TRANSLATE_TEMPERATURE = 0.6  # 翻译用（提高口语自然度）

# ========================
# 难度等级配置
# ========================
# 支持: CET-4, CET-6, IELTS-6, IELTS-7, ADVANCED

# ========================
# TTS 配置（仅支持 Confucius4-TTS-CPU）
# ========================
TTS_ENGINE = "confucius-tts"

# --- Confucius4-TTS-CPU 配置（零样本多语言 TTS） ---
# Confucius4-TTS-CPU 项目根目录路径
# 默认在 BiliMix 同级目录下: ../Confucius4-TTS-CPU
CONFUCIUS4_TTS_ROOT = ""
# Confucius4-TTS-CPU 使用的 Python 解释器（需已安装 torch/transformers 等依赖）
# 留空则使用当前 Python 解释器 (sys.executable)
CONFUCIUS4_TTS_PYTHON = ""
# 推理设备: "cpu" 或 "cuda"
CONFUCIUS4_TTS_DEVICE = "cpu"
# 单条 TTS 合成超时（秒），CPU 推理较慢，长句可能需要 60-120s
CONFUCIUS4_TTS_PER_JOB_TIMEOUT = 6000
# T2S 采样温度（0.0~1.0），越高输出越多样，越低越稳定。语音风格不一致时调低
CONFUCIUS4_TTS_TEMPERATURE = 0.6
# 核采样概率阈值
CONFUCIUS4_TTS_TOP_P = 0.9
# Top-k 采样参数（0 表示不限制）
CONFUCIUS4_TTS_TOP_K = 0
# 束搜索宽度（1 = 贪心解码，确定性强）
CONFUCIUS4_TTS_NUM_BEAMS = 1
# 重复惩罚系数（越高重复越少）
CONFUCIUS4_TTS_REPETITION_PENALTY = 10.0
# 扩散步骤数（越高音质越好，25 为原始默认值）
CONFUCIUS4_TTS_N_TIMESTEPS = 40
# 无分类器引导强度（越高越紧跟参考音频的语速/音量风格）
CONFUCIUS4_TTS_INFERENCE_CFG_RATE = 0.9
# 并行 Worker 数量（利用多核并行合成，1 = 串行，建议 2-3）
CONFUCIUS4_TTS_NUM_WORKERS = 2

# --- 参考音频选取配置（声音克隆） ---
# 参考音频选取模式: "speaker_local" | "segment"
#   speaker_local: 每句优先用自身原声；自身过短时向同说话人相邻 segment 扩展边界
#                  （单次 ffmpeg 提取覆盖 [首段.start, 末段.end]），保证音色一致 +
#                  情绪/节奏随原句自然变化（推荐）
#   segment: 每句仅用自身原声，不扩展（旧行为，短句易音色漂移）
REF_SELECT_MODE = "speaker_local"
# 参考音频最大硬上限（秒）：超过则以目标句为中心截取
REF_MAX_DURATION = 30

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
# ========================
#   "word_replace": 识别生词，在原音频中将生词替换为中文TTS
#   "sentence_translate": 按比例均匀间隔选句，整句翻译，中英交替播放
#   "smart_translate": （推荐）先识别生词，再将含生词的句子整句翻译替换

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

# ========================
# 句子翻译模式配置 (仅 sentence_translate 模式生效)
# ========================
# 中文句子替换占比: 0.0 ~ 1.0
#   1.0 = 每句英文都替换为中文翻译（纯中文）
#   0.5 = 每隔一句替换为中文（英文→中文→英文→中文交替）
#   0.33 = 每三句替换一句为中文
#   0.0 = 不替换（纯英文原声）
# 效果：被选中的句子用中文TTS替换英文原声，形成中英交替讲述
SENTENCE_CN_RATIO = 1.0
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
