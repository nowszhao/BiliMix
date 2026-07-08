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
from core.llm_utils import call_ollama

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
    # 否定/部分否定
    "not exactly": "也不完全是",
    "not really": "也不是",
    "not necessarily": "不一定",
    "not quite": "还差点意思",
    "not at all": "完全没有",
    "not even close": "差远了",
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
    "sort of": "算是吧",
    "kind of": "有点",
    "pretty much": "差不多",
    "at the end of the day": "说到底",
    "to be honest": "说实话",
    "honestly": "说真的",
    "basically": "基本上",
    "literally": "简直",
    "apparently": "显然",
    "technically": "严格来说",
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
    "输入：\n"
    "[1] I was like wait are you serious right now.\n"
    "[2] Yeah a hundred percent I'm not joking.\n"
    "[3] I new there was something off about that.\n"
    "输出（严格按此格式，每行只有 [N] + 中文翻译）：\n"
    "[1] 我当时心想，等等，你说真的吗。\n"
    "[2] 对，百分之百，我没跟你开玩笑。\n"
    "[3] 我就知道这事儿有蹊跷。\n\n"
    "输入：\n"
    "[7] That's literally insane how much they charge for this.\n"
    "[8] Right like at the end of the day it's not worth it.\n"
    "[9] I mean if you're on a budget you gotta cut corners somewhere.\n"
    "输出：\n"
    "[7] 他们收这个价也太离谱了吧，真的。\n"
    "[8] 对，说到底就不值那个价。\n"
    "[9] 我是说，手头紧的话，总得在哪儿省一省。\n"
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
    清洗翻译文本：
    1. 剥除前缀 `英文：` / `中文：` / `→` / `✗` / `[N]` 等角色标签
    2. 多行响应只取第一条有效翻译行（避免模型返回多条混入）
    3. 去除末尾括号中的拼音/英文注释（半角和全角括号）
    4. 去除末尾 LLM 元注释（宽泛匹配「不完整」「无法翻译」等关键词）
    5. 去除模型自行添加的尾部省略号（... / …）
    """
    if not text:
        return text

    # 1. 剥除「英文：」「中文：」「→」「✗」「[N] 编号」等前缀
    text = re.sub(
        r'^\s*(?:'
        r'英文\s*[：:]\s*'
        r'|中文\s*[：:]\s*'
        r'|英\s*[：:]\s*'
        r'|中\s*[：:]\s*'
        r'|→\s*'
        r'|✗\s*'
        r'|\[\d+\]\s*'
        r')+',
        '', text
    )

    # 2. 多行响应：只取第一条有效翻译行（过滤「英文：」开头的原文行）
    #    避免模型返回多条 [N] 翻译时全部混入一条结果
    if '\n' in text:
        for ln in text.split('\n'):
            ln = ln.strip()
            if not ln:
                continue
            if re.match(r'^(英文|英)\s*[：:]\s*', ln):
                continue
            text = ln
            break
        else:
            return ""

    # 3. 去除末尾括号中的拼音/英文注释（半角和全角括号，以拉丁字母开头）
    text = re.sub(r'\s*[（(]\s*' + _LATIN_RE + r'[^)）]*[)）]\s*$', '', text)
    text = re.sub(r'\s*[【\[]\s*' + _LATIN_RE + r'[^】\]]*[】\]]\s*$', '', text)

    # 4. 去除末尾 LLM 元注释（宽泛匹配含关键词的括号注释）
    #    覆盖「句子不完整」「无法翻译」「此处省略」「被截断」等各种措辞
    text = re.sub(
        r'\s*[（(][^)）]*'
        r'(?:不完整|无法翻译|不能翻译|无法完成|此处省略|待补充|'
        r'未完成|被截断|已截断|句子中断|内容缺失|翻译不了)'
        r'[^)）]*[)）]\s*$',
        '', text
    )

    # 5. 去除模型自行添加的尾部省略号
    text = re.sub(r'\s*\.{2,}\s*$', '', text)
    text = re.sub(r'\s*…\s*$', '', text)

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
    inline_id = re.compile(r'\[\d+\]')  # 行内残留编号检测
    non_prefixed_lines: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = id_prefix.match(line)
        if m:
            sid = int(m.group(1))
            translation = _clean_pinyin(m.group(2).strip())
            # 截断行内残留 [N] 编号（LLM 有时返回 "[1] 翻译A [2] 翻译B" 的单行）
            if translation:
                trunc = inline_id.search(translation)
                if trunc:
                    translation = translation[:trunc.start()].strip()
            if translation and sid not in result:
                result[sid] = translation
        else:
            cleaned = _clean_pinyin(line)
            if cleaned:
                # 截断行内残留 [N] 编号（同前缀行处理）
                trunc = inline_id.search(cleaned)
                if trunc:
                    cleaned = cleaned[:trunc.start()].strip()
                if cleaned:
                    non_prefixed_lines.append(cleaned)

    # 第二轮：前缀匹配缺失的 id，用非前缀行按位置顺序补充
    missing_ids = [sid for sid in expected_ids if sid not in result]
    for i, sid in enumerate(missing_ids):
        if i < len(non_prefixed_lines):
            result[sid] = non_prefixed_lines[i]

    return result


def _is_valid_translation(english: str, chinese: str) -> bool:
    """
    校验翻译结果是否合理，拦截明显的异常输出。

    检查项：
    1. 不应残留 [N] 编号标记（说明模型返回了多条翻译混在一起）
    2. 翻译长度不应远超原文（中文有效字符数 / 英文词数 <= 15）
    3. 翻译不应为空或纯空白

    Args:
        english: 英文原文
        chinese: 待校验的中文翻译

    Returns:
        bool: True 表示翻译合理
    """
    if not chinese or not chinese.strip():
        return False
    chinese = chinese.strip()

    # 1. 残留 [N] 标记 → 模型返回了多条翻译混在一起
    if re.search(r'\[\d+\]', chinese):
        return False

    # 2. 长度比异常（中文有效字符数 / 英文词数）
    eng_words = len(english.split()) if english and english.strip() else 1
    chi_chars = len(re.sub(r'[\s\W]', '', chinese, flags=re.UNICODE))
    if chi_chars == 0:
        return False
    if chi_chars / max(eng_words, 1) > 15:
        return False

    return True


_TRANSLATE_SYSTEM = (
    "你是一名专业的英中口语翻译。你的目标是输出母语级的地道中文口语，"
    "彻底消除翻译腔和机翻味。核心原则：\n\n"
    "1. 说人话：中文必须是口语对话里会出现的自然表达，不要书面语\n"
    "2. 懂言外之意：习语、俚语、反讽要翻译出实际含义，不要字面直译\n"
    "3. 纠错润色：原文可能含语音识别错误（如音近词错、漏词），"
    "按真实语义翻译，不要照搬错误文本\n"
    "4. 简洁有力：短应答就两个字搞定，长句保持语序流畅自然\n"
    "5. 输出格式铁律：\n"
    "   - 每行严格以 [N] 开头（N 是句号序号），后接中文翻译\n"
    "   - 绝对不要加「英文：」「中文：」「→」「以下是翻译」等任何标签或前缀\n"
    "   - 不要重复输出原文，不要续写多对英文/中文交替\n"
    "   - 不要加确认语、解释、注释或任何非翻译内容\n"
    "   - 输入 N 句就输出 N 行，缺一行视为错误"
)


def _call_llm(prompt: str, max_retries: int = _MAX_RETRIES,
             system: str = None) -> str:
    """
    带重试退避的 LLM 调用封装（使用翻译专用 temperature）。

    call_ollama 在连接失败时会 sys.exit(1)，此处捕获 SystemExit 以避免
    整个翻译流程被杀掉（已翻译的批次虽由 checkpoint 保存，但进程退出
    体验差）。对超时/空响应做指数退避重试。

    Args:
        prompt: 提示词
        max_retries: 最大重试次数
        system: 可选的 system prompt

    Returns:
        str: 模型回复文本（全部失败时返回空串）
    """
    translate_temp = float(getattr(config, "LLM_TRANSLATE_TEMPERATURE", 0.6))
    for attempt in range(1, max_retries + 1):
        try:
            resp = call_ollama(prompt, temperature=translate_temp, system=system)
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
        response = _call_llm(prompt, system=_TRANSLATE_SYSTEM)
        batch_translations = parse_translation_response(response, expected_ids)

        # 检测元响应：若模型返回了确认语而非翻译，用极简 prompt 重试批次
        if not batch_translations and _is_meta_response(response):
            print(f"  [元响应检测] 模型返回了确认语而非翻译，使用极简 prompt 重试")
            direct_prompt = _build_direct_prompt(batch, prev_context)
            response = _call_llm(direct_prompt, system=_TRANSLATE_SYSTEM)
            batch_translations = parse_translation_response(response, expected_ids)

        elapsed = time.time() - t0
        print(f"         耗时 {elapsed:.1f}s，翻译到 {len(batch_translations)} 句")

        # 合并结果：将 seq_id 映射回实际 segment 索引
        # 同时收集本批的英文→中文对，供下一批做上下文
        batch_english_chi: list[tuple[str, str]] = []  # 本批所有翻译的结果
        for seq_id, text in batch:
            actual_idx = seq_to_idx[seq_id]
            translation = batch_translations.get(seq_id, "")

            # 校验批次翻译结果：缺失或异常则转单句重试
            if translation and _is_valid_translation(text, translation):
                translation = _apply_colloquial_fixup(text, translation)
                all_translations[actual_idx] = translation
                batch_english_chi.append((text, translation))
                continue

            if translation:
                print(f"  [校验] 句子 {actual_idx} 翻译异常，转单句重试")
            print(f"  [重试] 句子 {actual_idx} 单句重试...")
            # 单句重试不传 prev_context，避免模型受前文诱导续写无关内容
            retry_prompt = build_translation_prompt([(1, text)], None)
            retry_resp = _call_llm(retry_prompt, system=_TRANSLATE_SYSTEM)
            # 检测元响应：若返回确认语，用极简 prompt 重试
            if _is_meta_response(retry_resp):
                print(f"    [元响应] 单句也返回了确认语，使用极简 prompt")
                retry_resp = _call_llm(_build_direct_prompt([(1, text)], None), system=_TRANSLATE_SYSTEM)

            # 用 parse_translation_response 正确解析，只取第一条翻译
            retry_parsed = parse_translation_response(retry_resp, [1])
            retry_text = retry_parsed.get(1, "")
            if not retry_text and retry_resp:
                # fallback: 取首行清洗
                retry_text = _clean_pinyin(retry_resp.strip().split('\n')[0])

            if retry_text and _is_valid_translation(text, retry_text):
                retry_text = _apply_colloquial_fixup(text, retry_text)
                all_translations[actual_idx] = retry_text
                batch_english_chi.append((text, retry_text))
            elif retry_text:
                print(f"  [校验失败] 句子 {actual_idx} 重试结果仍异常，跳过: {retry_text[:60]}...")
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
