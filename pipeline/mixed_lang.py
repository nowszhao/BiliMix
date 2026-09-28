"""
中英混排文本的语种切分与音频拼接。

背景（为什么需要这个模块）：
  Confucius4-TTS 是「单语种推理」——每次调用只能给一个 language token
  （见 confuciustts/cli/inference.py 的 _synth_segment），
  且 TextNormalizer.normalize() / segment_text() 也是按单一语种处理整段文本。
  因此一段里中英混排时，两种语言共用一套发音规则，中文会被英文腔带跑、
  咬字不清。

  解决办法：把混排文本按语种切成若干「同语种连续片段」（language run），
  每段用对应的 lang 单独合成，再把音频按原顺序拼回一个文件。

  注意：引擎对每段输出都会加 100ms 静音 pad + 100ms 淡入淡出
  （generate(edge_pad_duration=0.1, edge_fade_duration=0.1)），
  跨片段拼接时必须先裁掉这些边距，否则短中文词之间会出现明显停顿。
"""
import os
import re

# 语种分类用到的字符区间
_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
_CJK_PUNCT_RE = re.compile(r'[\u3000-\u303f\uff00-\uffef]')
_EN_CHAR_RE = re.compile(r"[A-Za-z0-9']")

# 单次切分允许的语种（后续要扩语种时在这里加）
SPLIT_LANGS = ("zh", "en")

# 过短的片段（低于该字符数）会并入相邻片段，避免把单个字单独送合成
MIN_RUN_CHARS = 2


def _classify(ch: str) -> str:
    """返回字符所属语种：'zh' / 'en' / ''（中立：空格、ASCII 标点等）"""
    if _CJK_RE.match(ch) or _CJK_PUNCT_RE.match(ch):
        return "zh"
    if _EN_CHAR_RE.match(ch):
        return "en"
    return ""


def split_language_runs(text: str) -> list:
    """把中英混排文本切成同语种连续片段。

    Returns:
        [{"lang": "en"|"zh", "text": str}, ...]

    规则：
      - 中立字符（空格、逗号、句号等）跟随它前面的片段，保持原有间距与语气；
      - 开头的连续中立字符并入第一个片段；
      - 相邻同语种片段合并；
      - 只有标点的片段被丢弃；
      - 短于 MIN_RUN_CHARS 的片段并入相邻片段，避免孤立单字合成。
    """
    if not text or not text.strip():
        return []

    runs = []          # [[lang, buf], ...]
    pending = ""       # 开头尚未归属的中立字符

    for ch in text:
        lang = _classify(ch)
        if not lang:
            if runs:
                runs[-1][1] += ch
            else:
                pending += ch
            continue

        if runs and runs[-1][0] == lang:
            runs[-1][1] += ch
        else:
            runs.append([lang, ch])

    if pending:
        if runs:
            runs[0][1] = pending + runs[0][1]
        # 全是中立字符时直接丢弃

    # 丢弃不含实义字符的片段（纯标点等）
    runs = [r for r in runs if _has_content(r[1])]
    if not runs:
        return []

    # 过短片段并入相邻片段：优先并入前一个（保持语流位置不变），
    # 若是首个片段则并入后一个
    merged = []
    for idx, (lang, buf) in enumerate(runs):
        if _content_len(buf) >= MIN_RUN_CHARS:
            merged.append([lang, buf])
            continue
        if merged:
            merged[-1][1] += buf
        elif idx + 1 < len(runs):
            runs[idx + 1][1] = buf + runs[idx + 1][1]
        else:
            merged.append([lang, buf])

    # 并入后可能出现相邻同语种片段，再合并一次
    final = []
    for lang, buf in merged:
        if final and final[-1][0] == lang:
            final[-1][1] += buf
        else:
            final.append([lang, buf])

    return [{"lang": lang, "text": buf.strip()} for lang, buf in final
            if buf.strip() and _has_content(buf)]


def _content_len(text: str) -> int:
    """实义字符数（不含空格与标点）"""
    return sum(1 for ch in text if _classify(ch))


def _has_content(text: str) -> bool:
    return _content_len(text) > 0


_END_PUNCT = "。！？，、；：!?.,;:"


