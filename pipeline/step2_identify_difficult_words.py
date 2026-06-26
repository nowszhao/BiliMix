"""
Step 2: 难词/短语识别与翻译模块
调用 Ollama (Qwen3.5:9b) 批量识别文本中的生僻词汇和短语，并翻译为中文。

策略：将 WhisperX 的 segments 按批次合并（每批 BATCH_SIZE 句），
一次性发给大模型分析，减少 API 调用次数以大幅提高识别速度。
识别到生词后再回到 segments.words 中定位具体时间戳。

优化措施：
- Few-shot 示例引导 LLM 精准区分基础词和真正的生词
- 高频词表后置过滤：基于 BNC/COCA 25k 词频分级表自动剔除基础词（含所有词形变化）
"""
import json
import os
import re
import sys
import time

import requests

from core import config

# 每批合并的句子数量（可在 config.py 中覆盖）
BATCH_SIZE = getattr(config, "LLM_BATCH_SIZE", 8)

# =============================================
# 高频词表：基于 BNC/COCA 25k 词频分级表过滤误标的基础词
# =============================================
# BNC/COCA 词表将 25000 个词头按频率分为 1k~25k 级别（每级约 1000 词），
# 且自带每个词头的所有词形变化（如 manage -> managed, manages, managing,
# manageable, management, ...），无需手写词形还原规则。
#
# 不同难度等级对应不同的过滤阈值（该级别以内的词被视为"基础词"不应标记）

_FREQ_FILTER_THRESHOLDS = {
    "CET-4": 2,       # 过滤前 2k 词头（如 time, work, manage, trust 等绝对基础词）
                       # 3k 词头（如 execute, barrier, persist）由 LLM prompt 判断
    "CET-6": 3,       # 六级学生词汇量更大，过滤前 3k
    "IELTS-6": 3,
    "IELTS-7": 2,
    "ADVANCED": 1,     # 高级水平只过滤前 1k 功能词
}

from core.word_frequency import get_word_level_num as _get_word_level_unified


def filter_by_frequency(words: list, difficulty: str = None) -> list:
    """
    基于 BNC/COCA 词频分级表过滤误标的基础词。

    BNC/COCA 表自带每个词头的所有词形变化（如 running/ran/runs 都映射到 run），
    因此不需要自己做词形还原，过滤更精准。

    Args:
        words: LLM 返回的难词列表
        difficulty: 难度等级

    Returns:
        list: 过滤后的难词列表
    """
    if difficulty is None:
        difficulty = config.DIFFICULTY_LEVEL

    threshold_k = _FREQ_FILTER_THRESHOLDS.get(difficulty, 3)
    filtered = []
    removed = []

    for w in words:
        eng = w["english"]
        # 短语/习语/搭配不做词频过滤（多词表达的难度不能简单用单词频率衡量）
        if w.get("type") in ("phrase", "idiom", "collocation") or " " in eng:
            filtered.append(w)
            continue

        level = _get_word_level_unified(eng)
        if level <= threshold_k:
            removed.append((eng, level))
        else:
            filtered.append(w)

    if removed:
        print(f"[Step2] 词频过滤: 剔除 {len(removed)} 个基础词 "
              f"(BNC/COCA ≤{threshold_k}k):")
        for eng, level in removed:
            print(f"    ✗ {eng} ({level}k)")

    return filtered


def _get_level_desc(difficulty: str = None) -> str:
    """获取难度等级描述文本。"""
    if difficulty is None:
        difficulty = config.DIFFICULTY_LEVEL

    level_desc = {
        "CET-4": "中国大学英语四级考试（CET-4）词汇量约4500词。低于此水平的常见词不需要标记。",
        "CET-6": "中国大学英语六级考试（CET-6）词汇量约6000词。低于此水平的常见词不需要标记。",
        "IELTS-6": "雅思6分水平，词汇量约5000-6000词。低于此水平的常见词不需要标记。",
        "IELTS-7": "雅思7分水平，词汇量约7000-8000词。低于此水平的常见词不需要标记。",
        "ADVANCED": "高级英语水平，词汇量约10000词以上。只标记非常罕见的专业词汇。",
    }
    return level_desc.get(difficulty, level_desc["CET-4"])


