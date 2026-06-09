"""
Step 2b: 句子翻译模块 (sentence_translate / smart_translate 模式)
调用 Ollama 将 WhisperX 转录的英文句子整句翻译为中文。

策略：
- 适配 TranslateGemma，使用编号分批 + 纯文本输出（无需 JSON）
- 每批用 [N] 前缀标记句子，模型返回对应的中文翻译
- 按位置匹配 + [N] 前缀双重解析，提高容错
"""
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
    构建批量翻译 prompt，适配 TranslateGemma。
    使用 [seq_id] 前缀标记句子，要求模型按相同格式输出。

    Args:
        sentences: 待翻译的句子列表 [(seq_id, text), ...]

    Returns:
        str: prompt 文本
    """
    numbered = "\n".join(f"[{seq_id}] {text}" for seq_id, text in sentences)
    return (f"You are a professional English (en) to Chinese (zh-Hans) translator.\n"
            f"Your goal is to accurately convey the meaning and nuances of the original "
            f"English text while adhering to Chinese grammar, vocabulary, and cultural "
            f"sensitivities.\n"
            f"Produce only the Chinese translation, without any additional explanations "
            f"or commentary.\n"
            f"Please translate the following English text into Chinese:\n\n\n"
            f"{numbered}")


def _clean_pinyin(text: str) -> str:
    """
    去除翻译文本末尾的拼音注释。

    模型有时会输出如：
      "我没有藏起你的骆驼，恰恰。 (Wǒ méiyǒu cáng qǐ nǐ de luòtuo, Qiàqià.)"
    需要去掉括号内的拼音部分，只保留中文。

    Args:
        text: 可能含拼音注释的翻译文本

    Returns:
        str: 清洗后的纯中文翻译
    """
    if not text:
        return text

    # 去除末尾括号中的拼音/英文注释
    # 模式1: "中文 (Wǒ méiyǒu...)" → 去掉末尾括号及括号内内容
    text = re.sub(r'\s*\([A-Za-zà-üā-ōǜěńňǵḿ][^)]*\)\s*$', '', text)
    # 模式2: "中文 [Wǒ méiyǒu...]"
    text = re.sub(r'\s*\[[A-Za-zà-üā-ōǜěńňǵḿ][^\]]*\]\s*$', '', text)
    return text.strip()


def parse_translation_response(response_text: str, expected_ids: list) -> dict:
    """
    解析 TranslateGemma 的批量翻译纯文本响应。

    优先按 [N] 前缀匹配；若前缀匹配失败，按行位置顺序匹配。

    Args:
        response_text: LLM 回复文本
        expected_ids: 期望的 seq_id 列表（按顺序）

    Returns:
        dict: {seq_id: chinese_translation}
    """
    if not response_text or not response_text.strip():
        return {}

    lines = response_text.strip().split("\n")
    result = {}

    # 第一轮：按 [N] 前缀匹配
    id_prefix = re.compile(r'^\[(\d+)\]\s*(.*)')
    found_by_prefix = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = id_prefix.match(line)
        if m:
            sid = int(m.group(1))
            translation = _clean_pinyin(m.group(2).strip())
            if translation:
                found_by_prefix[sid] = translation

    # 第二轮：前缀匹配不到的部分，按位置顺序补充
    if found_by_prefix:
        result.update(found_by_prefix)
        return result

    # 无前缀匹配时，将非空行按顺序映射到 expected_ids
    non_empty = [_clean_pinyin(l.strip()) for l in lines if l.strip()]
    for i, sid in enumerate(expected_ids):
        if i < len(non_empty) and non_empty[i]:
            result[sid] = non_empty[i]

    return result


def translate_sentences(segments: list, indices: list = None,
                        batch_size: int = None,
                        cancel_check=None, progress_cb=None) -> dict:
    """
    批量翻译指定的句子，适配 TranslateGemma。

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

    # 用 1-based 顺序编号作为 prompt 中的标记，seq_to_idx 映射回实际 segment 索引
    seq_to_idx = {}
    numbered_pairs = []
    for seq_id, (idx, text) in enumerate(sentence_pairs, start=1):
        seq_to_idx[seq_id] = idx
        numbered_pairs.append((seq_id, text))

    # 分批
    batches = []
    for i in range(0, len(numbered_pairs), batch_size):
        batches.append(numbered_pairs[i:i + batch_size])

    total_batches = len(batches)
    print(f"[Step2b] 开始逐批翻译，共 {len(numbered_pairs)} 个句子，"
          f"每批 {batch_size} 句，分为 {total_batches} 批")

    all_translations = {}

    for batch_idx, batch in enumerate(batches):
        if cancel_check and cancel_check():
            raise InterruptedError("任务已被用户终止")

        if progress_cb:
            progress_cb(batch_idx, total_batches)

        preview = batch[0][1][:50]
        expected_ids = [s[0] for s in batch]
        print(f"[Step2b] [批次 {batch_idx + 1}/{total_batches}] "
              f"翻译 {len(batch)} 句: {preview}...")

        t0 = time.time()
        prompt = build_translation_prompt(batch)
        response = call_ollama(prompt)
        batch_translations = parse_translation_response(response, expected_ids)

        elapsed = time.time() - t0
        print(f"         耗时 {elapsed:.1f}s，翻译到 {len(batch_translations)} 句")

        # 合并结果：将 seq_id 映射回实际 segment 索引
        for seq_id, text in batch:
            actual_idx = seq_to_idx[seq_id]
            if seq_id in batch_translations and batch_translations[seq_id]:
                all_translations[actual_idx] = batch_translations[seq_id]
            else:
                # 单句重试
                print(f"  [重试] 句子 {actual_idx} 单句重试...")
                retry_prompt = f"Translate the following text from English to Chinese:\n\n{text}"
                retry_resp = call_ollama(retry_prompt)
                retry_text = retry_resp.strip() if retry_resp else ""
                if retry_text:
                    all_translations[actual_idx] = retry_text
                else:
                    print(f"  [失败] 句子 {actual_idx} 翻译失败，跳过")

    print(f"[Step2b] 翻译完成: {len(all_translations)}/{len(sentence_pairs)} 句")
    return all_translations
