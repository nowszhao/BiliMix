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


# ============================================================
# 口语/习语易错短语字典 — 兜底后处理
# ============================================================
# 模型有时会对口语表达做字面翻译（如 a hundred percent → 一百分），
# 此字典用于后处理修正。key 为英文（小写），value 为正确的中文口语翻译。
_COLLOQUIAL_FIXUPS: dict[str, str] = {
    # 肯定/确认
    "a hundred percent": "百分之百确定",
    "one hundred percent": "百分之百确定",
    "you bet": "当然",
    "you betcha": "当然了",
    "for sure": "肯定的",
    "absolutely": "绝对的",
    "definitely": "肯定的",
    "without a doubt": "毫无疑问",
    # 惊讶/质疑
    "no way": "不会吧",
    "no kidding": "真的假的",
    "you don't say": "真的假的",
    "for real": "真的假的",
    "are you serious": "你说真的吗",
    "you're kidding": "你开玩笑吧",
    "get out of here": "别逗了",
    "shut up": "不会吧",
    "no shot": "不可能",
    # 共鸣/附和
    "tell me about it": "可不是嘛",
    "i know right": "谁说不是呢",
    "i feel you": "我懂你",
    "same here": "我也是",
    "you and me both": "彼此彼此",
    # 认同/接受
    "fair enough": "说得过去",
    "that works": "可以",
    "sounds good": "听起来不错",
    "i'm down": "我同意",
    "count me in": "算我一个",
    "i'm game": "我没问题",
    # 口语填充/语气
    "you know": "你知道的",
    "i mean": "我是说",
    "like": "",
    "sort of": "算是吧",
    "kind of": "有点",
    "pretty much": "差不多",
    "at the end of the day": "说到底",
    "to be honest": "说实话",
    "honestly": "说真的",
    # 转折/让步
    "that being said": "话虽如此",
    "having said that": "话虽这么说",
    "don't get me wrong": "别误会",
    "not gonna lie": "讲真",
    "to be fair": "公平地说",
    # 时间/顺序
    "right off the bat": "一开始",
    "down the road": "以后",
    "in the long run": "长远来看",
    "on the fly": "临时地",
    "off the top of my head": "凭印象说",
}


def _apply_colloquial_fixup(english: str, chinese: str) -> str:
    """
    对翻译结果做口语习语兜底修正。
    如果英语原文匹配已知口语短语，用字典值替换直译结果。

    Args:
        english: 英文原文（整句）
        chinese: 模型翻译的中文

    Returns:
        str: 修正后的中文（如无需修正则返回原值）
    """
    eng_lower = english.strip().rstrip(".!?。！？").lower()
    if eng_lower in _COLLOQUIAL_FIXUPS:
        return _COLLOQUIAL_FIXUPS[eng_lower]
    return chinese


# ============================================================
# 口语翻译 Few-shot 示例（精简为 2 组）
# ============================================================
_DIALOGUE_FEWSHOT = (
    "示例：\n"
    "[1] Really?\n"
    "[2] A hundred percent.\n"
    "→ [1] 真的吗？\n"
    "→ [2] 百分之百确定。\n\n"
    "[3] Are you coming tonight?\n"
    "[4] You bet.\n"
    "→ [3] 你今晚来吗？\n"
    "→ [4] 当然。\n"
)

_SINGLE_FEWSHOT = (
    "示例：\n"
    "英文：A hundred percent.\n"
    "中文：百分之百确定。\n"
    "英文：I had no idea what I wanted to do with my life.\n"
    "中文：我完全不知道这辈子想干什么。\n"
)


