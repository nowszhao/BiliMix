"""Reproducible narration, conservative acoustic checks and committed TTS files.

No model is loaded here. The worker supplies generate(text, seed, segment_limit).
Timing checks are acoustic heuristics. They cannot verify pronunciation,
speaker identity or omitted/repeated words.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

import numpy as np

from core.files import atomic_json


POLICY_VERSION = 2
SEED_VERSION = 1  # Keep the same first reading when only the acceptance rules change.
MAX_ATTEMPTS = 4
TARGET_CPS = 4.8
# Keep neighbouring lines within ~±8% of the target rate; the old 3.5-6.0
# band let consecutive sentences differ by 1.7x, which sounds uneven.
SOFT_CPS = (4.4, 5.2)
HARD_CPS = (2.0, 8.0)
SHORT_REPLY_ALLOWANCE_SECONDS = 1.0
SHORT_ZH_TEXT = re.compile(r"""[\u3400-\u9fff\s，。！？、；：…—“”‘’「」『』（）,.!?;:'"()]+""")
EDGE_KEEP_MS = 60


class NarrationQualityError(RuntimeError):
    def __init__(self, report):
        super().__init__('TTS 音频未通过节奏检查；请检查中文标点、参考音频或模型输出后重试')
        self.report = report


def plan_narration(text, language):
    """Keep short utterances intact; split long Chinese only at punctuation.

    One input unit still produces one output WAV. No caption/source timing or
    speaker boundary is changed. Unpunctuated clauses over 120 chars need editing.
    """
    text = text.strip()
    if not text:
        raise ValueError('TTS text is empty')
    if language != 'zh' or len(text) <= 80:
        return [text]
    # Keep closing quotation marks with the sentence, and accept ASCII Chinese
    # punctuation without splitting decimal numbers, times or digit grouping.
    separator = r'(?:[，；：。！？!?;]|(?<!\d)[,:.]|[,:.](?!\d))+'
    clauses = re.findall(r'.+?(?:' + separator + r'[”’」』）)"\']*|$)', text, flags=re.S)
    chunks, current = [], ''
    for clause in clauses:
        if len(clause) > 120:
            raise ValueError('中文长句缺少自然标点，请补充逗号或句号；不能按字数截断话语')
        if current and len(current + clause) > 80:
            chunks.append(current)
            current = ''
        current += clause
    if current:
        chunks.append(current)
    return chunks


def _measure(audio, sample_rate, text, language):
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1 or not audio.size or not np.isfinite(audio).all():
        return None, dict(reasons=['invalid_waveform'], chars_per_second=None)
    peak = float(np.max(np.abs(audio)))
    if peak < .0001:
        return None, dict(reasons=['silent_waveform'], chars_per_second=None)
    # 10ms RMS windows, a conservative threshold and 60ms margins protect quiet
    # consonants. Only outer silence is trimmed, never pauses inside the words.
    window = max(1, round(sample_rate * .01))
    padded = np.pad(audio, (0, (-len(audio)) % window))
    rms = np.sqrt(np.mean(padded.reshape(-1, window).astype(np.float64)**2, axis=1))
    active = rms > max(.0001, min(.003, float(rms.max())*.01))
    indices = np.flatnonzero(active)
    if not len(indices):
        return None, dict(reasons=['silent_waveform'], chars_per_second=None)
    first, last = int(indices[0]), int(indices[-1])
    span = (last-first+1)*window/sample_rate
    pauses = np.diff(indices)-1
    longest_pause = float(max(pauses, default=0)*window/sample_rate)
    margin = round(sample_rate*EDGE_KEEP_MS/1000)
    start, end = max(0, first*window-margin), min(len(audio), (last+1)*window+margin)
    cleaned = audio[start:end].copy()
    han = len(re.findall(r'[\u3400-\u9fff]', text))
    other = len(re.findall(r'[A-Za-z0-9]', text))
    # Mixed language, numbers and very short answers need semantic/ASR checks,
    # not an invented conversion from characters to spoken syllables.
    rate = han/span if language == 'zh' and han >= 12 and other == 0 else None
    reasons = []
    if span < .15:
        reasons.append('speech_too_short')
    if longest_pause > 1.2:
        reasons.append('long_internal_pause')
    # For short replies, allow emphasis and count only active windows so a
    # comma or quiet padding cannot turn an ordinary delivery into an outlier.
    active_seconds = len(indices) * window / sample_rate
    if language == 'zh' and 0 < han < 12 and SHORT_ZH_TEXT.fullmatch(text):
        if active_seconds > SHORT_REPLY_ALLOWANCE_SECONDS + han / HARD_CPS[0]:
            reasons.append('short_utterance_too_slow')
    if rate is not None and not HARD_CPS[0] <= rate <= HARD_CPS[1]:
        reasons.append('extreme_speech_rate')
    if float(np.mean(np.abs(audio) >= .999)) > .01:
        reasons.append('severe_clipping')
    return cleaned, dict(duration_seconds=round(len(cleaned)/sample_rate,4),
        speech_span_seconds=round(span,4), active_seconds=round(active_seconds,4),
        chars_per_second=round(rate,3) if rate else None,
        longest_internal_pause_seconds=round(longest_pause,4),
        trimmed_head_ms=round(start*1000/sample_rate),
        trimmed_tail_ms=round((len(audio)-end)*1000/sample_rate), reasons=reasons)


def synthesize_narration(text, language, reference_hash, generate, sample_rate, seed_context='',
                         *, max_attempts=MAX_ATTEMPTS):
    """Retry outliers without time-stretching or switching speaker references.

    Typical Chinese rate candidates stop immediately. Up to three deterministic
    candidates are compared for outliers; unsafe outputs fail, mild remaining
    deviations are retained with a warning. Seeds do not depend on queue order.
    """
    if sample_rate <= 0:
        raise ValueError('Invalid sample rate')
    chunks = plan_narration(text, language)
    report = dict(schema_version=1, policy_version=POLICY_VERSION, language=language,
                  reference_sha256=reference_hash, chunk_count=len(chunks), chunks=[])
    outputs = []
    for index, chunk in enumerate(chunks):
        item = dict(index=index, text_sha256=hashlib.sha256(chunk.encode()).hexdigest(), attempts=[])
        report['chunks'].append(item)
        best = None
        for attempt in range(max_attempts):
            identity = json.dumps([SEED_VERSION, reference_hash, language, chunk,
                                   seed_context, attempt], ensure_ascii=False)
            seed = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4], 'big')
            # BiliMix owns Chinese phrasing; prevent the engine from hard-cutting
            # a planned clause at its internal default 80-character boundary.
            limit = max(256, len(chunk)*4) if language == 'zh' else 80
            raw = generate(chunk, seed, limit)
            audio, metrics = _measure(raw, sample_rate, chunk, language)
            metrics.update(attempt=attempt, seed=seed)
            item['attempts'].append(metrics)
            if metrics['reasons']:
                continue
            rate = metrics['chars_per_second']
            score = abs(math.log(rate/TARGET_CPS)) if rate else 0
            if best is None or score < best[0]:
                best = score, audio, attempt
            if rate is None or SOFT_CPS[0] <= rate <= SOFT_CPS[1]:
                break
        if best is None:
            raise NarrationQualityError(report)
        _, audio, selected = best
        item['selected_attempt'] = selected
        rate = item['attempts'][selected]['chars_per_second']
        item['warnings'] = ['rate_outside_preferred_range'] if rate and not SOFT_CPS[0] <= rate <= SOFT_CPS[1] else []
        # A 5ms edge ramp prevents clicks when a model emits no quiet margin.
        # This changes neither duration nor the ordering of voiced samples.
        fade = min(round(sample_rate*.005), len(audio)//2)
        if fade:
            ramp = np.linspace(0, 1, fade, dtype=np.float32)
            audio[:fade] *= ramp
            audio[-fade:] *= ramp[::-1]
        # Never cross-fade voiced speech. Keep words separate with a punctuation
        # pause after subtracting the retained edge silence on both chunks.
        if outputs:
            previous = chunks[index-1].rstrip('”’」』）) ')
            desired = .28 if previous.endswith(('。','！','？','!','?')) else .15
            left, right = outputs[-1], audio
            def edge_seconds(samples):
                nonzero = np.flatnonzero(np.abs(samples) > .0001)
                return (int(nonzero[0]) if len(nonzero) else len(samples))/sample_rate
            gap = max(0, desired-edge_seconds(left[::-1])-edge_seconds(right))
            outputs.append(np.zeros(round(gap*sample_rate), dtype=np.float32))
        outputs.append(audio)
    return np.concatenate(outputs), report


def report_path(path):
    return Path(str(path) + '.quality.json')


def cached_synthesis_valid(path):
    """WAV is the commit marker; a corrupt committed pair is an explicit error."""
    path = Path(path)
    if not path.is_file():
        return False
    try:
        report = json.loads(report_path(path).read_text(encoding='utf-8'))
        valid = (report['schema_version'] == 1 and
                 report['audio_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f'Invalid TTS cache report: {path.name}') from error
    if not valid:
        raise ValueError(f'Invalid TTS cache audio: {path.name}')
    return True


def write_synthesis(path, audio, sample_rate, report):
    """Commit the report first and the complete PCM16 WAV last, atomically."""
    import soundfile as sf
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.tts-', suffix='.wav', dir=path.parent)
    os.close(fd)
    try:
        sf.write(temporary, audio, sample_rate, subtype='PCM_16')
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
            digest = hashlib.sha256(stream.read()).hexdigest()
        atomic_json(report_path(path), {**report, 'audio_sha256':digest})
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect_synthesis_reports(audio_map):
    """Embed available diagnostics in task results, including cache reuse."""
    units = []
    for index, path in sorted(audio_map.items(), key=lambda pair: int(pair[0])):
        if report_path(path).is_file():
            cached_synthesis_valid(path)
            units.append(dict(segment_index=int(index), **json.loads(report_path(path).read_text(encoding='utf-8'))))
    return dict(schema_version=1, units=units)