def _build_fewshot_examples(difficulty: str = None) -> str:
    """构建 Few-shot 示例，根据难度等级返回不同的示例。"""
    if difficulty is None:
        difficulty = config.DIFFICULTY_LEVEL

    # 通用示例：展示"应该标什么"和"不应该标什么"，包含成语/俚语/固定搭配的正例
    return """
---示例---
句子: "The team spent every single day managing the system to keep the business running smoothly."
分析: team, spent, every, single, day, managing, system, business, running 都是初高中基础词汇，不标记。smoothly 也是常见副词。→ 本句无生词。
输出: {"difficult_words": []}

句子: "The animals on Rainbow Island evacuated quickly as the volcanic eruption intensified."
分析: animals, quickly, island 是基础词，不标记。evacuated（撤离）、volcanic（火山的）、eruption（喷发）、intensified（加剧）超出基础词汇，需标记。
输出: {"difficult_words": [{"english": "evacuated", "chinese": "撤离", "type": "word"}, {"english": "volcanic eruption", "chinese": "火山喷发", "type": "phrase"}, {"english": "intensified", "chinese": "加剧", "type": "word"}]}

句子: "AI agents can now seamlessly retrieve real-time product variants and availability."
分析: agents, now, product 是基础词，不标记。seamlessly（无缝地）、retrieve（检索）、variants（变体）、availability（库存情况）超出基础词汇，需标记。real-time 也是常见词，不标记。
输出: {"difficult_words": [{"english": "seamlessly", "chinese": "无缝地", "type": "word"}, {"english": "retrieve", "chinese": "检索", "type": "word"}, {"english": "variants", "chinese": "变体", "type": "word"}, {"english": "availability", "chinese": "库存情况", "type": "word"}]}

句子: "Meta will allow rival AI chatbots on WhatsApp in Brazil for a fee."
分析: allow, chatbots 是基础词；rival 在此做形容词修饰 chatbots，意为"竞争对手的"，超出基础范围需标记。for a fee 是固定搭配，字面理解可能有偏差，意为"付费地"，需标记。Meta、WhatsApp、Brazil 是专有名词不标记。
输出: {"difficult_words": [{"english": "rival", "chinese": "竞争对手的", "type": "word"}, {"english": "for a fee", "chinese": "收费", "type": "collocation"}]}

句子: "The new policy could cut corners on safety standards, which would undermine public trust on the heels of the recent scandal."
分析: policy, safety, standards, public, trust, recent 是基础词。cut corners 是习语，意为"偷工减料"；undermine 是学术词汇，意为"削弱"；on the heels of 是习语，意为"紧随其后"；scandal 超出基础词汇，意为"丑闻"。
输出: {"difficult_words": [{"english": "cut corners", "chinese": "偷工减料", "type": "idiom"}, {"english": "undermine", "chinese": "削弱", "type": "word"}, {"english": "on the heels of", "chinese": "紧随其后", "type": "idiom"}, {"english": "scandal", "chinese": "丑闻", "type": "word"}]}

句子: "MIDI's approach sets it apart from other healthcare providers."
分析: approach, other, providers 是基础词。sets it apart 是固定搭配，在句中连续出现，意为"使其脱颖而出"，需标记。注意：必须写成 "sets it apart" 而不是 "sets ... apart"，因为必须是原文连续出现的完整词组。
输出: {"difficult_words": [{"english": "sets it apart", "chinese": "使其脱颖而出", "type": "idiom"}]}
---示例结束---"""


