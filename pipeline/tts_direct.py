"""
独立 TTS 合成（不经翻译流水线）。

场景：给一段参考音频 + 一段（或多段）文本，直接用 Confucius4-TTS-CPU
做零样本声音克隆合成，不需要转录、翻译、混音等上游产物。

实现要点：
  - 复用 step3 的 worker 调度（子进程管理、超时、取消、并行分片、磁盘缓存），
    不重复实现任何 subprocess 逻辑；
  - 通过「伪 segment」把 (文本, 参考音频) 对喂给
    synthesize_sentences_with_confucius_tts，该函数在 ref_audio_map 完整时不依赖
    上游 segments / audio_path；
  - 产物按文件名（文本+参考音频+语种的 md5）缓存，重复提交可秒级命中。
"""
import hashlib
import os

from core import config
from pipeline.mixed_lang import build_speech_plan, splice_wavs
from pipeline.step3_tts_confucius import synthesize_sentences_with_confucius_tts

# Confucius4-TTS 支持的语种（见 confuciustts/utils/text_utils.py LANGUAGE_TOKEN_MAP）
SUPPORTED_LANGS = ("zh", "en", "ja", "ko")
# 中英混排专用：按语种切分后分别合成，再拼回一条音频
AUTO_LANG = "auto"
DEFAULT_LANG = "zh"

# 单次请求的文本条数与单条长度上限（防止误传超大文本拖垮远端）
MAX_TEXTS = 200
MAX_TEXT_CHARS = 5000


def build_job_id(ref_audio: str, texts: list, lang: str) -> str:
    """由「参考音频 + 文本列表 + 语种」生成确定性 job_id。

    相同输入得到相同 ID，因此重复提交会复用同一目录与 TTS 磁盘缓存。
    """
    raw = "|".join([os.path.abspath(ref_audio), lang, *texts])
    return "tts_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def get_job_dir(job_id: str) -> str:
    """返回 job 的产物目录（位于 RESULT_DIR 之下，可经 /api/audio 下载）。"""
    return os.path.join(config.TTS_JOB_DIR, job_id)


def validate_texts(texts) -> list:
    """校验并规范化文本列表，返回去除首尾空白的文本列表。

    非法输入抛 ValueError（由 API 层转成 400）。
    """
    if not isinstance(texts, (list, tuple)):
        raise ValueError("texts 必须是数组")
    cleaned = []
    for item in texts:
        if not isinstance(item, str):
            raise ValueError("texts 中的每一项都必须是字符串")
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"单条文本超过 {MAX_TEXT_CHARS} 字符上限")
        cleaned.append(text)
    if not cleaned:
        raise ValueError("没有有效的文本（不能为空）")
    if len(cleaned) > MAX_TEXTS:
        raise ValueError(f"单次最多合成 {MAX_TEXTS} 条文本，当前 {len(cleaned)} 条")
    return cleaned


def validate_lang(lang: str) -> str:
    """校验语种，非法值抛 ValueError。"""
    lang = (lang or DEFAULT_LANG).strip().lower()
    if lang == AUTO_LANG:
        return lang
    if lang not in SUPPORTED_LANGS:
        raise ValueError(
            f"不支持的语种: {lang}，可选 {', '.join(SUPPORTED_LANGS)} 或 {AUTO_LANG}")
    return lang


def synthesize_texts(texts: list, ref_audio: str, job_id: str,
                     lang: str = DEFAULT_LANG, cancel_check=None,
                     progress_cb=None) -> list:
    """用一段参考音频合成多段文本，返回有序的产物列表。

    Args:
        texts: 待合成文本列表（已校验）
        ref_audio: 服务端参考音频绝对路径（声音克隆的音色来源）
        job_id: 任务 ID，产物写入 data/results/tts/<job_id>/
        lang: 合成语种；传 "auto" 时按中英语种切分分别合成再拼回一条
        cancel_check: 终止检查回调
        progress_cb: 进度回调 (current, total)，current 为已完成片段数

    Returns:
        [{"index": int, "text": str, "output_path": str, "part_count": int}, ...]

    Raises:
        InterruptedError: 被用户终止
        RuntimeError: 存在未能合成的条目
    """
    if not os.path.isfile(ref_audio):
        raise ValueError(f"参考音频不存在: {ref_audio}")

    job_dir = get_job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)

    # 按语种切分：lang="auto" 时一段文本可能拆成多个同语种片段
    items, groups = build_speech_plan(texts, lang)
    if not items:
        raise ValueError("没有可合成的文本")

    segments = list(range(len(items)))
    translations = {i: items[i]["text"] for i in segments}
    ref_audio_map = {i: ref_audio for i in segments}
    seg_lang_map = {i: items[i]["lang"] for i in segments}

    # segments / audio_path 在 ref_audio_map 完整时不会被使用，
    # 这里传占位值以复用句子级合成逻辑。
    tts_map = synthesize_sentences_with_confucius_tts(
        [], segments, translations, "", job_dir,
        ref_audio_map=ref_audio_map,
        cancel_check=cancel_check,
        progress_cb=progress_cb,
        task_id=job_id,
        lang="zh" if lang == AUTO_LANG else lang,
        seg_lang_map=seg_lang_map,
    )

    outputs = []
    missing = []
    for group, text in enumerate(texts):
        parts = [tts_map.get(i, "") for i in groups.get(group, [])]
        if not parts or any(not p or not os.path.isfile(p) for p in parts):
            missing.append(group)
            continue

        if len(parts) == 1:
            output_path = parts[0]
        else:
            # 拼接文件名带上片段指纹，避免切分规则变化后误用旧的合并结果
            fingerprint = hashlib.md5(
                "|".join(os.path.basename(p) for p in parts).encode()
            ).hexdigest()[:12]
            output_path = os.path.join(
                job_dir, f"merged_{group:04d}_{fingerprint}.wav")
            if not os.path.isfile(output_path):
                splice_wavs(parts, output_path)

        outputs.append({"index": group, "text": text,
                        "output_path": output_path,
                        "part_count": len(parts)})

    if missing:
        raise RuntimeError(
            f"{len(missing)}/{len(texts)} 条文本合成失败（索引 {missing}）")

    return outputs
