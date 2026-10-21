"""
配置管理模块
负责配置的读取、更新和独立 JSON 持久化（不修改源码）。
"""

from core import config


# 可更新的配置项映射: { json_key: (config_attr, type_converter) }
from core.settings_store import UPDATABLE_CONFIGS, save_settings, validate_settings
from threading import RLock

_UPDATE_LOCK = RLock()


def get_all_config() -> dict:
    """返回前端需要的全部配置"""
    return {
        # 处理模式
        "skip_confirmation": getattr(config, "SKIP_CONFIRMATION", True),
        # 智能翻译
        # 句子翻译
        "sentence_cn_ratio": getattr(config, "SENTENCE_CN_RATIO", 0.9),
        "sentence_gap_ms": getattr(config, "SENTENCE_GAP_MS", 400),
        "sentence_full_gap_ms": getattr(config, "SENTENCE_FULL_GAP_MS", 250),
        # TTS
        "tts_engine": getattr(config, "TTS_ENGINE", "confucius-tts"),
        "tts_text_format": getattr(config, "TTS_TEXT_FORMAT", "chinese_only"),
        # WhisperX
        "whisperx_model": getattr(config, "WHISPERX_MODEL", "base"),
        "whisperx_device": getattr(config, "WHISPERX_DEVICE", "cpu"),
        "whisperx_language": getattr(config, "WHISPERX_LANGUAGE", "en"),
        "whisperx_threads": getattr(config, "WHISPERX_THREADS", 4),
        # Demucs
        "demucs_timeout": getattr(config, "DEMUCS_TIMEOUT", 1800),
        # Ollama
        "ollama_base_url": getattr(config, "OLLAMA_BASE_URL", "http://localhost:11434"),
        "ollama_model": getattr(config, "OLLAMA_MODEL", "qwen3:8b"),
        "llm_batch_size": getattr(config, "LLM_BATCH_SIZE", 8),
        "llm_num_predict": getattr(config, "LLM_NUM_PREDICT", 8192),
        # 音频
        "output_format": getattr(config, "OUTPUT_FORMAT", "mp3"),
        "output_bitrate": getattr(config, "OUTPUT_BITRATE", "192k"),
        # Qwen3-TTS
        # Confucius4-TTS-CPU
        "confucius_tts_device": getattr(config, "CONFUCIUS4_TTS_DEVICE", "cpu"),
        "confucius_tts_temperature": getattr(config, "CONFUCIUS4_TTS_TEMPERATURE", 0.8),
        "confucius_tts_top_p": getattr(config, "CONFUCIUS4_TTS_TOP_P", 0.8),
        "confucius_tts_top_k": getattr(config, "CONFUCIUS4_TTS_TOP_K", 30),
        "confucius_tts_num_beams": getattr(config, "CONFUCIUS4_TTS_NUM_BEAMS", 3),
        "confucius_tts_repetition_penalty": getattr(config, "CONFUCIUS4_TTS_REPETITION_PENALTY", 10.0),
        "confucius_tts_n_timesteps": getattr(config, "CONFUCIUS4_TTS_N_TIMESTEPS", 25),
        "confucius_tts_inference_cfg_rate": getattr(config, "CONFUCIUS4_TTS_INFERENCE_CFG_RATE", 0.7),
        "confucius_tts_num_workers": getattr(config, "CONFUCIUS4_TTS_NUM_WORKERS", 2),
        # 重试
        "auto_retry_max": getattr(config, "AUTO_RETRY_MAX", 3),
        # 登录认证
        "auth_enabled": getattr(config, "AUTH_ENABLED", True),
        "auth_username": getattr(config, "AUTH_USERNAME", "admin"),
        "auth_password": getattr(config, "AUTH_PASSWORD", "bilimix2024"),
        # 默认保留背景音乐
        "keep_bgm": getattr(config, "KEEP_BGM", False),
        # 参考音频配置
        # 句子翻译
        "sentence_tts_voice_clone": getattr(config, "SENTENCE_TTS_VOICE_CLONE", True),
        # LLM 翻译
        "llm_translate_temperature": getattr(config, "LLM_TRANSLATE_TEMPERATURE", 0.3),
        # 音频混音
        "tts_target_dbfs": getattr(config, "TTS_TARGET_DBFS", -20.0),
        "mixer_default_gap_ms": getattr(config, "MIXER_DEFAULT_GAP_MS", 150),
        "mixer_fade_ms": getattr(config, "MIXER_FADE_MS", 60),
        "mixer_bgm_gain_db": getattr(config, "MIXER_BGM_GAIN_DB", -10.0),
        # 转录缺口补录
        "transcribe_gap_min_seconds": getattr(config, "TRANSCRIBE_GAP_MIN_SECONDS", 3.0),
        "transcribe_gap_voice_dbfs": getattr(config, "TRANSCRIBE_GAP_VOICE_DBFS", -35.0),
        # 视频组装
        "ffmpeg_threads_cap": getattr(config, "FFMPEG_THREADS_CAP", 8),
        # 新建视频任务时字幕字号的默认值（-1/0 表示按视频高度自动）
        "sub_font_size_default": getattr(config, "ASS_FONT_SIZE_DEFAULT", 40),
        # 水印
        "watermark_enabled": getattr(config, "WATERMARK_ENABLED", True),
        "watermark_text": getattr(config, "WATERMARK_TEXT", "BiliMix"),
        "watermark_opacity": getattr(config, "WATERMARK_OPACITY", 0.5),
        "watermark_font": getattr(config, "WATERMARK_FONT", ""),
    }


def update_config(data: dict) -> list:
    """Validate and persist a complete update before exposing it to the process."""
    if not isinstance(data, dict):
        raise ValueError('Configuration must be an object')
    unknown = set(data) - UPDATABLE_CONFIGS.keys()
    if unknown:
        raise ValueError('Unknown configuration fields: ' + ', '.join(sorted(unknown)))
    changed = validate_settings({UPDATABLE_CONFIGS[key][0]: value for key, value in data.items()})
    if not changed:
        return []
    with _UPDATE_LOCK:
        initial = {attr: getattr(config, attr) for attr, _ in UPDATABLE_CONFIGS.values()
                   if hasattr(config, attr)}
        save_settings(config.SETTINGS_FILE, changed, initial=initial)
        for attr, value in changed.items():
            setattr(config, attr, value)
    return list(changed)
