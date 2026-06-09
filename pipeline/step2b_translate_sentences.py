"""
Step 2b: 句子翻译模块 (sentence_translate / smart_translate 模式)
调用 Ollama 将 WhisperX 转录的英文句子整句翻译为中文。

策略：
- 按批次合并句子发送给 LLM，减少 API 调用次数
- 翻译要求"信达雅"，保留原文语境和语气
- sentence_translate 模式：根据 SENTENCE_CN_RATIO 均匀间隔选择需要翻译的句子
- smart_translate 模式：根据生词识别结果，选择包含生词的句子进行翻译
"""
import json
import math
import re
import time

from core import config
from pipeline.step2_identify_difficult_words import call_ollama


def select_sentences_by_difficulty(segments: list, difficult_words: list,
                                   max_ratio: float = None) -> list:
    """
    根据生词识别结果，选出包含生词的句子索引（smart_translate 模式专用）。

    Args:
        segments: WhisperX segments 列表（每项含 "text"）
        difficult_words: 生词列表（每项含 "word"）
        max_ratio: 最大翻译比例上限（安全阀），None 则用 config 配置

    Returns:
        list[int]: 需要翻译的 segment 索引列表（按生词密度降序截断后再排序）
    """
    if max_ratio is None:
        max_ratio = getattr(config, "SMART_MAX_TRANSLATE_RATIO", 0.7)

    total = len(segments)
    if total == 0 or not difficult_words:
        return []

    # 构建生词集合（小写），包括多词短语
    word_set = set()
    for w in difficult_words:
        word_lower = w.get("english", "").lower().strip()
        if word_lower:
            word_set.add(word_lower)

    # 遍历每个 segment，统计包含的生词数量（密度）
    seg_scores = []  # [(seg_index, word_count)]
    for i, seg in enumerate(segments):
        text_lower = seg.get("text", "").lower()
        count = 0
        for word in word_set:
            if word in text_lower:
                count += 1
        if count > 0:
            seg_scores.append((i, count))

    if not seg_scores:
        return []

    # 按生词密度降序排列
    seg_scores.sort(key=lambda x: x[1], reverse=True)

    # 应用最大比例上限
    max_count = max(1, round(total * max_ratio))
    selected = seg_scores[:max_count]

    # 按原始顺序排列
    indices = sorted([idx for idx, _ in selected])

    print(f"[Smart] 共 {total} 句，含生词 {len(seg_scores)} 句，"
          f"上限 {max_ratio*100:.0f}%={max_count} 句，"
          f"最终选择 {len(indices)} 句")

    return indices


def select_sentences_to_translate(segments: list, ratio: float = None) -> list:
    """
    根据翻译占比，均匀间隔选择需要翻译的句子索引。

    Args:
        segments: WhisperX segments 列表
        ratio: 中文翻译占比 (0.0~1.0)，None 则用 config 配置

    Returns:
        list[int]: 需要翻译的 segment 索引列表
    """
    if ratio is None:
        ratio = getattr(config, "SENTENCE_CN_RATIO", 1.0)

    total = len(segments)
    if total == 0 or ratio <= 0:
        return []

    if ratio >= 1.0:
        return list(range(total))

    # 均匀间隔选择
    # 例如 ratio=0.5, total=10: 选 0,2,4,6,8 (每隔一个)
    # ratio=0.33, total=12: 选 0,3,6,9 (每隔两个)
    count = max(1, round(total * ratio))
    if count >= total:
        return list(range(total))

    step = total / count
    indices = []
    for i in range(count):
        idx = round(i * step)
        if idx < total:
            indices.append(idx)

    return sorted(set(indices))


