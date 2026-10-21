"""Atomic, non-executable persistence for Web settings (never edit source files)."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import tempfile
import threading

_LOCK = threading.RLock()

UPDATABLE_CONFIGS = {
    "skip_confirmation": ("SKIP_CONFIRMATION", bool),
    "sentence_cn_ratio": ("SENTENCE_CN_RATIO", float),
    "sentence_gap_ms": ("SENTENCE_GAP_MS", int),
    "sentence_full_gap_ms": ("SENTENCE_FULL_GAP_MS", int),
    "tts_engine": ("TTS_ENGINE", str),
    "tts_text_format": ("TTS_TEXT_FORMAT", str),
    "whisperx_model": ("WHISPERX_MODEL", str),
    "whisperx_device": ("WHISPERX_DEVICE", str),
    "whisperx_language": ("WHISPERX_LANGUAGE", str),
    "whisperx_threads": ("WHISPERX_THREADS", int),
    "demucs_timeout": ("DEMUCS_TIMEOUT", int),
    "ollama_base_url": ("OLLAMA_BASE_URL", str),
    "ollama_model": ("OLLAMA_MODEL", str),
    "llm_batch_size": ("LLM_BATCH_SIZE", int),
    "llm_num_predict": ("LLM_NUM_PREDICT", int),
    "output_format": ("OUTPUT_FORMAT", str),
    "output_bitrate": ("OUTPUT_BITRATE", str),
    "confucius_tts_device": ("CONFUCIUS4_TTS_DEVICE", str),
    "confucius_tts_temperature": ("CONFUCIUS4_TTS_TEMPERATURE", float),
    "confucius_tts_top_p": ("CONFUCIUS4_TTS_TOP_P", float),
    "confucius_tts_top_k": ("CONFUCIUS4_TTS_TOP_K", int),
    "confucius_tts_num_beams": ("CONFUCIUS4_TTS_NUM_BEAMS", int),
    "confucius_tts_repetition_penalty": ("CONFUCIUS4_TTS_REPETITION_PENALTY", float),
    "confucius_tts_n_timesteps": ("CONFUCIUS4_TTS_N_TIMESTEPS", int),
    "confucius_tts_inference_cfg_rate": ("CONFUCIUS4_TTS_INFERENCE_CFG_RATE", float),
    "confucius_tts_num_workers": ("CONFUCIUS4_TTS_NUM_WORKERS", int),
    "auto_retry_max": ("AUTO_RETRY_MAX", int),
    # 登录认证
    "auth_enabled": ("AUTH_ENABLED", bool),
    "auth_username": ("AUTH_USERNAME", str),
    "auth_password": ("AUTH_PASSWORD", str),
    # 默认保留背景音乐
    "keep_bgm": ("KEEP_BGM", bool),
    # --- 参考音频配置 ---
    # --- 句子翻译 ---
    "sentence_tts_voice_clone": ("SENTENCE_TTS_VOICE_CLONE", bool),
    # --- LLM 翻译 ---
    "llm_translate_temperature": ("LLM_TRANSLATE_TEMPERATURE", float),
    # --- 音频混音 ---
    "tts_target_dbfs": ("TTS_TARGET_DBFS", float),
    "mixer_default_gap_ms": ("MIXER_DEFAULT_GAP_MS", int),
    "mixer_fade_ms": ("MIXER_FADE_MS", int),
    "mixer_bgm_gain_db": ("MIXER_BGM_GAIN_DB", float),
    # --- 转录缺口补录 ---
    "transcribe_gap_min_seconds": ("TRANSCRIBE_GAP_MIN_SECONDS", float),
    "transcribe_gap_voice_dbfs": ("TRANSCRIBE_GAP_VOICE_DBFS", float),
    # --- 视频组装 ---
    "ffmpeg_threads_cap": ("FFMPEG_THREADS_CAP", int),
    # 新建视频任务时字幕字号的默认值（-1/0 表示按视频高度自动）
    "sub_font_size_default": ("ASS_FONT_SIZE_DEFAULT", int),
    # --- 水印 ---
    "watermark_enabled": ("WATERMARK_ENABLED", bool),
    "watermark_text": ("WATERMARK_TEXT", str),
    "watermark_opacity": ("WATERMARK_OPACITY", float),
    # 水印字体文件（留空 = 自动探测中文字体）
    "watermark_font": ("WATERMARK_FONT", str),
}

_ALLOWED = {attr:convert for attr,convert in UPDATABLE_CONFIGS.values()}


def validate_settings(values):
    if not isinstance(values,dict):
        raise ValueError('Settings must be an object')
    result = {}
    for attr,value in values.items():
        convert = _ALLOWED.get(attr)
        if convert is None:
            raise ValueError('Unknown persisted setting: '+attr)
        valid = (isinstance(value,(int,float)) and not isinstance(value,bool)) if convert is float else type(value) is convert
        if convert is float and valid:
            valid = math.isfinite(value)
        if not valid:
            raise ValueError('Invalid persisted setting type: '+attr)
        result[attr] = convert(value)
    return result


def load_settings(path):
    path = Path(path)
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document,dict) or document.get('schema_version') != 1:
        raise ValueError('Unsupported settings file schema')
    return validate_settings(document.get('settings'))


def save_settings(path,updated,initial=None):
    """Merge changed fields under a lock; snapshot defaults only on first save."""
    path = Path(path)
    updated = validate_settings(updated)
    with _LOCK:
        current = load_settings(path) if path.exists() else validate_settings(initial or {})
        current.update(updated)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                    prefix='.bilimix-settings-',suffix='.json',delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'schema_version':1,'settings':current},stream,ensure_ascii=False,indent=2)
                stream.write('\n');stream.flush();os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary,path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return current