def build_translation_prompt(sentences: list, prev_context: list = None) -> str:
    """
    构建批量翻译 prompt，针对播客口语 + 叙事场景优化。

    改进点（v2）：
    - 大幅精简：去掉习语列表，模型自己知道
    - 多句用对话 few-shot，单句用独立 few-shot
    - 放开推理：让模型先理解语境再翻译，而非禁止思考
    - 区分应答短句 vs 叙述长句的处理策略
    - 跨 batch 上下文保持连贯

    Args:
        sentences: 待翻译的句子列表 [(seq_id, text), ...]
        prev_context: 上一批末尾的上下文 [(english原文, 中文翻译), ...]
    """
    numbered = "\n".join(f"[{seq_id}] {text}" for seq_id, text in sentences)
    is_dialogue = len(sentences) > 1

    # 构建前文上下文（跨 batch 连贯性保障）
    context_section = ""
    if prev_context:
        ctx_lines = "\n".join(
            f"  {eng} → {chi}" for eng, chi in prev_context
        )
        context_section = (
            f"## 前文参考\n"
            f"上一批已翻译（英文→中文）：\n{ctx_lines}\n"
            f"当前句子紧接着前文，请保持连贯。\n\n"
        )

    if is_dialogue:
        # ---- 多句（对话/段落）模式 ----
        # 注意：不使用「你是...」等角色扮演开头，translategemma 容易将其理解为元指令并回复确认语
        return (
            f"将以下 {len(sentences)} 行英文播客口语翻译为自然中文口语。直接输出，不要确认语。\n\n"
            f"{context_section}"
            f"要求：口语化（可加\"吧、呢、嘛、啊\"等语气词）、习语意译、"
            f"应答短句简短（如\"A hundred percent.\"→\"百分之百确定。\"）、叙述长句完整。\n\n"
            f"{_DIALOGUE_FEWSHOT}"
            f"按 [N] 中文翻译 格式输出：\n\n"
            f"{numbered}"
        )
    else:
        # ---- 单句模式 ----
        return (
            f"将以下英文翻译为自然中文口语。只输出中文翻译，不要确认语。\n\n"
            f"{_SINGLE_FEWSHOT}"
            f"英文：{sentences[0][1]}\n"
            f"中文："
        )


# ============================================================
# 元响应检测 — 拦截模型返回的提示语而非翻译
# ============================================================
# translategemma:12b 有时会返回「请提供需要翻译的英文...」等确认语，
# 而非实际翻译内容。此检测器用于识别并触发降级重试。
_META_RESPONSE_PATTERNS = [
    r'请提供需要翻译的',
    r'请您提供需要翻译',
    r'请提供您需要翻译',
    r'请提供.*翻译.*内容',
    r'我会尽力.*翻译',
    r'翻译成自然流畅',
    r'^好的[，,]',
    r'^好的[。.]',
    r'^可以[，,]',
    r'^可以[。.]',
    r'请告诉我',
    r'请输入',
    r'请发送',
]


