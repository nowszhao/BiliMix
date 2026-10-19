"""
外部字幕文件解析模块

支持格式:
- ASS 字幕（Dialogue 行，|| 分隔英文和中文）
- 未来可扩展 SRT、VTT
"""
import os
import re

# ASS Dialogue 行格式: Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
# 共 10 个逗号分隔字段，Text 是最后一个
_DIALOGUE_RE = re.compile(
    r'^Dialogue:\s*[^,]*,\s*([^,]+),\s*([^,]+),'
    r'(?:[^,]*,\s*){5}'
    r'(.*)$'
)


def _ass_time_to_seconds(ts: str) -> float:
    """将 ASS 时间戳 (H:MM:SS.cc) 转换为秒"""
    parts = ts.strip().split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    try:
        return float(ts)
    except ValueError:
        return 0.0


def parse_ass_subtitle(ass_path: str) -> tuple:
    """解析 ASS 双语字幕文件（|| 分隔英文和中文）。

    Args:
        ass_path: ASS 文件路径

    Returns:
        tuple: (segments, translations, translated_indices)
            segments: list of {text, start, end, speaker}
            translations: dict of {segment_index: chinese_text}
            translated_indices: list of segment indices with Chinese text
    """
    if not os.path.isfile(ass_path):
        return [], {}, []

    segments = []
    translations = {}
    translated_indices = []

    event_format = None
    in_events = False
    with open(ass_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("["):
                in_events = stripped.lower() == "[events]"
            if in_events and stripped.lower().startswith("format:"):
                event_format = [x.strip().lower() for x in stripped.split(":", 1)[1].split(",")]
                if not event_format or event_format[-1] != "text":
                    raise ValueError("ASS Events Format must end in Text")
                continue
            if not line.strip().startswith("Dialogue:"):
                continue
            fields = line.strip()[len("Dialogue:"):].lstrip().split(",", 9)
            if len(fields) != 10:
                raise ValueError("Invalid ASS Dialogue field count")
            if event_format:
                values = dict(zip(event_format, fields))
                start_ts, end_ts, name, text = values["start"], values["end"], values.get("name", "").strip(), values["text"]
            else:
                start_ts, end_ts, name, text = fields[1], fields[2], fields[4].strip(), fields[9]
            start = _ass_time_to_seconds(start_ts)
            end = _ass_time_to_seconds(end_ts)

            # 去除 ASS override 标签 {\xxx} 和开头多余标点
            text = re.sub(r'\{[^}]*\}', '', text).strip().lstrip(', ')
            if not text:
                continue

            # 按 || 分割英文和中文
            if '||' in text:
                parts = text.split('||', 1)
                eng = parts[0].strip()
                chn = parts[1].strip() if len(parts) > 1 else ''
            else:
                eng = text
                chn = ''

            idx = len(segments)
            segments.append({
                "text": eng,
                "start": start,
                "end": end,
                "speaker": name,
            })

            if chn:
                translations[idx] = chn
                translated_indices.append(idx)

    return segments, translations, translated_indices
