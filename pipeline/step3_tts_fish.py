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
    """检查 Fish Speech 服务是否在线（TCP 端口可达即可）"""
    import socket
    host = getattr(config, "FISH_SPEECH_HOST", "127.0.0.1")
    port = getattr(config, "FISH_SPEECH_PORT", 3030)
    try:
        sock = socket.create_connection((host, port), timeout=3)
        sock.close()
        return True
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
    timeout = getattr(config, "FISH_SPEECH_TIMEOUT", 300)
    total = len(translated_indices)

    tts_audio_map = {}

    for i, seg_idx in enumerate(translated_indices):
        if cancel_check and cancel_check():
            raise InterruptedError("任务已被用户终止")

        chinese_text = translations.get(seg_idx, "")
        if not chinese_text:
            raise RuntimeError(
                f"seg[{seg_idx}] 翻译文本为空，无法合成，任务中断")

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
            raise RuntimeError(
                f"seg[{seg_idx}] 缺少参考音频 ({ref_audio or '(空)'})，无法合成，任务中断")

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

        # 调用 Fish Speech HTTP 服务（无限重试直到成功或用户终止）
        print(f"  [{i+1}/{total}] seg[{seg_idx}] 合成: {chinese_text[:30]}... "
              f"(ref: {os.path.basename(ref_audio)})")

        # 预先构建 form_data（内容不变，每次重试复用）
        form_data = {"text": (None, chinese_text)}
        form_data["prompt_text"] = (None, ref_text if ref_text else chinese_text)
        with open(ref_audio, "rb") as ref_f:
            ref_bytes = ref_f.read()
        form_data["prompt_audio"] = (os.path.basename(ref_audio),
                                      ref_bytes, "audio/wav")

        attempt = 0
        sentence_start = time.time()
        SENTENCE_MAX_WALL = 600  # 单句最大总耗时 10 分钟
        while True:
            if cancel_check and cancel_check():
                raise InterruptedError("任务已被用户终止")

            elapsed_total = time.time() - sentence_start
            if elapsed_total > SENTENCE_MAX_WALL:
                raise RuntimeError(
                    f"句子 seg[{seg_idx}] 合成超时 "
                    f"({elapsed_total:.0f}s > {SENTENCE_MAX_WALL}s)，"
                    f"请点击「重试」继续断点续传")

            attempt += 1
            try:
                t0 = time.time()
                resp = requests.post(
                    fish_url,
                    files=form_data,
                    timeout=timeout,
                )

                if resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    elapsed = time.time() - t0
                    retry_note = f" (重试{attempt-1}次)" if attempt > 1 else ""
                    print(f"  [完成] seg[{seg_idx}] -> "
                          f"{os.path.basename(output_path)} "
                          f"({len(resp.content)/1024:.0f}KB, {elapsed:.1f}s)"
                          f"{retry_note}")
                    tts_audio_map[seg_idx] = output_path
                    break

                # 503 或超时后可能出现的其他状态 → 等一会重试
                # s2.cpp 是单 worker，503 意味着服务器正在处理上一个请求，
                # 需要给足够时间完成当前任务后再重试
                err_msg = resp.text[:100] if resp.text else f"HTTP {resp.status_code}"
                wait = min(30 + attempt * 15, 90)  # 45s, 60s, 75s, 90s
                print(f"  [重试] seg[{seg_idx}] {err_msg}，{wait}s 后重试 (第{attempt}次)")
                time.sleep(wait)

            except requests.Timeout:
                # HTTP 超时说明合成耗时过长，s2.cpp 可能仍在处理中
                wait = min(45 + attempt * 15, 120)  # 60s, 75s, 90s, 105s, 120s
                print(f"  [超时] seg[{seg_idx}] 第{attempt}次超时({timeout}s)，"
                      f"{wait}s 后重试")
                time.sleep(wait)

            except requests.ConnectionError:
                wait = min(attempt * 5, 30)
                print(f"  [断连] seg[{seg_idx}] 服务不可达，{wait}s 后重试 (第{attempt}次)")
                time.sleep(wait)

            except InterruptedError:
                raise

            except Exception as e:
                wait = min(attempt * 10, 60)
                print(f"  [异常] seg[{seg_idx}] {e}，{wait}s 后重试")
                time.sleep(wait)

        if progress_cb:
            progress_cb(i + 1, total)

    if progress_cb:
        progress_cb(len(tts_audio_map), total)

    print(f"[Step3-Fish] 合成完成: {len(tts_audio_map)}/{total} 条")
    return tts_audio_map