def _is_meta_response(text: str) -> bool:
    """
    检测模型是否返回了元指令/确认语，而非实际翻译内容。

    Args:
        text: LLM 回复文本

    Returns:
        bool: True 表示这是元响应（需要降级重试）
    """
    if not text or not text.strip():
        return False
    for pattern in _META_RESPONSE_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def _build_direct_prompt(sentences: list) -> str:
    """
    构建极简直接的降级翻译 prompt，用于元响应重试。

    特点：去掉所有角色扮演和复杂指令，只用一句祈使句 + 编号内容。
    translategemma 对这种格式的依从性更高。

    Args:
        sentences: 待翻译的句子列表 [(seq_id, text), ...]
    """
    numbered = "\n".join(f"[{seq_id}] {text}" for seq_id, text in sentences)
    return (
        f"翻译以下英文为中文口语，直接输出结果，不要任何确认语。\n\n"
        f"{numbered}\n\n"
        f"输出格式：\n"
    )


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
                        cancel_check=None, progress_cb=None,
                        resume_batch: int = 0,
                        existing_translations: dict = None,
                        checkpoint_cb=None) -> dict:
    """
    批量翻译指定的句子，适配 TranslateGemma。
    支持断点续跑：resume_batch 跳过已完成批次，existing_translations 预填充。

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
    start_batch = resume_batch if resume_batch < total_batches else total_batches

    all_translations = existing_translations or {}
    if start_batch > 0:
        print(f"[Step2b] 从批次 {start_batch + 1}/{total_batches} 续跑 "
              f"(已跳过前 {start_batch} 批，已有 {len(all_translations)} 个翻译)")

    print(f"[Step2b] 开始逐批翻译，共 {len(numbered_pairs)} 个句子，"
          f"每批 {batch_size} 句，分为 {total_batches} 批")

    # 跨 batch 上下文：记录上一批末尾 2 句的 (英文原文, 中文翻译)
    prev_context = []
    prompt_logged = 0  # 只打印前 10 个 prompt 方便调试

    for batch_idx, batch in enumerate(batches):
        if batch_idx < start_batch:
            continue  # 跳过已完成的批次
        if cancel_check and cancel_check():
            raise InterruptedError("任务已被用户终止")

        if progress_cb:
            progress_cb(batch_idx, total_batches)

        preview = batch[0][1][:50]
        expected_ids = [s[0] for s in batch]
        print(f"[Step2b] [批次 {batch_idx + 1}/{total_batches}] "
              f"翻译 {len(batch)} 句: {preview}...")

        t0 = time.time()
        prompt = build_translation_prompt(batch, prev_context)
        if prompt_logged < 10:
            prompt_logged += 1
            print(f"  [Prompt #{prompt_logged}] 长度 {len(prompt)} 字符:")
            # 只打印 prompt 的句子部分（跳过系统指令），最多 500 字符
            prompt_lines = prompt.split("\n")
            sentence_start = next((i for i, ln in enumerate(prompt_lines) if ln.startswith("[")), 0)
            snippet = "\n".join(prompt_lines[sentence_start:])
            if len(snippet) > 500:
                snippet = snippet[:500] + "..."
            print(f"  {snippet}")
            print(f"  ---")
        response = call_ollama(prompt)
        batch_translations = parse_translation_response(response, expected_ids)

        # 检测元响应：若模型返回了确认语而非翻译，用极简 prompt 重试批次
        if not batch_translations and _is_meta_response(response):
            print(f"  [元响应检测] 模型返回了确认语而非翻译，使用极简 prompt 重试")
            direct_prompt = _build_direct_prompt(batch)
            response = call_ollama(direct_prompt)
            batch_translations = parse_translation_response(response, expected_ids)

        elapsed = time.time() - t0
        print(f"         耗时 {elapsed:.1f}s，翻译到 {len(batch_translations)} 句")

        # 合并结果：将 seq_id 映射回实际 segment 索引
        # 同时收集本批的英文→中文对，供下一批做上下文
        batch_english_chi = []  # 本批所有翻译的结果
        for seq_id, text in batch:
            actual_idx = seq_to_idx[seq_id]
            if seq_id in batch_translations and batch_translations[seq_id]:
                # 口语习语兜底修正 + 拼音清洗
                translation = batch_translations[seq_id]
                translation = _apply_colloquial_fixup(text, translation)
                all_translations[actual_idx] = translation
                batch_english_chi.append((text, translation))
            else:
                # 单句重试（使用对话感知 prompt）
                print(f"  [重试] 句子 {actual_idx} 单句重试...")
                retry_prompt = build_translation_prompt([(1, text)], prev_context)
                retry_resp = call_ollama(retry_prompt)
                # 检测元响应：若返回确认语，用极简 prompt 重试
                if _is_meta_response(retry_resp):
                    print(f"    [元响应] 单句也返回了确认语，使用极简 prompt")
                    retry_resp = call_ollama(_build_direct_prompt([(1, text)]))
                retry_text = _clean_pinyin(retry_resp.strip()) if retry_resp else ""
                if retry_text:
                    retry_text = _apply_colloquial_fixup(text, retry_text)
                    all_translations[actual_idx] = retry_text
                    batch_english_chi.append((text, retry_text))
                else:
                    print(f"  [失败] 句子 {actual_idx} 翻译失败，跳过")

        # 更新跨 batch 上下文：取本批最后 2 条翻译
        if batch_english_chi:
            prev_context = batch_english_chi[-2:]

        # 每批完成后保存断点
        if checkpoint_cb:
            checkpoint_cb(batch_idx + 1, dict(all_translations))

    print(f"[Step2b] 翻译完成: {len(all_translations)}/{len(sentence_pairs)} 句")
    return all_translations