def build_sentence_prompt(sentence: str, difficulty: str = None) -> str:
    """
    构建针对单句的提示词（保留用于兼容旧接口）。

    Args:
        sentence: 单句英文文本
        difficulty: 难度等级

    Returns:
        str: 提示词
    """
    desc = _get_level_desc(difficulty)
    fewshot = _build_fewshot_examples(difficulty)

    return f"""你是一个**严格的**英语教学助手。用户正在学习英语听力，水平为：{desc}

请分析以下英文句子，找出**真正超出该水平**的生僻词汇、短语、习语和固定搭配。

## 判断标准（非常重要！严格遵守！）

**不要标记的词（即使你觉得可能有人不认识）：**
- 初中、高中英语课本中的基础词汇（如 time, work, team, day, business, system, program, control, support, focus, reach, manage, through, every, single, while, using, found, running, include, content, access, edit, language, natural, trust, concern, comments, partners, launch, shop, goals, daily, default, require, inform 等）
- 人名、地名、品牌名、产品名等专有名词
- 日常口语高频词

**应该标记的词（四类）：**
1. **word（生词）**：学术词汇、专业术语（如 evacuated, vulnerabilities, seamlessly, generative, erode, undermine）
2. **phrase（短语/术语）**：专业术语短语、技术名词（如 volcanic eruption, threat model, market importance）
3. **idiom（习语/俚语）**：字面意思无法理解实际含义的表达（如 cut corners=偷工减料, on the heels of=紧随其后, a dime a dozen=极其常见的）
4. **collocation（固定搭配）**：常见动词/介词搭配，字面理解可能有偏差（如 for a fee=收费, at stake=处于危险中）

**核心原则：宁可漏标，不可错标！如果你不确定一个词是否超出该水平，就不要标它。**

## 翻译要求（非常重要！严格遵守！）
- 翻译必须基于该词/短语**在当前句子中的实际用法和词性**
- 例如 rival 做名词时翻译为"对手"，做形容词修饰名词时翻译为"竞争对手的"
- 不要给词典通用释义，要给**句中语境含义**
- **每个词/短语只给一个翻译！** 绝对不允许用分号(；)、斜杠(/)、顿号(、)、括号补充等方式列举多个含义
- 错误示范：~~"整体的；全面的"~~  ~~"方案/规程"~~  ~~"（医疗）方案"~~
- 正确示范：只选一个最贴合语境的，如"全面的"或"方案"

## 格式要求
1. **短语/习语/搭配必须是句子中原文连续出现的完整词组**，绝对不要用省略号(...)或占位符替代中间的词
2. 错误示范：~~"sets ... apart"~~（中间有省略号）
3. 正确做法：提取原文中实际连续出现的完整表达，如句中是"sets it apart"就写"sets it apart"；如果表达在原文中不连续，就不要提取为一个词组
4. 如果句子没有真正的生词，返回空列表 `{{"difficult_words": []}}`
5. 每个词只给**一个**最贴合语境的中文翻译
6. type 必须是 word / phrase / idiom / collocation 之一
{fewshot}

请严格按照JSON格式输出，不要输出任何其他内容：
{{"difficult_words": [{{"english": "evacuated", "chinese": "撤离", "type": "word"}}, {{"english": "for a fee", "chinese": "付费地", "type": "collocation"}}]}}

英文句子：
{sentence}"""


