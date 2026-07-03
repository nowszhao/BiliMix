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
