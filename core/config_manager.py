"""
配置管理模块
负责配置的读取、更新和持久化（写回 config.py 文件）。
"""
import os
import re

from core import config


# 可更新的配置项映射: { json_key: (config_attr, type_converter) }
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
    "same_speaker_gap": ("SAME_SPEAKER_GAP", float),
    "auto_retry_max": ("AUTO_RETRY_MAX", int),
    # 登录认证
    "auth_enabled": ("AUTH_ENABLED", bool),
    "auth_username": ("AUTH_USERNAME", str),
    "auth_password": ("AUTH_PASSWORD", str),
    # 默认保留背景音乐
    "keep_bgm": ("KEEP_BGM", bool),
    # --- 参考音频配置 ---
    "ref_select_mode": ("REF_SELECT_MODE", str),
    "ref_min_duration": ("REF_MIN_DURATION", int),
    "ref_target_duration": ("REF_TARGET_DURATION", int),
    "ref_max_duration": ("REF_MAX_DURATION", int),
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
}


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
        "same_speaker_gap": getattr(config, "SAME_SPEAKER_GAP", 0.8),
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
        "ref_select_mode": getattr(config, "REF_SELECT_MODE", "speaker_local"),
        "ref_min_duration": getattr(config, "REF_MIN_DURATION", 2),
        "ref_target_duration": getattr(config, "REF_TARGET_DURATION", 5),
        "ref_max_duration": getattr(config, "REF_MAX_DURATION", 15),
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
    }


def update_config(data: dict) -> tuple:
    """
    更新配置（内存 + 文件）。

    Args:
        data: {json_key: value} 配置项

    Returns:
        (updated_attrs, error_msg): 成功更新的属性列表, 错误信息(None 表示无错误)
    """
    updated = []
    for key, value in data.items():
        if key in UPDATABLE_CONFIGS:
            attr, converter = UPDATABLE_CONFIGS[key]
            try:
                converted = converter(value)
                setattr(config, attr, converted)
                updated.append(attr)
            except (ValueError, TypeError) as e:
                print(f"[Config] 忽略无效值: {key}={value} ({e})")

    # 写回 config.py 文件
    if updated:
        try:
            _write_config_file(updated)
            print(f"[Config] 已更新 {len(updated)} 项: {', '.join(updated)}")
        except Exception as e:
            print(f"[Config] 写入文件失败: {e}")
            return updated, f"配置已更新到内存，但写入文件失败: {str(e)}"

    return updated, None


def _write_config_file(updated_attrs: list):
    """将配置变更写回 config.py 文件（逐行替换对应的赋值行）"""
    config_path = os.path.join(config.BASE_DIR, "core", "config.py")
    with open(config_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    for line in lines:
        replaced = False
        for attr in updated_attrs:
            pattern = rf'^({attr}\s*=\s*)(.+)$'
            m = re.match(pattern, line.rstrip())
            if m:
                val = getattr(config, attr)
                if isinstance(val, str):
                    new_val = f'"{val}"'
                elif isinstance(val, bool):
                    new_val = "True" if val else "False"
                elif isinstance(val, float):
                    new_val = str(val)
                else:
                    new_val = str(val)
                new_lines.append(f'{attr} = {new_val}\n')
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    with open(config_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
