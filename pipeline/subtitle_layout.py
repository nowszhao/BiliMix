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
# 避头尾：这些字符不能出现在行首，断行时必须留在上一行
_NO_LINE_START = _BREAKS | set('）】》」』”’%)]}>')


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


def _avoid_leading_punctuation(items, cut, floor):
    """Move the cut back so a closing punctuation mark never starts a line."""
    while cut > floor and items[cut] and not items[cut].isspace() and items[cut][0] in _NO_LINE_START:
        cut -= 1
    return cut


def _rebalance(spans, items, size, width, fill=.12):
    """Pull trailing tokens down while the last line is conspicuously short.

    Greedy wrapping fills every line to the brim, so a 32-glyph Chinese
    sentence becomes 30+2 and renders 「样。」 alone on the second line.
    Shifting only the few tokens needed lifts the tail off a lone glyph while
    keeping the first line nearly full — balancing both lines to half width
    would waste the margins the viewer expects to see used.

    Operating on token spans (not spliced strings) keeps the whitespace tokens
    in place: rebuilding lines by concatenation would silently drop the space
    and turn "them offline" into "themoffline".
    """
    min_fill = fill*width
    for _ in range(len(spans)*40):
        if len(spans) < 2:
            break
        head_start, head_end = spans[-2]
        tail_start, tail_end = spans[-1]
        if text_width(''.join(items[tail_start:tail_end]).strip(), size) >= min_fill:
            break
        cut = head_end-1
        while cut > head_start and items[cut].isspace():
            cut -= 1
        cut = _avoid_leading_punctuation(items, cut, head_start+1)
        if cut <= head_start:
            break
        head = ''.join(items[head_start:cut]).rstrip()
        tail = ''.join(items[cut:tail_end]).strip()
        if not head or text_width(tail, size) > width or text_width(head, size) < min_fill:
            break
        spans[-2], spans[-1] = (head_start, cut), (cut, tail_end)
    return spans


def wrap_lines(text, size, width, fill=.12):
    items = tokens(plain_text(text), size, width)
    spans, start, used = [], 0, 0.0
    for index, token in enumerate(items):
        advance = text_width(token, size)
        if used and used+advance > width and not token.isspace():
            cut = _avoid_leading_punctuation(items, index, start+1)
            if cut <= start:
                cut = index
            spans.append((start, cut))
            start = cut
            used = sum(text_width(t, size) for t in items[start:index+1])
            continue
        used += advance
    spans.append((start, len(items)))
    spans = _rebalance(spans, items, size, width, fill)
    return [''.join(items[a:b]).strip() for a, b in spans if ''.join(items[a:b]).strip()]


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
    max_height_ratio: float = .27
    bilingual: bool = True
    outline: float = 1.5
    # 1 = outline + shadow on transparent background (broadcast standard)
    # 3 = opaque box behind each line
    border_style: int = 1
    shadow: int = 1
    shadow_color: str = '&H80000000&'
    # 中英间隔相对字号的比例；0 表示只用单个换行（电影字幕常见的紧凑排版）
    gap_ratio: float = .3

    def __post_init__(self):
        if min(self.width, self.height, self.requested_font_size) <= 0:
            raise ValueError('Positive video dimensions and subtitle size required')
        if not 0 < self.max_height_ratio <= .5 or not 0 < self.english_ratio <= 1:
            raise ValueError('Invalid subtitle height/font ratio')
        if self.max_lines < (2 if self.bilingual else 1) or self.max_language_lines < 1:
            raise ValueError('Subtitle budget cannot display the requested languages')
        if self.border_style not in (1, 3):
            raise ValueError('BorderStyle must be 1 (outline) or 3 (opaque box)')
        if self.shadow < 0 or self.outline < 0:
            raise ValueError('Outline and shadow must not be negative')

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

    @property
    def gap_px(self):
        """Preferred spacer height between the Chinese and English blocks."""
        if self.gap_ratio <= 0:
            return 0.0
        return 1.2*max(1, int(round(self.font_size*self.gap_ratio)))

    def effective_gap(self, chinese_lines, english_lines):
        """Spacer height that still fits this page's height budget.

        A single \\N puts the English line ~3px under a short Chinese tail and
        the two blocks read as one lump. A spacer only helps when there is room
        for it: on a 320x180 canvas one Chinese line plus one English line
        already consumes the budget, and a fixed spacer would make paginate()
        unsatisfiable.
        """
        if not (chinese_lines and english_lines):
            return 0.0
        base = (1.2*(chinese_lines*self.font_size+english_lines*self.english_size)
                + 2*self.outline)
        return max(0.0, min(self.gap_px, self.height*self.max_height_ratio-base))

    def fitted_sizes(self, chinese, english):
        """Shrink slightly so a sentence that just misses one line still fits.

        32 glyphs at 38px need 1216px against 1200px available. Hard-wrapping
        that yields 17+15 with both lines half-full and wide margins on both
        sides; dropping to 37px keeps it on one line. Only gaps under 7% are
        absorbed — anything wider genuinely needs two lines.
        """
        need = text_width(chinese, self.font_size)
        if not chinese or need <= self.text_width:
            return self.font_size, self.english_size
        scale = self.text_width/need
        if scale < .93:
            return self.font_size, self.english_size
        chinese_size = max(1, int(self.font_size*scale))
        return chinese_size, max(1, int(round(chinese_size*self.english_ratio)))

    def measurements(self, chinese, english):
        chinese_size, english_size = self.fitted_sizes(chinese, english)
        cn = wrap_lines(chinese, chinese_size, self.text_width)
        en = wrap_lines(english, english_size, self.text_width)
        base = 1.2*(len(cn)*chinese_size+len(en)*english_size)+2*self.outline
        return cn, en, base+self.effective_gap(len(cn), len(en)), chinese_size, english_size

    def fits(self, chinese, english):
        cn, en, height, _, _ = self.measurements(chinese, english)
        return (len(cn) <= self.max_language_lines and len(en) <= self.max_language_lines
                and len(cn)+len(en) <= self.max_lines
                and height <= self.height*self.max_height_ratio)

    def paginate(self, entry):
        chinese = plain_text(entry.get('chinese', ''))
        english = plain_text(entry.get('english', ''))
        if not chinese and not english:
            return []
        cn, en, height, _, _ = self.measurements(chinese, english)
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
            cn, en, height, cn_size, en_size = self.measurements(c,e)
            result.append({**entry, 'start':cursor, 'end':stop, 'chinese':c, 'english':e,
                           'chinese_lines':cn, 'english_lines':en,
                           'line_count':len(cn)+len(en), 'estimated_height':height,
                           'chinese_size':cn_size, 'english_size':en_size})
            cursor = stop
        return result


