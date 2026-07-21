# ============================================================
# BiliMix config_local.py 模板
#
# 用法：
#   1. 推荐：运行 ./setup.sh，会自动从本文件生成 core/config_local.py
#      并替换 __BILIMIX_PYTHON__ / __CONFUCIUS_ROOT__ 为检测到的路径。
#   2. 手动：cp core/config_local.example.py core/config_local.py 然后编辑。
#
# 本文件提交到仓库；core/config_local.py 被 .gitignore 忽略，不会提交。
# ============================================================

# --- WhisperX ---
# setup.sh 会 pip install whisperx 到 bilimix conda env，激活后即在 PATH
# 如需指定绝对路径，改为 "/opt/homebrew/bin/whisperx" 等
WHISPERX_BIN = "whisperx"
WHISPERX_MODEL = "small"            # small=快（CPU/M1 推荐）；medium=准；large-v3=最准但慢
WHISPERX_DEVICE = "cpu"
WHISPERX_COMPUTE_TYPE = "int8"      # int8=CPU 推荐；float16=GPU
WHISPERX_LANGUAGE = "en"
WHISPERX_THREADS = 8                # CPU 核数
WHISPERX_BATCH_SIZE = 10
WHISPERX_DIARIZE = False            # 设 True 并配置 HF_TOKEN 启用说话人分离

# --- Demucs 人声分离 ---
# demucs 子进程超时（秒）。CPU 推理长音频可能需要 20-40 分钟。
# 默认 1800（30 分钟）。超时后任务跳过人声分离，用原始音频继续。
DEMUCS_TIMEOUT = 1800

# --- Ollama ---
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "translategemma:4b"   # 或 translategemma:12b（更准，需更多内存）
LLM_BATCH_SIZE = 5
LLM_NUM_PREDICT = 4096

# --- Confucius4-TTS ---
# setup.sh 替换为 ../Confucius4-TTS-CPU 的绝对路径
CONFUCIUS4_TTS_ROOT = "__CONFUCIUS_ROOT__"
# setup.sh 替换为 bilimix env 的 python（即 sys.executable）
# 留空也会 fallback 到 sys.executable
CONFUCIUS4_TTS_PYTHON = "__BILIMIX_PYTHON__"
CONFUCIUS4_TTS_DEVICE = "cpu"
CONFUCIUS4_TTS_N_TIMESTEPS = 15     # CPU 推荐 15；GPU 可设 25
CONFUCIUS4_TTS_NUM_WORKERS = 1      # CPU 单 worker；GPU 可设 2-4
CONFUCIUS4_TTS_TEMPERATURE = 0.3
CONFUCIUS4_TTS_TOP_P = 0.9
CONFUCIUS4_TTS_PER_JOB_TIMEOUT = 6000

# --- 任务/输出 ---
SKIP_CONFIRMATION = True            # True=自动跳过翻译确认，直接合成
SENTENCE_CN_RATIO = 1.0             # 中文朗读比例（1.0=全部翻译）
SENTENCE_TTS_VOICE_CLONE = True     # True=用原音色克隆 TTS
KEEP_BGM = False                    # 默认不保留背景音（可任务级覆盖）

OUTPUT_FORMAT = "mp3"
OUTPUT_BITRATE = "192k"

AUTH_ENABLED = False                # Web 服务是否启用登录认证

# --- 视频组装 ---
# ffmpeg 编码线程数上限
# FFMPEG_THREADS_CAP = 8
# 视频组装超时（秒），长视频建议设为 28800（8小时）
# VIDEO_ASSEMBLE_TIMEOUT = 14400
# libx264 编码预设（veryfast/ultrafast，越快文件越大）
# VIDEO_X264_PRESET = "veryfast"
# 视频比特率（720p 推荐 500k-1500k，讲稿类可用低码率）
# VIDEO_BITRATE = "1500k"

# --- 分块并行组装（长视频自动启用） ---
# 当 TTS 句子数超过此值时自动分块（设 0 禁用）
# VIDEO_MAX_CONCAT_SEGMENTS = 200
# 每块句子数
# VIDEO_BLOCK_SIZE = 50
# 并行块数
# VIDEO_BLOCK_WORKERS = 2
