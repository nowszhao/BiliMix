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
from typing import Any, Callable, Optional

from core import config
from pipeline.step2_identify_difficult_words import call_ollama

# ============================================================
# 常量配置
# ============================================================
_MAX_PROMPT_LOG = 10          # 调试时最多打印的 prompt 数量
_PROMPT_SNIPPET_LEN = 500     # 打印 prompt 片段的最大长度
_CONTEXT_WINDOW = 4           # 跨 batch 上下文保留的句子数
_MAX_RETRIES = 3              # LLM 调用失败最大重试次数
_RETRY_BACKOFF = 2.0          # 重试退避基数（秒）
_META_MAX_LEN = 100           # 元响应判定的最大文本长度
# 拉丁字母（含拼音声调符号），用于识别括号内的拼音注释
_LATIN_RE = r'[A-Za-zÀ-ÖØ-öø-ÿĀ-ž]'


def select_sentences_by_difficulty(
        segments: list[dict[str, Any]],
        difficult_words: list[dict[str, Any]],
        max_ratio: Optional[float] = None) -> list[int]:
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
        max_ratio = float(getattr(config, "SMART_MAX_TRANSLATE_RATIO", 0.7))

    total = len(segments)
    if total == 0 or not difficult_words:
        return []

    # 构建生词集合（小写），区分单词与多词短语
    # 单词必须用词边界匹配，否则 cat 会命中 category / location
    single_words: set[str] = set()
    phrases: set[str] = set()
    for w in difficult_words:
        word_lower = str(w.get("english", "")).lower().strip()
        if not word_lower:
            continue
        if " " in word_lower:
            phrases.add(word_lower)
        else:
            single_words.add(word_lower)

    # 预编译词边界正则：单词需精确匹配
    word_re = (
        re.compile(r"\b(?:" + "|".join(re.escape(w) for w in single_words) + r")\b")
        if single_words else None
    )
    phrase_patterns = [re.compile(re.escape(p)) for p in phrases]

    # 遍历每个 segment，统计包含的生词数量（密度）
    seg_scores: list[tuple[int, int]] = []  # [(seg_index, word_count)]
    for i, seg in enumerate(segments):
        text_lower = str(seg.get("text", "")).lower()
        count = 0
        if word_re:
            # set 去重：同一生词在一句中多次出现只计一次
            count += len(set(word_re.findall(text_lower)))
        for pat in phrase_patterns:
            if pat.search(text_lower):
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


def select_sentences_to_translate(
        segments: list[dict[str, Any]],
        ratio: Optional[float] = None) -> list[int]:
    """
    根据翻译占比，均匀间隔选择需要翻译的句子索引。

    Args:
        segments: WhisperX segments 列表
        ratio: 中文翻译占比 (0.0~1.0)，None 则用 config 配置

    Returns:
        list[int]: 需要翻译的 segment 索引列表
    """
    if ratio is None:
        ratio = float(getattr(config, "SENTENCE_CN_RATIO", 1.0))

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
    indices: list[int] = []
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

# 预编译口语短语匹配正则（词边界），(pattern, 短词数, 修正译文)
# 仅保留有非空修正译文且为有效短语的条目
_COLLOQUIAL_PATTERNS = [
    (re.compile(r"\b" + re.escape(phrase) + r"\b"), len(phrase.split()), fixup)
    for phrase, fixup in _COLLOQUIAL_FIXUPS.items()
    if fixup and phrase.split()
]


def _apply_colloquial_fixup(english: str, chinese: str) -> str:
    """
    对翻译结果做口语习语兜底修正。

    修正策略（按优先级）：
    1. 整句完全匹配：原文去掉首尾标点后等于字典 key，直接替换。
    2. 短语主导匹配：已知口语短语在原文中出现，且其词数占整句词数
       比例 >= 0.6（短句应答），用字典值替换直译结果，避免字面翻译。

    Args:
        english: 英文原文（整句）
        chinese: 模型翻译的中文

    Returns:
        str: 修正后的中文（如无需修正则返回原值）
    """
    if not english:
        return chinese
    eng_lower = english.strip().rstrip(".!?。！？").lower()
    if not eng_lower:
        return chinese

    # 1. 整句完全匹配
    if eng_lower in _COLLOQUIAL_FIXUPS:
        return _COLLOQUIAL_FIXUPS[eng_lower]

    # 2. 短语在句中占主导时替换（避免对长句误替换）
    eng_words = eng_lower.split()
    eng_wc = len(eng_words)
    if eng_wc == 0:
        return chinese

    for pat, phrase_wc, fixup in _COLLOQUIAL_PATTERNS:
        if pat.search(eng_lower) and phrase_wc / eng_wc >= 0.6:
            return fixup
    return chinese