def build_batch_prompt(sentences: list, difficulty: str = None) -> str:
    """
    构建批量句子的提示词。将多个句子编号后一次性发给模型。

    Args:
        sentences: 句子列表 [str, ...]
        difficulty: 难度等级

    Returns:
        str: 提示词
    """
    desc = _get_level_desc(difficulty)
    fewshot = _build_fewshot_examples(difficulty)

    # 将句子编号，方便模型区分
    numbered = "\n".join(f"[{i+1}] {s}" for i, s in enumerate(sentences))

    return f"""你是一个**严格的**英语教学助手。用户正在学习英语听力，水平为：{desc}

请分析以下 {len(sentences)} 个英文句子，找出**真正超出该水平**的生僻词汇、短语、习语和固定搭配。

## 判断标准（非常重要！严格遵守！）

**不要标记的词（即使你觉得可能有人不认识）：**
- 初中、高中英语课本中的基础词汇（如 time, work, team, day, business, system, program, control, support, focus, reach, manage, through, every, single, while, using, found, running, include, content, access, edit, language, natural, trust, concern, comments, partners, launch, shop, goals, daily, default, require, inform 等）
- 人名、地名、品牌名、产品名等专有名词
- 日常口语高频词
- 不要因为语境是科技/商业话题就把常见词标为生词

**应该标记的词（四类）：**
1. **word（生词）**：学术词汇、专业术语（如 evacuated, vulnerabilities, seamlessly, generative, erode, undermine）
2. **phrase（短语/术语）**：专业术语短语、技术名词（如 volcanic eruption, threat model, market importance）
3. **idiom（习语/俚语）**：字面意思无法理解实际含义的表达（如 cut corners=偷工减料, on the heels of=紧随其后, a dime a dozen=极其常见的）
4. **collocation（固定搭配）**：常见动词/介词搭配，字面理解可能有偏差（如 for a fee=收费, at stake=处于危险中）

**核心原则：宁可漏标，不可错标！如果你不确定一个词是否超出该水平，就不要标它。**

## 翻译要求（非常重要！严格遵守！）
- 翻译必须基于该词/短语**在所在句子中的实际用法和词性**
- 例如 rival 做名词时翻译为"对手"，做形容词修饰名词时翻译为"竞争对手的"
- 不要给词典通用释义，要给**句中语境含义**
- 如果同一个词在不同句子中含义差异大，请分别列出并在 source_sentence 中标注来源句号
- **每个词/短语只给一个翻译！** 绝对不允许用分号(；)、斜杠(/)、顿号(、)、括号补充等方式列举多个含义
- 错误示范：~~"整体的；全面的"~~  ~~"方案/规程"~~  ~~"（医疗）方案"~~  ~~"茁壮成长/蓬勃发展"~~
- 正确示范：只选一个最贴合语境的，如"全面的"或"方案"或"蓬勃发展"

## 格式要求
1. **短语/习语/搭配必须是句子中原文连续出现的完整词组**，绝对不要用省略号(...)或占位符替代中间的词
2. 错误示范：~~"sets ... apart"~~  ~~"second X script"~~（中间有省略号或占位符）
3. 正确做法：提取原文中实际连续出现的完整表达，如句中是"sets it apart"就写"sets it apart"；如果表达在原文中不连续，就不要提取为一个词组
4. 对所有句子统一分析，相同词义只列一次；但同一个词在不同句子中含义不同时，请分别列出
5. 如果所有句子都没有真正的生词，返回空列表 `{{"difficult_words": []}}`
6. 每个词只给**一个**最贴合语境的中文翻译
7. type 必须是 word / phrase / idiom / collocation 之一
8. source_sentence 字段为该词/短语出现的句子编号（如 1, 2, 3），帮助关联语境
{fewshot}

请严格按照JSON格式输出，不要输出任何其他内容：
{{"difficult_words": [{{"english": "evacuated", "chinese": "撤离", "type": "word", "source_sentence": 1}}, {{"english": "for a fee", "chinese": "付费地", "type": "collocation", "source_sentence": 2}}]}}

英文句子：
{numbered}"""


def call_ollama(prompt: str, temperature: float = None) -> str:
    """
    调用 Ollama API 获取大模型回复（关闭思考模式）。

    Args:
        prompt: 提示词
        temperature: 推理温度，None 则使用 config.LLM_IDENTIFY_TEMPERATURE（默认 0.3）

    Returns:
        str: 模型回复文本
    """
    url = f"{config.OLLAMA_BASE_URL}/api/generate"
    # num_predict: 单句翻译需要约 512 token，批量翻译可能更多
    # 同时 qwen3.5 是推理模型，即使 think=False 仍可能输出大量推理 token
    # 增大 num_predict 可避免响应被截断导致 JSON 解析失败后逐句重试
    num_predict = getattr(config, "LLM_NUM_PREDICT", 8192)
    if temperature is None:
        temperature = float(getattr(config, "LLM_IDENTIFY_TEMPERATURE", 0.3))
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "think": False,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
        }
    }

    try:
        resp = requests.post(url, json=payload, timeout=300)
        resp.raise_for_status()
        result = resp.json()
        return result.get("response", "")
    except requests.exceptions.ConnectionError:
        print(f"[Step2] 错误: 无法连接 Ollama ({config.OLLAMA_BASE_URL})，请确认 Ollama 服务已启动")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print(f"[Step2] 警告: Ollama 请求超时")
        return ""
    except Exception as e:
        print(f"[Step2] Ollama 调用异常: {e}")
        return ""