def build_translation_prompt(sentences: list) -> str:
    """
    构建批量句子翻译的 LLM prompt。

    Args:
        sentences: 待翻译的句子列表 [(idx, text), ...]

    Returns:
        str: prompt 文本
    """
    numbered = "\n".join(f"[{idx}] {text}" for idx, text in sentences)

    return f"""你是一位专业的中英翻译专家。请将以下英文句子翻译成中文。

## 翻译要求
1. **信达雅**：翻译要忠实原文、通顺流畅、符合中文表达习惯
2. **保持语气**：如果原文是疑问句、感叹句、命令句，翻译也要保持同样的语气
3. **自然口语化**：这些句子来自英文播客，翻译应该像自然的中文口语，适合朗读
4. **不要加注释**：不要在翻译中添加括号注释或解释
5. **人名地名**：保留英文原名，不翻译专有名词

## 输出格式
请严格按 JSON 格式输出，每个句子编号对应其翻译：
{{"translations": [{{"id": 1, "chinese": "翻译内容"}}, {{"id": 2, "chinese": "翻译内容"}}]}}

## 待翻译句子
{numbered}"""


def parse_translation_response(response_text: str) -> dict:
    """
    解析 LLM 返回的翻译 JSON。

    Args:
        response_text: LLM 回复文本

    Returns:
        dict: {sentence_id: chinese_translation}
    """
    if not response_text.strip():
        return {}

    json_match = re.search(r'\{[\s\S]*"translations"[\s\S]*\}', response_text)
    if not json_match:
        return {}

    try:
        data = json.loads(json_match.group())
        translations = data.get("translations", [])
        result = {}
        for item in translations:
            sid = item.get("id")
            chinese = item.get("chinese", "").strip()
            if sid is not None and chinese:
                result[int(sid)] = chinese
        return result
    except (json.JSONDecodeError, ValueError):
        return {}


def translate_sentences(segments: list, indices: list = None,
                        batch_size: int = None,
                        cancel_check=None, progress_cb=None) -> dict:
    """
    批量翻译指定的句子。

    Args:
        segments: WhisperX segments 列表
        indices: 需要翻译的 segment 索引列表，None 则翻译全部
        batch_size: 每批翻译的句子数量
        cancel_check: 终止检查回调
        progress_cb: 进度回调 (batch_idx, total_batches)

    Returns:
        dict: {segment_index: chinese_translation}
    """
    if indices is None:
        indices = list(range(len(segments)))

    if batch_size is None:
        batch_size = getattr(config, "LLM_BATCH_SIZE", 8)

    # 构建 (索引, 文本) 对
    sentence_pairs = []
    for idx in indices:
        if idx < len(segments):
            text = segments[idx].get("text", "").strip()
            if text:
                sentence_pairs.append((idx, text))

    if not sentence_pairs:
        print("[Step2b] 没有需要翻译的句子")
        return {}

    # 分批
    batches = []
    for i in range(0, len(sentence_pairs), batch_size):
        batches.append(sentence_pairs[i:i + batch_size])

    total_batches = len(batches)
    print(f"[Step2b] 开始批量翻译，共 {len(sentence_pairs)} 个句子，"
          f"每批 {batch_size} 句，分为 {total_batches} 批")

    all_translations = {}

    for batch_idx, batch in enumerate(batches):
        if cancel_check and cancel_check():
            raise InterruptedError("任务已被用户终止")

        if progress_cb:
            progress_cb(batch_idx, total_batches)

        preview = batch[0][1][:50]
        print(f"[Step2b] [批次 {batch_idx + 1}/{total_batches}] "
              f"翻译 {len(batch)} 句: {preview}...")

        t0 = time.time()
        prompt = build_translation_prompt(batch)
        response = call_ollama(prompt)
        translations = parse_translation_response(response)

        elapsed = time.time() - t0
        print(f"         耗时 {elapsed:.1f}s，翻译到 {len(translations)} 句")

        # 合并结果
        for idx, text in batch:
            if idx in translations:
                all_translations[idx] = translations[idx]
            else:
                # 如果单个句子翻译失败，尝试单句重试
                print(f"  [重试] 句子 {idx} 未找到翻译，单句重试...")
                retry_prompt = build_translation_prompt([(idx, text)])
                retry_resp = call_ollama(retry_prompt)
                retry_result = parse_translation_response(retry_resp)
                if idx in retry_result:
                    all_translations[idx] = retry_result[idx]
                else:
                    print(f"  [失败] 句子 {idx} 翻译失败，跳过")

    print(f"[Step2b] 翻译完成: {len(all_translations)}/{len(sentence_pairs)} 句")
    return all_translations