def annotate_language_runs(text: str, target_lang: str = "zh",
                           prefix: str = ", ", suffix: str = ",",
                           min_chars: int = MIN_RUN_CHARS) -> str:
    """给指定语种的片段两侧插入停顿标注（默认逗号），其余文本原样保留。

    用途：整段用同一套发音规则合成时（语流最连贯），靠标点让中文前后
    各有一个短停顿，听起来更清楚。

    注意：引号类符号不是韵律标记，会被文本规范化环节去掉、不产生停顿，
    所以这里用逗号这类真实标点。片段自身或相邻位置已经有标点时不再重复添加。
    """
    if not text or not text.strip():
        return text

    runs = split_language_runs(text)
    if not runs or all(r["lang"] == target_lang for r in runs):
        return text                      # 没有混排，或整段都是目标语种

    out = []
    i, n = 0, len(text)
    while i < n:
        if _classify(text[i]) != target_lang:
            out.append(text[i])
            i += 1
            continue

        j = i
        while j < n and _classify(text[j]) == target_lang:
            j += 1
        body = text[i:j]

        if _content_len(body) >= min_chars:
            before = text[:i].rstrip()
            after = text[j:].lstrip()
            add_prefix = (body[:1] not in _END_PUNCT
                          and not (before and before[-1] in _END_PUNCT))
            add_suffix = bool(after) and body[-1:] not in _END_PUNCT \
                and after[0] not in _END_PUNCT
            out.append((prefix if add_prefix else "") + body
                       + (suffix if add_suffix else ""))
        else:
            out.append(body)
        i = j

    result = "".join(out)
    result = re.sub(r'\s*,\s*', ', ', result)
    result = re.sub(r'\s+', ' ', result)
    return result.strip()


def build_speech_plan(texts: list, lang: str = "auto") -> tuple:
    """为多段文本构建「按语种拆分」的合成清单。

    Args:
        texts: 原始文本列表
        lang: "auto" 时按语种切分；其他值表示不切分（整段用该语种）

    Returns:
        (items, groups)
        items: 扁平合成清单 [{"lang": str, "text": str, "group": int}]
        groups: {原文本下标: [item 下标, ...]}（拼接时按此还原）
    """
    items = []
    groups = {}
    for group, text in enumerate(texts):
        if lang == "auto":
            runs = split_language_runs(text)
        else:
            runs = [{"lang": lang, "text": text.strip()}]
        idxs = []
        for run in runs:
            if not run["text"]:
                continue
            idxs.append(len(items))
            items.append({"lang": run["lang"], "text": run["text"],
                          "group": group})
        groups[group] = idxs
    return items, groups


# ============================================================
# 音频拼接
# ============================================================

def _trim_edges(samples, sample_rate, max_trim_ms=260, keep_ms=20,
                threshold_ratio=0.02):
    """裁掉片段首尾的静音 pad / 淡入淡出残留。

    引擎为每段加了 100ms pad + 100ms fade，跨片段拼接若不去掉，
    短词之间会出现明显停顿。这里按幅度阈值裁剪，并限制最大裁剪长度，
    避免误伤真正轻声的起音。
    """
    import numpy as np

    if samples.size == 0:
        return samples

    peak = float(np.max(np.abs(samples)))
    if peak <= 0:
        return samples

    threshold = max(peak * threshold_ratio, 1e-4)
    loud = np.where(np.abs(samples) > threshold)[0]
    if loud.size == 0:
        return samples

    max_trim = int(sample_rate * max_trim_ms / 1000)
    keep = int(sample_rate * keep_ms / 1000)

    start = max(0, int(loud[0]) - keep)
    end = min(samples.size, int(loud[-1]) + keep + 1)
    if start > max_trim:
        start = max_trim if start - max_trim > 0 else start
    return samples[start:end]


def splice_wavs(paths: list, out_path: str, crossfade_ms=25):
    """把多个 WAV 片段按顺序拼成一个文件（带短交叉淡化）。

    用于把按语种拆分合成的片段还原成一句话/一段话。

    Args:
        paths: 待拼接的 WAV 路径（顺序即播放顺序）
        out_path: 输出 WAV 路径
        crossfade_ms: 片段之间的交叉淡化时长（毫秒），消除切换爆音

    Returns:
        输出文件路径
    """
    import numpy as np
    import soundfile as sf

    parts = []
    sample_rate = None
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        if data.size == 0:
            continue
        if data.shape[1] > 1:                      # 多声道压成单声道
            data = data.mean(axis=1, keepdims=True)
        if sample_rate is None:
            sample_rate = sr
        elif sr != sample_rate:                    # 采样率不一致则重采样
            ratio = sample_rate / sr
            new_len = int(data.shape[0] * ratio)
            idx = np.linspace(0, data.shape[0] - 1, new_len)
            data = np.interp(idx, np.arange(data.shape[0]),
                             data[:, 0])[:, None]
        parts.append(_trim_edges(data[:, 0], sample_rate))

    if not parts:
        raise ValueError("没有可拼接的音频片段")
    if sample_rate is None:
        raise ValueError("无法确定采样率")

    fade = max(1, int(sample_rate * crossfade_ms / 1000))
    merged = parts[0]
    for nxt in parts[1:]:
        if merged.size < fade or nxt.size < fade:
            merged = np.concatenate([merged, nxt])
            continue
        head = nxt[:fade]
        tail = merged[-fade:]
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        blended = tail * (1.0 - ramp) + head * ramp
        merged = np.concatenate([merged[:-fade], blended, nxt[fade:]])

    peak = float(np.max(np.abs(merged)))
    if peak > 1.0:                                 # 防削波
        merged = merged / peak * 0.99

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    sf.write(out_path, merged, sample_rate)
    return out_path