def parse_llm_response(response_text: str) -> list:
    """
    解析大模型返回的JSON，提取难词列表。

    Args:
        response_text: 大模型的原始回复

    Returns:
        list[dict]: 难词列表，每项包含 english, chinese, type, (可选) source_sentence
    """
    if not response_text.strip():
        return []

    # 尝试从回复中提取 JSON 块
    json_match = re.search(r'\{[\s\S]*"difficult_words"[\s\S]*\}', response_text)
    if not json_match:
        return []

    VALID_TYPES = {"word", "phrase", "idiom", "collocation"}

    try:
        data = json.loads(json_match.group())
        words = data.get("difficult_words", [])
        # 校验格式
        valid = []
        for w in words:
            if "english" in w and "chinese" in w:
                english = w["english"].strip()
                chinese = w["chinese"].strip()

                # === 后置清洗 1：过滤 english 中包含省略号/占位符的条目 ===
                # 如 "sets ... apart"、"second X script" 等不连续的写法
                if "..." in english or "…" in english:
                    print(f"[Step2] 跳过不连续短语: {english} (含省略号)")
                    continue
                # 检测单个大写字母占位符（如 "second X script" 中的 X）
                if re.search(r'\b[A-Z]\b', english) and len(english.split()) > 1:
                    # 排除正常缩写如 "AI"（连续大写）或单字母词如 "I"
                    has_placeholder = False
                    for token in english.split():
                        if len(token) == 1 and token.isupper() and token != "I":
                            has_placeholder = True
                            break
                    if has_placeholder:
                        print(f"[Step2] 跳过含占位符的短语: {english}")
                        continue

                # === 后置清洗 2：翻译只保留第一个含义 ===
                # 处理模型返回的多含义翻译，如 "整体的；全面的"、"方案/规程"、"茁壮成长/蓬勃发展"
                # 按分号、斜杠、顿号拆分，只保留第一个
                chinese = re.split(r'[；;/、]', chinese)[0].strip()
                # 去掉括号补充说明，如 "（医疗）方案" -> "方案"
                chinese = re.sub(r'[（(][^）)]*[）)]', '', chinese).strip()
                if not chinese:
                    chinese = w["chinese"].strip()  # 清洗后为空则回退原始值

                word_type = w.get("type", "word")
                # 规范化 type：不在合法范围内的统一回退
                if word_type not in VALID_TYPES:
                    # 包含空格的默认为 phrase
                    word_type = "phrase" if " " in english else "word"
                item = {
                    "english": english,
                    "chinese": chinese,
                    "type": word_type,
                }
                # 可选字段：来源句编号
                if "source_sentence" in w:
                    try:
                        item["source_sentence"] = int(w["source_sentence"])
                    except (ValueError, TypeError):
                        pass
                valid.append(item)
        return valid
    except json.JSONDecodeError:
        return []


def identify_difficult_words_by_segments(segments: list, difficulty: str = None,
                                         cancel_check=None, progress_cb=None,
                                         resume_batch: int = 0,
                                         existing_results: dict = None,
                                         checkpoint_cb=None) -> list:
    """
    批量分析 WhisperX segments，识别生词/短语。

    支持断点续跑：resume_batch 跳过已完成批次，existing_results 预填充。
    """
    all_words = existing_results or {}  # 用 english 小写作为 key 去重

    # 过滤有效句子
    valid_sentences = []
    for seg in segments:
        text = seg.get("text", "").strip()
        if text and len(text) >= 3:
            valid_sentences.append(text)

    total_sentences = len(valid_sentences)
    if total_sentences == 0:
        print("[Step2] 没有有效句子需要分析")
        return []

    # 按 BATCH_SIZE 分批
    batches = []
    for i in range(0, total_sentences, BATCH_SIZE):
        batches.append(valid_sentences[i:i + BATCH_SIZE])

    total_batches = len(batches)
    start_batch = resume_batch if resume_batch < total_batches else total_batches
    if start_batch > 0:
        print(f"[Step2] 从批次 {start_batch + 1}/{total_batches} 续跑 "
              f"(已跳过前 {start_batch} 批，已有 {len(all_words)} 个生词)")

    print(f"[Step2] 开始批量分析，共 {total_sentences} 个句子，"
          f"分为 {total_batches} 批（每批最多 {BATCH_SIZE} 句）")

    for batch_idx, batch_sentences in enumerate(batches):
        if batch_idx < start_batch:
            continue  # 跳过已完成的批次

        # --- 终止检查点 ---
        if cancel_check and cancel_check():
            raise InterruptedError("任务已被用户终止")

        # 进度回调
        if progress_cb:
            progress_cb(batch_idx, total_batches)

        preview = batch_sentences[0][:50]
        print(f"[Step2] [批次 {batch_idx+1}/{total_batches}] "
              f"分析 {len(batch_sentences)} 句: {preview}...")

        t0 = time.time()

        if len(batch_sentences) == 1:
            # 单句退化为单句 prompt（更精确）
            prompt = build_sentence_prompt(batch_sentences[0], difficulty)
        else:
            prompt = build_batch_prompt(batch_sentences, difficulty)

        response = call_ollama(prompt)
        words = parse_llm_response(response)

        elapsed = time.time() - t0
        print(f"        耗时 {elapsed:.1f}s，识别到 {len(words)} 个词")

        for w in words:
            key = w["english"].lower()
            if key not in all_words:
                all_words[key] = w
                type_tags = {
                    "phrase": "📖短语",
                    "idiom": "💬习语",
                    "collocation": "🔗搭配",
                }
                tag = type_tags.get(w["type"], "📝单词")
                print(f"  ✓ {tag} {w['english']} -> {w['chinese']}")

        # 每批完成后保存断点
        if checkpoint_cb:
            checkpoint_cb(batch_idx + 1, dict(all_words))

    result = list(all_words.values())
    print(f"\n[Step2] LLM 共识别 {len(result)} 个不重复的难词/短语"
          f"（{total_batches} 次 LLM 调用，原需 {total_sentences} 次）")

    # 后置过滤：基于词频表剔除基础词
    before_count = len(result)
    result = filter_by_frequency(result, difficulty)
    after_count = len(result)

    if before_count != after_count:
        print(f"[Step2] 词频过滤后: {before_count} -> {after_count} 个词")

    print(f"[Step2] 最终确认 {len(result)} 个难词/短语")
    return result