def ass_document(entries, layout, margin_v=21, font_family='Noto Sans CJK SC', shadow=None,
                 chinese_color='&H00FFFFFF&', english_color='&H00E6E6E6&'):
    """Render a complete ASS document and auditable display-only layout report."""
    def timestamp(value):
        cs = max(0, round(float(value)*100))
        seconds,cs = divmod(cs,100);minutes,seconds = divmod(seconds,60);hours,minutes = divmod(minutes,60)
        return f'{hours}:{minutes:02}:{seconds:02}.{cs:02}'
    family = font_family.replace(',', ' ').replace('\n',' ')
    margin = min(layout.margin_lr,layout.width//12)
    shadow = layout.shadow if shadow is None else shadow
    styles = []
    # The English line is drawn smaller, so it takes a proportionally thinner
    # edge; a full-width outline turns 30px glyphs into black blobs on light
    # backgrounds.
    english_outline = max(1, int(layout.outline*layout.english_ratio))
    for name,size,color,edge in [('Chinese', layout.font_size, chinese_color, layout.outline),
                                 ('English', layout.english_size, english_color, english_outline)]:
        styles.append(
            f'Style: {name},{family},{size},{color},&H000000FF&,&H00000000&,{layout.shadow_color},'
            f'0,0,0,0,100,100,0,0,{layout.border_style},{edge},{shadow},2,'
            f'{margin},{margin},{margin_v},1')
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
        # A transparent period carries the spacer line's height. Blank lines
        # (\N\N) and whitespace-only lines are dropped by libass, and \h makes
        # libass lose the CJK font entirely and draw every glyph as a tofu box.
        gap_px = layout.effective_gap(len(page['chinese_lines']), len(page['english_lines']))
        gap = ''
        if gap_px >= 6:
            gap = '\\N{\\fs%d\\alpha&HFF&}.\\N' % max(1, int(round(gap_px/1.2)))
        # 为单句缩过字号时，必须在该行覆盖 \fs，否则又会被拉回全局字号。
        chinese_size = page.get('chinese_size', layout.font_size)
        english_size = page.get('english_size', layout.english_size)
        cn_tag = ('{\\rChinese\\q2}' if chinese_size == layout.font_size
                  else '{\\rChinese\\q2\\fs%d}' % chinese_size)
        en_tag = ('{\\rEnglish\\q2}' if english_size == layout.english_size
                  else '{\\rEnglish\\q2\\fs%d}' % english_size)
        text = (cn_tag+cn if cn else '')+(gap if cn and en else '')+(en_tag+en if en else '')
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