# ============================================================
# 口语翻译 Few-shot 示例 — 展示地道翻译 + ASR 纠错
# ============================================================
# 原始英文来自 WhisperX 语音识别，可能含转录错误（发音相近词、漏词等）。
# few-shot 示范：理解真实语义后翻译为地道中文，而非直译错误。
_DIALOGUE_FEWSHOT = (
    "示例（原文可能有转录错误，翻译时修正；展示地道中文口语 vs 错误直译）：\n"
    "[1] I was like wait are you serious right now.\n"
    "[2] Yeah a hundred percent I'm not joking.\n"
    "[3] I new there was something off about that.\n"
    "→ [1] 我当时心想，等等，你说真的吗。\n"
    "  ✗ 直译错误示范：「我就像等等你现在是认真的吗」\n"
    "→ [2] 对，百分之百，我没跟你开玩笑。\n"
    "→ [3] 我就知道这事儿有蹊跷。\n\n"
    "[4] They was saying its gonna be huge.\n"
    "[5] But to be honest I don't buy it.\n"
    "[6] Fair enough I get your point.\n"
    "→ [4] 他们说这事儿会搞得很大。\n"
    "  ✗ 直译错误示范：「他们说它将会是巨大的」\n"
    "→ [5] 但说实话，我不太信。\n"
    "  ✗ 直译错误示范：「但是说实话我不买它」\n"
    "→ [6] 也是，我明白你的意思。\n\n"
    "[7] That's literally insane how much they charge for this.\n"
    "[8] Right like at the end of the day it's not worth it.\n"
    "[9] I mean if you're on a budget you gotta cut corners somewhere.\n"
    "→ [7] 他们收这个价也太离谱了吧，真的。\n"
    "  ✗ 直译错误示范：「那从字面上是疯狂的他们收多少钱」\n"
    "→ [8] 对，说到底就不值那个价。\n"
    "→ [9] 我是说，手头紧的话，总得在哪儿省一省。\n"
)

_SINGLE_FEWSHOT = (
    "示例（原文可能有转录错误，翻译时修正；地道口语翻译）：\n"
    "英文：I new there was something off about that.\n"
    "中文：我就知道这事儿有蹊跷。\n"
    "英文：They was saying its gonna be huge but I don't buy it.\n"
    "中文：他们说这事儿会搞得很大，但我不太信。\n"
    "英文：That's literally insane how much they charge for this.\n"
    "中文：他们收这个价也太离谱了吧，真的。\n"
    "英文：I mean at the end of the day you gotta do what you gotta do.\n"
    "中文：我是说，说到底，该做的还是得做。\n"
)


def _build_context_section(
        prev_context: Optional[list[tuple[str, str]]] = None,
        simple: bool = False) -> str:
    """
    构建跨 batch 前文上下文片段（供各 prompt 复用，避免重复代码）。

    Args:
        prev_context: 上一批末尾的上下文 [(english原文, 中文翻译), ...]
        simple: True 用极简措辞（降级 prompt），False 用完整措辞

    Returns:
        str: 上下文片段（无上下文时返回空串）
    """
    if not prev_context:
        return ""
    ctx_lines = "\n".join(f"  {eng} → {chi}" for eng, chi in prev_context)
    if simple:
        return f"前文参考（承接前文语境，保持人称、术语与语气连贯）：\n{ctx_lines}\n\n"
    return (
        f"## 前文参考\n"
        f"上一批已翻译（英文→中文）：\n{ctx_lines}\n"
        f"当前句子承接前文语境，请保持人称、术语与语气连贯。\n\n"
    )