def locate_words_in_segments(difficult_words: list, segments: list) -> list:
    """
    将识别到的难词/短语定位到 segments.words 中，获取精确时间戳。

    策略：
    1. 先匹配多词短语/习语/搭配（优先级高），再匹配单词
    2. 已被短语覆盖的时间段不再被单词重复匹配（防止重叠替换）
    3. 最终结果按时间排序且无重叠

    Args:
        difficult_words: 难词列表 [{"english": ..., "chinese": ..., "type": ...}]
        segments: WhisperX 输出的 segments 列表

    Returns:
        list[dict]: 带时间戳的替换列表，每项包含:
            english, chinese, type, start, end, segment_index
    """
    replacements = []

    # ---- 第一轮：匹配多词表达（短语/习语/搭配），优先级高 ----
    multi_word_items = [dw for dw in difficult_words if len(dw["english"].split()) > 1]
    single_word_items = [dw for dw in difficult_words if len(dw["english"].split()) == 1]

    # 记录已被多词表达覆盖的时间区间 [(start, end, seg_idx), ...]
    covered_intervals = []

    for dw in multi_word_items:
        eng = dw["english"].lower()
        eng_words = eng.split()

        for seg_idx, seg in enumerate(segments):
            seg_words = seg.get("words", [])
            if not seg_words:
                continue

            for j in range(len(seg_words) - len(eng_words) + 1):
                match = True
                for k, ew in enumerate(eng_words):
                    clean_word = re.sub(r'[^\w\'-]', '', seg_words[j + k].get("word", "")).lower()
                    if clean_word != ew:
                        match = False
                        break
                if match:
                    start_w = seg_words[j]
                    end_w = seg_words[j + len(eng_words) - 1]
                    if "start" in start_w and "end" in end_w:
                        s, e = start_w["start"], end_w["end"]
                        replacements.append({
                            "english": dw["english"],
                            "chinese": dw["chinese"],
                            "type": dw["type"],
                            "start": s,
                            "end": e,
                            "segment_index": seg_idx,
                        })
                        covered_intervals.append((s, e, seg_idx))

    def _is_covered(start, end, seg_idx):
        """检查一个时间区间是否已被已有的多词表达完全或大部分覆盖"""
        for cs, ce, ci in covered_intervals:
            if ci != seg_idx:
                continue
            # 如果单词的时间段与已覆盖区间有显著重叠（>50%），则视为已覆盖
            overlap_start = max(start, cs)
            overlap_end = min(end, ce)
            if overlap_end > overlap_start:
                overlap_dur = overlap_end - overlap_start
                word_dur = end - start
                if word_dur > 0 and overlap_dur / word_dur > 0.5:
                    return True
        return False

    # ---- 第二轮：匹配单词，跳过已被短语覆盖的 ----
    for dw in single_word_items:
        eng = dw["english"].lower()

        for seg_idx, seg in enumerate(segments):
            seg_words = seg.get("words", [])
            if not seg_words:
                continue

            for w in seg_words:
                clean_word = re.sub(r'[^\w\'-]', '', w.get("word", "")).lower()
                if clean_word == eng and "start" in w and "end" in w:
                    s, e = w["start"], w["end"]
                    # 检查是否被多词表达覆盖
                    if _is_covered(s, e, seg_idx):
                        continue
                    replacements.append({
                        "english": dw["english"],
                        "chinese": dw["chinese"],
                        "type": dw["type"],
                        "start": s,
                        "end": e,
                        "segment_index": seg_idx,
                    })

    # 按时间排序
    replacements.sort(key=lambda x: x["start"])

    # ---- 最终去重：移除仍然存在的时间重叠 ----
    # 当两个替换的时间段有显著重叠时，保留更长（更精确）的那个
    if replacements:
        deduped = [replacements[0]]
        removed_overlaps = 0
        for i in range(1, len(replacements)):
            prev = deduped[-1]
            curr = replacements[i]
            # 检查与前一个是否重叠
            if curr["start"] < prev["end"]:
                # 有重叠：保留时间跨度更长的（通常是短语）
                prev_dur = prev["end"] - prev["start"]
                curr_dur = curr["end"] - curr["start"]
                if curr_dur > prev_dur:
                    deduped[-1] = curr  # 替换为更长的
                removed_overlaps += 1
            else:
                deduped.append(curr)

        if removed_overlaps > 0:
            print(f"[Step2] 去重: 移除 {removed_overlaps} 处时间重叠的替换 "
                  f"({len(replacements)} -> {len(deduped)})")
        replacements = deduped

    print(f"[Step2] 在音频中定位到 {len(replacements)} 处需要替换的位置")
    return replacements


