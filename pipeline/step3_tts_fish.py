"""
Step 3 (Fish Speech S2 Pro): 声音克隆语音合成模块
通过 s2.cpp HTTP 服务调用 Fish Speech S2 Pro 进行跨语言声音克隆。

架构:
  BiliMix ──POST /generate──→ s2.cpp HTTP server (localhost:3030)
                            ├── text: 待合成中文文本
                            ├── prompt_audio: 参考音频文件路径（服务器本地）
                            └── prompt_text: 参考音频对应文本
                            ←── WAV audio bytes

前置条件:
  1. 已安装 s2.cpp: https://github.com/rodrigomatta/s2.cpp
  2. 已下载 GGUF 模型到 /root/s2.cpp/models/
  3. 已启动 HTTP 服务:
     /root/s2.cpp/build/s2 --model s2-pro-q4_k_m.gguf --server --port 3030

特点:
  - 跨语言声音克隆质量远超 Qwen3-TTS x-vector
  - 无乱码风险（模型原生多语言训练 80+ 语言）
  - 纯 C++ 推理，CPU 可运行
"""
import hashlib
import os
import time

import requests

from core import config


def _get_fish_url() -> str:
    """获取 Fish Speech HTTP 服务地址"""
    host = getattr(config, "FISH_SPEECH_HOST", "127.0.0.1")
    port = getattr(config, "FISH_SPEECH_PORT", 3030)
    return f"http://{host}:{port}"


def _check_server_health() -> bool:
    """检查 Fish Speech 服务是否在线"""
    try:
        url = f"{_get_fish_url()}/health"
        resp = requests.get(url, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def synthesize_sentences_with_fish_tts(
    segments: list,
    translated_indices: list,
    translations: dict,
    audio_path: str,
    cache_dir: str,
    ref_audio_map: dict = None,
    ref_source_map: dict = None,
    cancel_check=None,
    progress_cb=None,
    task_id: str = None,
) -> dict:
    """
    使用 Fish Speech S2 Pro 为句子翻译模式合成中文 TTS 音频。

    每个被翻译的句子独立调用 Fish Speech 合成，使用对应英文句子的原声作为
    声音克隆参考。s2.cpp 会从参考音频中提取说话人特征并应用到中文合成。

    Args:
        segments: WhisperX segments 列表
        translated_indices: 需要翻译的 segment 索引列表
        translations: {segment_index: chinese_text} 翻译结果
        audio_path: 原始音频文件路径（备用，Fish Speech 不加载整文件）
        cache_dir: TTS 缓存目录
        ref_audio_map: {segment_index: ref_audio_path} 说话人参考音频映射
        ref_source_map: {segment_index: source_segment_index} ICL 来源
        cancel_check: 终止检查回调
        progress_cb: 进度回调 (current, total)

    Returns:
        dict: {segment_index: tts_wav_path} 映射
    """
    os.makedirs(cache_dir, exist_ok=True)

    if not translated_indices or not translations:
        print("[Step3-Fish] 没有需要合成的句子")
        return {}

    # 校验 Fish Speech 服务是否在线
    if not _check_server_health():
        msg = ("Fish Speech 服务不可达，请先启动 s2.cpp:\n"
               "  /root/s2.cpp/build/s2 --model s2-pro-q4_k_m.gguf "
               "--server --port 3030")
        print(f"[Step3-Fish] ❌ {msg}")
        raise RuntimeError(msg)

    print(f"[Step3-Fish] 准备为 {len(translated_indices)} 个句子合成中文 TTS")

    fish_url = f"{_get_fish_url()}/generate"
    timeout = getattr(config, "FISH_SPEECH_TIMEOUT", 120)
    total = len(translated_indices)

    tts_audio_map = {}

    for i, seg_idx in enumerate(translated_indices):
        if cancel_check and cancel_check():
            raise InterruptedError("任务已被用户终止")

        chinese_text = translations.get(seg_idx, "")
        if not chinese_text:
            print(f"  [跳过] seg[{seg_idx}] 翻译文本为空")
            continue

        # 参考音频及文本
        ref_audio = ""
        ref_text = ""
        if ref_audio_map:
            ref_audio = ref_audio_map.get(seg_idx, "")
        if ref_audio and ref_source_map and seg_idx < len(segments):
            source_seg = ref_source_map.get(seg_idx, seg_idx)
            if source_seg < len(segments):
                ref_text = segments[source_seg].get("text", "").strip()

        if not ref_audio or not os.path.isfile(ref_audio):
            print(f"  [跳过] seg[{seg_idx}] 无有效参考音频: {ref_audio}")
            continue

        # 缓存 key: 文本 + 参考音频路径
        cache_key = hashlib.md5(
            f"fish_{chinese_text}|{ref_audio}".encode()
        ).hexdigest()[:12]
        output_path = os.path.join(cache_dir, f"fish_tts_{cache_key}.wav")

        # 检查缓存
        if os.path.exists(output_path):
            tts_audio_map[seg_idx] = output_path
            print(f"  [缓存] seg[{seg_idx}] {chinese_text[:20]}...")
            if progress_cb:
                progress_cb(i + 1, total)
            continue

        if progress_cb:
            progress_cb(i, total)

        # 调用 Fish Speech HTTP 服务
        print(f"  [{i+1}/{total}] seg[{seg_idx}] 合成: {chinese_text[:30]}... "
              f"(ref: {os.path.basename(ref_audio)})")

        try:
            t0 = time.time()
            resp = requests.post(
                fish_url,
                json={
                    "text": chinese_text,
                    "prompt_audio": ref_audio,
                    "prompt_text": ref_text,
                },
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )

            if resp.status_code != 200:
                # 尝试解析错误信息
                try:
                    err_detail = resp.json().get("error", resp.text[:200])
                except Exception:
                    err_detail = resp.text[:200]
                print(f"  [失败] seg[{seg_idx}] HTTP {resp.status_code}: {err_detail}")
                continue

            # 保存合成音频
            with open(output_path, "wb") as f:
                f.write(resp.content)

            elapsed = time.time() - t0
            print(f"  [完成] seg[{seg_idx}] -> {os.path.basename(output_path)} "
                  f"({len(resp.content)/1024:.0f}KB, {elapsed:.1f}s)")
            tts_audio_map[seg_idx] = output_path

        except requests.Timeout:
            print(f"  [超时] seg[{seg_idx}] 请求超时 ({timeout}s): {chinese_text[:30]}...")
        except requests.ConnectionError:
            print(f"  [连接失败] seg[{seg_idx}] Fish Speech 服务断连")
            break
        except Exception as e:
            print(f"  [异常] seg[{seg_idx}] {e}")

        if progress_cb:
            progress_cb(i + 1, total)

    if progress_cb:
        progress_cb(len(tts_audio_map), total)

    print(f"[Step3-Fish] 合成完成: {len(tts_audio_map)}/{total} 条")
    return tts_audio_map