def build_translation_prompt(
        sentences: list[tuple[int, str]],
        prev_context: Optional[list[tuple[str, str]]] = None) -> str:
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
    context_section = _build_context_section(prev_context)

    if is_dialogue:
        # ---- 多句（对话/段落）模式 ----
        # 不使用「你是...」等角色扮演开头，translategemma 容易将其理解为元指令并回复确认语
        return (
            f"将以下 {len(sentences)} 行英文播客口语翻译为地道中文口语。直接输出，不要确认语。\n\n"
            f"提示：原文来自语音识别，可能含转录错误。理解真实语义后再翻译。\n\n"
            f"{context_section}"
            f"要求：\n"
            f"1. 地道中文：用母语者日常聊天的表达方式，不要书面语或翻译腔\n"
            f"2. 习语意译：口语习语翻实际含义，不字面直译\n"
            f"3. 纠错润色：识别转录错误（如发音相近词、漏词），按正确意思翻译，不要直译错误\n"
            f"4. 应答短句简短（如\"A hundred percent.\"→\"百分之百确定。\"），叙述长句完整自然\n\n"
            f"{_DIALOGUE_FEWSHOT}"
            f"按 [N] 中文翻译 格式输出：\n\n"
            f"{numbered}"
        )
    else:
        # ---- 单句模式 ----
        return (
            f"将以下英文翻译为地道中文口语。只输出中文翻译，不要确认语。\n\n"
            f"提示：原文可能有转录错误，理解真实语义后翻译。\n\n"
            f"{context_section}"
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

    为降低误判：仅当文本较短（<= _META_MAX_LEN 字符）时才检测，
    避免长篇正常翻译中恰好含「请告诉我」等短语被误判。
    ^ 锚定模式启用 MULTILINE，使模型先输出空行时也能命中行首。

    Args:
        text: LLM 回复文本

    Returns:
        bool: True 表示这是元响应（需要降级重试）
    """
    if not text or not text.strip():
        return False
    stripped = text.strip()
    # 元响应通常很短（确认语不会是长翻译），限制长度避免误判正常翻译
    if len(stripped) > _META_MAX_LEN:
        return False
    for pattern in _META_RESPONSE_PATTERNS:
        if re.search(pattern, stripped, re.MULTILINE):
            return True
    return False


def _build_direct_prompt(
        sentences: list[tuple[int, str]],
        prev_context: Optional[list[tuple[str, str]]] = None) -> str:
    """
    构建极简直接的降级翻译 prompt，用于元响应重试。

    特点：去掉所有角色扮演和复杂指令，只用一句祈使句 + 编号内容。
    translategemma 对这种格式的依从性更高。

    Args:
        sentences: 待翻译的句子列表 [(seq_id, text), ...]
        prev_context: 上一批末尾的上下文 [(english原文, 中文翻译), ...]
    """
    numbered = "\n".join(f"[{seq_id}] {text}" for seq_id, text in sentences)
    context_section = _build_context_section(prev_context, simple=True)

    return (
        f"将以下英文翻译为地道中文口语。原文可能有转录错误，理解真实语义后翻译，直接输出结果，不要确认语。\n\n"
        f"{context_section}"
        f"{numbered}\n"
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
    # 括号内以拉丁字母（含拼音声调符号）开头才视为拼音注释并移除
    # 模式1: "中文 (Wǒ méiyǒu...)" → 去掉末尾圆括号及括号内内容
    text = re.sub(r'\s*\(' + _LATIN_RE + r'[^)]*\)\s*$', '', text)
    # 模式2: "中文 [Wǒ méiyǒu...]" → 去掉末尾方括号
    text = re.sub(r'\s*\[' + _LATIN_RE + r'[^\]]*\]\s*$', '', text)
    return text.strip()


def parse_translation_response(response_text: str, expected_ids: list[int]) -> dict[int, str]:
    """
    解析 TranslateGemma 的批量翻译纯文本响应。

    优先按 [N] 前缀匹配；对前缀匹配缺失的 id，用非前缀行按位置顺序补充，
    避免部分带前缀、部分不带前缀时丢失句子。同一 [N] 多次出现只保留首次。

    Args:
        response_text: LLM 回复文本
        expected_ids: 期望的 seq_id 列表（按顺序）

    Returns:
        dict: {seq_id: chinese_translation}
    """
    if not response_text or not response_text.strip():
        return {}

    lines = response_text.strip().split("\n")
    result: dict[int, str] = {}

    # 第一轮：按 [N] 前缀匹配（同一 id 只保留首次出现的翻译）
    id_prefix = re.compile(r'^\[(\d+)\]\s*(.*)')
    non_prefixed_lines: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = id_prefix.match(line)
        if m:
            sid = int(m.group(1))
            translation = _clean_pinyin(m.group(2).strip())
            if translation and sid not in result:
                result[sid] = translation
        else:
            cleaned = _clean_pinyin(line)
            if cleaned:
                non_prefixed_lines.append(cleaned)

    # 第二轮：前缀匹配缺失的 id，用非前缀行按位置顺序补充
    missing_ids = [sid for sid in expected_ids if sid not in result]
    for i, sid in enumerate(missing_ids):
        if i < len(non_prefixed_lines):
            result[sid] = non_prefixed_lines[i]

    return result


def _call_llm(prompt: str, max_retries: int = _MAX_RETRIES) -> str:
    """
    带重试退避的 LLM 调用封装（使用翻译专用 temperature）。

    call_ollama 在连接失败时会 sys.exit(1)，此处捕获 SystemExit 以避免
    整个翻译流程被杀掉（已翻译的批次虽由 checkpoint 保存，但进程退出
    体验差）。对超时/空响应做指数退避重试。

    Args:
        prompt: 提示词
        max_retries: 最大重试次数

    Returns:
        str: 模型回复文本（全部失败时返回空串）
    """
    translate_temp = float(getattr(config, "LLM_TRANSLATE_TEMPERATURE", 0.6))
    for attempt in range(1, max_retries + 1):
        try:
            resp = call_ollama(prompt, temperature=translate_temp)
        except SystemExit:
            # call_ollama 连接失败触发 sys.exit，阻止其杀掉整个进程
            wait = _RETRY_BACKOFF * attempt
            print(f"  [Step2b] LLM 连接失败（第 {attempt}/{max_retries} 次），"
                  f"{wait:.0f}s 后重试")
            time.sleep(wait)
            continue
        if resp and resp.strip():
            return resp
        wait = _RETRY_BACKOFF * attempt
        print(f"  [Step2b] LLM 返回空响应（第 {attempt}/{max_retries} 次），"
              f"{wait:.0f}s 后重试")
        time.sleep(wait)
    print(f"  [Step2b] LLM 调用重试 {max_retries} 次仍失败，放弃此批")
    return ""


def translate_sentences(
        segments: list[dict[str, Any]],
        indices: Optional[list[int]] = None,
        batch_size: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        resume_batch: int = 0,
        existing_translations: Optional[dict[int, str]] = None,
        checkpoint_cb: Optional[Callable[[int, dict[int, str]], None]] = None) -> dict[int, str]:
    """
    批量翻译指定的句子，适配 TranslateGemma。
    支持断点续跑：resume_batch 跳过已完成批次，existing_translations 预填充。

    Args:
        segments: WhisperX segments 列表
        indices: 需要翻译的 segment 索引列表，None 则翻译全部
        batch_size: 每批翻译的句子数量
        cancel_check: 终止检查回调，返回 True 则中断任务
        progress_cb: 进度回调 (batch_idx, total_batches)
        resume_batch: 从第几个批次开始续跑（0-based），跳过之前的批次
        existing_translations: 已有翻译 {segment_index: chinese}，用于断点续跑预填充
        checkpoint_cb: 每批完成后的断点保存回调 (completed_batches, translations_copy)

    Returns:
        dict: {segment_index: chinese_translation}
    """
    if indices is None:
        indices = list(range(len(segments)))

    if batch_size is None:
        batch_size = int(getattr(config, "LLM_BATCH_SIZE", 8))

    # 构建 (索引, 文本) 对
    sentence_pairs: list[tuple[int, str]] = []
    for idx in indices:
        if idx < len(segments):
            text = str(segments[idx].get("text", "")).strip()
            if text:
                sentence_pairs.append((idx, text))

    if not sentence_pairs:
        print("[Step2b] 没有需要翻译的句子")
        return {}

    # 用 1-based 顺序编号作为 prompt 中的标记，seq_to_idx 映射回实际 segment 索引
    seq_to_idx: dict[int, int] = {}
    numbered_pairs: list[tuple[int, str]] = []
    for seq_id, (idx, text) in enumerate(sentence_pairs, start=1):
        seq_to_idx[seq_id] = idx
        numbered_pairs.append((seq_id, text))

    # 分批
    batches: list[list[tuple[int, str]]] = []
    for i in range(0, len(numbered_pairs), batch_size):
        batches.append(numbered_pairs[i:i + batch_size])

    total_batches = len(batches)
    start_batch = resume_batch if resume_batch < total_batches else total_batches

    all_translations: dict[int, str] = dict(existing_translations) if existing_translations else {}
    if start_batch > 0:
        print(f"[Step2b] 从批次 {start_batch + 1}/{total_batches} 续跑 "
              f"(已跳过前 {start_batch} 批，已有 {len(all_translations)} 个翻译)")
        # 续跑时先报告已完成的进度，避免进度条停滞
        if progress_cb:
            progress_cb(start_batch, total_batches)

    print(f"[Step2b] 开始逐批翻译，共 {len(numbered_pairs)} 个句子，"
          f"每批 {batch_size} 句，分为 {total_batches} 批")

    # 跨 batch 上下文：记录上一批末尾 N 句的 (英文原文, 中文翻译)
    # 续跑时从已有翻译重建上下文，保持跨 batch 连贯性
    prev_context: list[tuple[str, str]] = []
    if start_batch > 0 and existing_translations:
        tail_pairs = [
            (idx, str(segments[idx].get("text", "")).strip())
            for idx, _ in sentence_pairs
            if idx in existing_translations
        ][-_CONTEXT_WINDOW:]
        prev_context = [(eng, str(existing_translations[idx])) for idx, eng in tail_pairs]

    prompt_logged = 0  # 只打印前若干个 prompt 方便调试

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
        if prompt_logged < _MAX_PROMPT_LOG:
            prompt_logged += 1
            print(f"  [Prompt #{prompt_logged}] 长度 {len(prompt)} 字符:")
            # 只打印 prompt 的句子部分（跳过系统指令），最多 _PROMPT_SNIPPET_LEN 字符
            prompt_lines = prompt.split("\n")
            sentence_start = next((i for i, ln in enumerate(prompt_lines) if ln.startswith("[")), 0)
            snippet = "\n".join(prompt_lines[sentence_start:])
            if len(snippet) > _PROMPT_SNIPPET_LEN:
                snippet = snippet[:_PROMPT_SNIPPET_LEN] + "..."
            print(f"  {snippet}")
            print(f"  ---")
        response = _call_llm(prompt)
        batch_translations = parse_translation_response(response, expected_ids)

        # 检测元响应：若模型返回了确认语而非翻译，用极简 prompt 重试批次
        if not batch_translations and _is_meta_response(response):
            print(f"  [元响应检测] 模型返回了确认语而非翻译，使用极简 prompt 重试")
            direct_prompt = _build_direct_prompt(batch, prev_context)
            response = _call_llm(direct_prompt)
            batch_translations = parse_translation_response(response, expected_ids)

        elapsed = time.time() - t0
        print(f"         耗时 {elapsed:.1f}s，翻译到 {len(batch_translations)} 句")

        # 合并结果：将 seq_id 映射回实际 segment 索引
        # 同时收集本批的英文→中文对，供下一批做上下文
        batch_english_chi: list[tuple[str, str]] = []  # 本批所有翻译的结果
        for seq_id, text in batch:
            actual_idx = seq_to_idx[seq_id]
            if seq_id in batch_translations and batch_translations[seq_id]:
                # 口语习语兜底修正（拼音已在 parse 阶段清洗）
                translation = batch_translations[seq_id]
                translation = _apply_colloquial_fixup(text, translation)
                all_translations[actual_idx] = translation
                batch_english_chi.append((text, translation))
            else:
                # 单句重试（使用单句 prompt）
                print(f"  [重试] 句子 {actual_idx} 单句重试...")
                retry_prompt = build_translation_prompt([(1, text)], prev_context)
                retry_resp = _call_llm(retry_prompt)
                # 检测元响应：若返回确认语，用极简 prompt 重试
                if _is_meta_response(retry_resp):
                    print(f"    [元响应] 单句也返回了确认语，使用极简 prompt")
                    retry_resp = _call_llm(_build_direct_prompt([(1, text)], prev_context))
                retry_text = _clean_pinyin(retry_resp.strip()) if retry_resp else ""
                if retry_text:
                    retry_text = _apply_colloquial_fixup(text, retry_text)
                    all_translations[actual_idx] = retry_text
                    batch_english_chi.append((text, retry_text))
                else:
                    print(f"  [失败] 句子 {actual_idx} 翻译失败，跳过")

        # 更新跨 batch 上下文：取本批最后 N 条翻译
        if batch_english_chi:
            prev_context = batch_english_chi[-_CONTEXT_WINDOW:]

        # 每批完成后保存断点
        if checkpoint_cb:
            checkpoint_cb(batch_idx + 1, dict(all_translations))

    print(f"[Step2b] 翻译完成: {len(all_translations)}/{len(sentence_pairs)} 句")
    return all_translations