# ============================================================
# 兼容旧接口（保留，供 main.py 调用）
# ============================================================

def identify_difficult_words(full_text: str, difficulty: str = None) -> list:
    """
    旧接口兼容：传入完整文本，单次调用LLM识别。
    建议使用 identify_difficult_words_by_segments() 替代。
    """
    prompt = build_sentence_prompt(full_text, difficulty)
    response = call_ollama(prompt)
    words = parse_llm_response(response)

    # 后置词频过滤
    words = filter_by_frequency(words, difficulty)

    print(f"[Step2] 识别到 {len(words)} 个难词/短语:")
    for w in words:
        type_tags = {
            "phrase": "📖短语",
            "idiom": "💬习语",
            "collocation": "🔗搭配",
        }
        tag = type_tags.get(w["type"], "📝单词")
        print(f"  {tag} {w['english']} -> {w['chinese']}")

    return words


if __name__ == "__main__":
    # 独立测试：批量模式
    test_json = os.path.join(config.OUTPUT_DIR, "0187bb3058e541a88f3c9e979c399912.json")
    if os.path.exists(test_json):
        with open(test_json, "r") as f:
            data = json.load(f)
        segments = data.get("segments", [])

        # 测试前 20 个 segment（批量分析）
        test_segs = segments[:20]
        print(f"=== 测试批量分析（前 {len(test_segs)} 句，BATCH_SIZE={BATCH_SIZE}）===")
        t0 = time.time()
        words = identify_difficult_words_by_segments(test_segs)
        elapsed = time.time() - t0
        print(f"\n总耗时: {elapsed:.1f}s")
        print(json.dumps(words, ensure_ascii=False, indent=2))

        if words:
            print("\n=== 测试时间戳定位 ===")
            locs = locate_words_in_segments(words, test_segs)
            print(json.dumps(locs, ensure_ascii=False, indent=2))
    else:
        # fallback 简单测试
        test_text = "The animals on Rainbow Island evacuated quickly."
        words = identify_difficult_words(test_text)
        print(json.dumps(words, ensure_ascii=False, indent=2))
