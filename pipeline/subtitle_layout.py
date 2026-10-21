"""Bound display captions independently of speech synthesis and audio timing.

All wrapping is explicit (ASS q2). Pagination preserves text and never creates
TTS jobs. A conservative glyph estimate keeps the planner dependency-free.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import unicodedata


_TOKEN = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*|\s+|.", re.DOTALL)
_BREAKS = set('，。！？；：、,.!?;:')


def plain_text(text):
    return re.sub(r'\s+', ' ', str(text).replace('\\N', ' ').replace('\\n', ' ').replace('\\h', ' ')).strip()


def glyph_width(char, size):
    if unicodedata.combining(char) or char == '\u200d':
        return 0.0
    if char.isspace():
        return size * .35
    if ord(char) > 127 or char in '{}\\':
        return float(size)
    return size * (.95 if char in 'MWmw@%&' else .65)


def text_width(text, size):
    return sum(glyph_width(c, size) for c in text)


def tokens(text, size, width):
    # Long words/URLs also need a bounded fallback; ordinary English stays whole.
    result = []
    for token in _TOKEN.findall(text):
        result.extend(list(token) if text_width(token, size) > width else [token])
    return result


def wrap_lines(text, size, width):
    lines, line = [], ''
    for token in tokens(plain_text(text), size, width):
        if token.isspace() and not line:
            continue
        if line and text_width(line+token, size) > width:
            lines.append(line.rstrip())
            line = token.lstrip()
        else:
            line += token
    if line.strip():
        lines.append(line.rstrip())
    return lines


def _balanced_chunks(text, count, size, width):
    items = tokens(plain_text(text), size, width)
    if not items:
        return [''] * count
    chunks, cursor = [], 0
    for page in range(count):
        remaining = count-page
        if cursor >= len(items):
            chunks.append('')
            continue
        if remaining == 1:
            chunks.append(''.join(items[cursor:]).strip())
            break
        target = sum(text_width(t, size) for t in items[cursor:])/remaining
        used, best, best_cost = 0.0, cursor+1, float('inf')
        for end in range(cursor+1, len(items)+1):
            used += text_width(items[end-1], size)
            cost = abs(used-target)
            if items[end-1] in _BREAKS and .7*target <= used <= 1.2*target:
                cost -= .15*target
            if cost < best_cost:
                best, best_cost = end, cost
            if used > 1.3*target:
                break
        chunks.append(''.join(items[cursor:best]).strip())
        cursor = best
    return chunks


@dataclass(frozen=True)
class SubtitleLayout:
    width: int
    height: int
    requested_font_size: int
    english_ratio: float = .75
    margin_lr: int = 30
    max_lines: int = 4
    max_language_lines: int = 2
    max_height_ratio: float = .25
    bilingual: bool = True
    outline: int = 2

    def __post_init__(self):
        if min(self.width, self.height, self.requested_font_size) <= 0:
            raise ValueError('Positive video dimensions and subtitle size required')
        if not 0 < self.max_height_ratio <= .5 or not 0 < self.english_ratio <= 1:
            raise ValueError('Invalid subtitle height/font ratio')
        if self.max_lines < (2 if self.bilingual else 1) or self.max_language_lines < 1:
            raise ValueError('Subtitle budget cannot display the requested languages')

    @property
    def font_size(self):
        # Treat a requested size as an upper bound on small/low-resolution video.
        languages = 1+self.english_ratio if self.bilingual else 1
        maximum = max(1, int((self.height*self.max_height_ratio-2*self.outline)/(1.2*languages)))
        return min(self.requested_font_size, maximum)

    @property
    def english_size(self):
        return max(1, int(self.font_size*self.english_ratio))

    @property
    def text_width(self):
        return max(1, self.width-2*min(self.margin_lr, self.width//12)-20)

    def measurements(self, chinese, english):
        cn = wrap_lines(chinese, self.font_size, self.text_width)
        en = wrap_lines(english, self.english_size, self.text_width)
        height = 1.2*(len(cn)*self.font_size+len(en)*self.english_size)+2*self.outline
        return cn, en, height

    def fits(self, chinese, english):
        cn, en, height = self.measurements(chinese, english)
        return (len(cn) <= self.max_language_lines and len(en) <= self.max_language_lines
                and len(cn)+len(en) <= self.max_lines
                and height <= self.height*self.max_height_ratio)

    def paginate(self, entry):
        chinese = plain_text(entry.get('chinese', ''))
        english = plain_text(entry.get('english', ''))
        if not chinese and not english:
            return []
        cn, en, height = self.measurements(chinese, english)
        count = max(1, math.ceil(len(cn)/self.max_language_lines),
                    math.ceil(len(en)/self.max_language_lines),
                    math.ceil((len(cn)+len(en))/self.max_lines),
                    math.ceil(height/(self.height*self.max_height_ratio)))
        limit = max(len(chinese), len(english), 1)
        while count <= limit:
            chinese_chunks = _balanced_chunks(chinese, count, self.font_size, self.text_width)
            english_chunks = _balanced_chunks(english, count, self.english_size, self.text_width)
            pairs = [(c,e) for c,e in zip(chinese_chunks, english_chunks) if c or e]
            if all(self.fits(c,e) for c,e in pairs):
                break
            count += 1
        else:
            raise ValueError('Subtitle area too small; reduce the font or margins')
        # Internal integrity check: pagination must not remove/repeat content.
        compact = lambda t: re.sub(r'\s+', '', t)
        if (compact(''.join(c for c,e in pairs)) != compact(chinese)
                or compact(''.join(e for c,e in pairs)) != compact(english)):
            raise ValueError('Subtitle pagination changed source text')
        weights = [max(len(c), len(e)/3, 1) for c,e in pairs]
        total, cursor, result = sum(weights), entry['start'], []
        for i, ((c,e), weight) in enumerate(zip(pairs, weights)):
            stop = entry['end'] if i == len(pairs)-1 else cursor+(entry['end']-entry['start'])*weight/total
            cn, en, height = self.measurements(c,e)
            result.append({**entry, 'start':cursor, 'end':stop, 'chinese':c, 'english':e,
                           'chinese_lines':cn, 'english_lines':en,
                           'line_count':len(cn)+len(en), 'estimated_height':height})
            cursor = stop
        return result


def ass_document(entries, layout, margin_v=21, font_family='Noto Sans CJK SC', shadow=0):
    """Render a complete ASS document and auditable display-only layout report."""
    def timestamp(value):
        cs = max(0, round(float(value)*100))
        seconds,cs = divmod(cs,100);minutes,seconds = divmod(seconds,60);hours,minutes = divmod(minutes,60)
        return f'{hours}:{minutes:02}:{seconds:02}.{cs:02}'
    family = font_family.replace(',', ' ').replace('\n',' ')
    margin = min(layout.margin_lr,layout.width//12)
    styles = []
    for name,size,color in [('Chinese',layout.font_size,'&H0000D7FF&'),('English',layout.english_size,'&H00E6E6E6&')]:
        styles.append(f'Style: {name},{family},{size},{color},&H000000FF&,&H00000000&,&H40000000&,0,0,0,0,100,100,0,0,3,{layout.outline},{shadow},2,{margin},{margin},{margin_v},1')
    lines = ['[Script Info]','ScriptType: v4.00+',f'PlayResX: {layout.width}',f'PlayResY: {layout.height}',
             'ScaledBorderAndShadow: yes','WrapStyle: 2','','[V4+ Styles]',
             'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
             *styles,'','[Events]',
             'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    pages = []
    for entry in entries:
        if entry['end'] <= entry['start']:
            raise ValueError('Caption duration must be positive')
        pages.extend(layout.paginate(entry))
    for page in pages:
        if round(page['end']*100) <= round(page['start']*100):
            raise ValueError('Caption interval is too short at ASS centisecond precision')
        # Source text cannot execute ASS control sequences or reset font size.
        literal = lambda t: t.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
        cn = "\\N".join(literal(t) for t in page['chinese_lines'])
        en = "\\N".join(literal(t) for t in page['english_lines'])
        text = ('{\\rChinese\\q2}'+cn if cn else '')+('\\N' if cn and en else '')+('{\\rEnglish\\q2}'+en if en else '')
        speaker = str(page.get('speaker','')).replace(',','_').replace('\n',' ').replace('\r',' ')
        lines.append(f'Dialogue: 0,{timestamp(page["start"])},{timestamp(page["end"])},Chinese,{speaker},0,0,0,,{text}')
    report = {'status':'PASS','source_captions':len(entries),'display_pages':len(pages),
        'video_width':layout.width,'video_height':layout.height,'font_size':layout.font_size,
        'english_font_size':layout.english_size,'max_lines':layout.max_lines,
        'max_language_lines':layout.max_language_lines,'max_height_ratio':layout.max_height_ratio,
        'max_observed_lines':max((p['line_count'] for p in pages),default=0),
        'max_estimated_height_ratio':max((p['estimated_height']/layout.height for p in pages),default=0),
        'pages':[{'start':p['start'],'end':p['end'],'speaker':p.get('speaker',''),
                  'chinese_lines':len(p['chinese_lines']),'english_lines':len(p['english_lines']),
                  'line_count':p['line_count'],'estimated_height':p['estimated_height']}for p in pages]}
    return '\n'.join(lines)+'\n',report
