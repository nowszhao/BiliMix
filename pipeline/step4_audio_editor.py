"""
Step 4: 音频编辑模块
根据时间戳将原始音频中的难词/短语替换为中文语音
"""
import os
import sys

from pydub import AudioSegment

from core import config


def load_audio(audio_path: str) -> AudioSegment:
    """加载音频文件"""
    print(f"[Step4] 加载原始音频: {audio_path}")
    return AudioSegment.from_file(audio_path)


def apply_replacements(
    original_audio: AudioSegment,
    replacements: list,
    tts_audio_map: dict,
    tts_index_map: dict = None,
) -> tuple:
    """
    执行音频替换：将原音频中的指定时间段替换为中文 TTS 音频。

    支持两种 TTS 映射模式：
    1. tts_index_map (qwen3-tts 模式): {group_key: {"path": ..., "indices": [idx, ...]}}
       - 支持相邻词合并组：一段 TTS 音频替换多个连续替换点
    2. tts_audio_map (edge-tts 模式): {chinese_text: tts_audio_path}

    方案 B：直接替换，允许总时长变化。

    同时生成时间映射表 (time_mapping)，记录混合音频时间段到原始音频时间段的对应关系，
    用于前端播放混合音频时精确定位转录原文的句子行。

    Args:
        original_audio: 原始音频
        replacements: 替换计划列表（已按时间排序）
        tts_audio_map: {chinese_text: tts_audio_path} 映射 (edge-tts 模式)
        tts_index_map: {group_key: {"path": ..., "indices": [...]}} 映射 (qwen3-tts 模式，可选)

    Returns:
        tuple: (AudioSegment 替换后的新音频, list 时间映射表)
            时间映射表格式: [{"mixed_start", "mixed_end", "orig_start", "orig_end", "type"}, ...]
            type: "original" 表示原始音频片段, "tts" 表示插入的中文 TTS 片段
    """
    if not replacements:
        print("[Step4] 没有需要替换的内容")
        return original_audio, []

    print(f"[Step4] 开始音频拼接，共 {len(replacements)} 处替换")

    # 统一音频参数
    target_sr = original_audio.frame_rate
    target_channels = original_audio.channels

    # 如果有 tts_index_map，先建立"哪些索引属于合并组"的查找表
    # merged_group_for[idx] = group_key，表示该索引属于某个合并组
    # merged_group_leader[idx] = True，表示该索引是合并组的第一个元素（负责插入 TTS）
    merged_group_for = {}
    merged_group_leader = {}
    if tts_index_map:
        for group_key, info in tts_index_map.items():
            indices = info.get("indices", [])
            if len(indices) > 1:
                for idx in indices:
                    merged_group_for[idx] = group_key
                merged_group_leader[indices[0]] = group_key

    result = AudioSegment.empty()
    time_mapping = []  # 时间映射表
    mixed_pos = 0  # 混合音频的当前位置（毫秒）
    current_pos = 0  # 原始音频的当前位置（毫秒）

    for idx, rep in enumerate(replacements):
        start_ms = int(rep["start"] * 1000)
        end_ms = int(rep["end"] * 1000)

        # 如果是合并组中的非首元素，跳过（它的时间段已被首元素的 TTS 覆盖）
        if idx in merged_group_for and idx not in merged_group_leader:
            # 更新 current_pos 到该替换的 end
            current_pos = max(current_pos, end_ms)
            continue

        # 安全保护：跳过与前一个替换时间重叠的条目
        # 当 start_ms 已经被 current_pos 越过时，说明该位置已被前面的替换覆盖
        if start_ms < current_pos:
            overlap_ms = current_pos - start_ms
            print(f"  [跳过] 替换 '{rep['english']}' @ {rep['start']:.2f}s "
                  f"与前一个替换重叠 {overlap_ms}ms，跳过")
            # 仍然推进 current_pos（如果 end_ms 更远的话）
            current_pos = max(current_pos, end_ms)
            continue

        # 1. 添加替换点之前的原始音频
        if start_ms > current_pos:
            segment_len = start_ms - current_pos
            result += original_audio[current_pos:start_ms]
            # 记录原始音频片段的映射
            time_mapping.append({
                "mixed_start": round(mixed_pos / 1000.0, 3),
                "mixed_end": round((mixed_pos + segment_len) / 1000.0, 3),
                "orig_start": round(current_pos / 1000.0, 3),
                "orig_end": round(start_ms / 1000.0, 3),
                "type": "original",
            })
            mixed_pos += segment_len

        # 2. 查找 TTS 音频文件
        tts_path = None
        orig_start_for_tts = start_ms  # 记录这段 TTS 替换了原始音频的哪段

        if idx in merged_group_leader:
            # 合并组首元素：用合并 TTS 音频替换从第一个到最后一个替换点的整段
            group_key = merged_group_leader[idx]
            info = tts_index_map[group_key]
            tts_path = info["path"]
            # 把 end_ms 推进到合并组最后一个替换点的 end
            last_idx = info["indices"][-1]
            end_ms = int(replacements[last_idx]["end"] * 1000)
        elif tts_index_map:
            # 非合并组的单独替换（qwen3-tts 模式）
            single_key = f"single_{idx}"
            info = tts_index_map.get(single_key)
            if info:
                tts_path = info["path"]
        # 回退到 edge-tts 模式
        if not tts_path and rep["chinese"] in tts_audio_map:
            tts_path = tts_audio_map[rep["chinese"]]

        if tts_path and os.path.exists(tts_path):
            tts_audio = AudioSegment.from_file(tts_path)
            # 统一采样率和声道
            tts_audio = tts_audio.set_frame_rate(target_sr).set_channels(target_channels)
            tts_len = len(tts_audio)
            result += tts_audio
            # 记录 TTS 片段的映射（TTS 片段对应原始音频中被替换的时间段）
            time_mapping.append({
                "mixed_start": round(mixed_pos / 1000.0, 3),
                "mixed_end": round((mixed_pos + tts_len) / 1000.0, 3),
                "orig_start": round(orig_start_for_tts / 1000.0, 3),
                "orig_end": round(end_ms / 1000.0, 3),
                "type": "tts",
            })
            mixed_pos += tts_len
        else:
            # 如果 TTS 文件不存在，保留原始音频
            print(f"  [警告] TTS文件不存在: {rep['chinese']}, 保留原始音频")
            segment_len = end_ms - start_ms
            result += original_audio[start_ms:end_ms]
            time_mapping.append({
                "mixed_start": round(mixed_pos / 1000.0, 3),
                "mixed_end": round((mixed_pos + segment_len) / 1000.0, 3),
                "orig_start": round(start_ms / 1000.0, 3),
                "orig_end": round(end_ms / 1000.0, 3),
                "type": "original",
            })
            mixed_pos += segment_len

        current_pos = end_ms

    # 3. 添加最后一段原始音频
    if current_pos < len(original_audio):
        segment_len = len(original_audio) - current_pos
        result += original_audio[current_pos:]
        time_mapping.append({
            "mixed_start": round(mixed_pos / 1000.0, 3),
            "mixed_end": round((mixed_pos + segment_len) / 1000.0, 3),
            "orig_start": round(current_pos / 1000.0, 3),
            "orig_end": round(len(original_audio) / 1000.0, 3),
            "type": "original",
        })

    print(f"[Step4] 拼接完成: 原始 {len(original_audio)/1000:.1f}s -> 新 {len(result)/1000:.1f}s")
    print(f"[Step4] 生成时间映射表: {len(time_mapping)} 个片段")
    return result, time_mapping


def export_audio(audio: AudioSegment, output_path: str):
    """
    导出音频文件。

    Args:
        audio: AudioSegment 对象
        output_path: 输出路径
    """
    fmt = config.OUTPUT_FORMAT
    bitrate = config.OUTPUT_BITRATE

    print(f"[Step4] 导出音频: {output_path}")
    audio.export(output_path, format=fmt, bitrate=bitrate)
    print(f"[Step4] 导出完成! 大小: {os.path.getsize(output_path) / 1024:.1f} KB")


if __name__ == "__main__":
    print("此模块需要配合其他步骤使用，请运行 main.py")
