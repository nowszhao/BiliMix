"""Shared audio-first boundary and caption/visual planning primitives."""
import re


def additional_pause_ms(current, following, tail_ms=0, head_ms=0):
    boundary = current.get("boundary_after", "sentence_end")
    if boundary == "continuation":
        desired = 0
    elif current.get("speaker") != following.get("speaker"):
        desired = 220
    else:
        desired = 150
    return max(0, desired - tail_ms - head_ms)


def pad_last_video_chain(parts, duration):
    if duration > 0:
        parts[-1] = re.sub(r"(\[v\d+\])$",
                          lambda m: f",tpad=stop_mode=clone:stop_duration={duration:.6f}" + m[1], parts[-1])


def video_chains(entries, fps=25, include_trailing_gap=False):
    """Allocate frames from cumulative audio endpoints, never per-clip rounding.

    Blocks use the same global endpoints, so their frame counts telescope to the
    full timeline. Speech is stretched; a new pause without source footage holds
    the preceding frame. Audio is muxed once after all video blocks concatenate.
    """
    count = len(entries)-1 if include_trailing_gap else len(entries)
    parts = []

    def append(start, source_duration, target_duration, out_start, out_end, hold=0):
        frames = round(out_end*fps)-round(out_start*fps)
        if frames <= 0:
            return
        if source_duration <= 0 or target_duration <= 0:
            raise ValueError("Invalid source/output video span")
        # A positive source span shorter than one frame may contain no frame.
        # Include the nearest following frame, then bound the output by count.
        source_duration = max(source_duration, 1 / fps + .000001)
        chain = (f"[0:v]trim=start={start:.6f}:duration={source_duration:.6f},"
                 f"setpts=PTS-STARTPTS,setpts={target_duration/source_duration:.9f}*PTS")
        if hold > 0:
            chain += f",tpad=stop_mode=clone:stop_duration={hold:.6f}"
        chain += (# eof_action=pass retains the sole frame of a compressed short gap.
                  # Padding by the requested count covers even a long new pause.
                  f",fps={fps:.9f}:eof_action=pass,tpad=stop_mode=clone:stop={frames},"
                  f"trim=end_frame={frames},setpts=PTS-STARTPTS,format=yuv420p[v{len(parts)}]")
        parts.append(chain)

    for i in range(count):
        e = entries[i]
        following = entries[i+1] if i+1 < len(entries) else None
        gap_out = following['mixed_start']-e['mixed_end'] if following else 0
        gap_src = following['orig_start']-e['orig_end'] if following else 0
        hold = max(0, gap_out) if gap_src <= 0 else 0
        append(e['orig_start'],e['orig_end']-e['orig_start'],
               e['mixed_end']-e['mixed_start'],e['mixed_start'],e['mixed_end']+hold,hold)
        if gap_out > 0 and gap_src > 0:
            append(e['orig_end'],gap_src,gap_out,e['mixed_end'],following['mixed_start'])
    return parts


def caption_entries(segment, start, end, english, chinese):
    captions = segment.get("captions") or []
    if not captions:
        return [{"start": start, "end": end, "english": english, "chinese": chinese, "speaker": segment.get("speaker", "")}]
    # Display-only estimate. It never splits/resynthesizes speech or adds gaps.
    total = sum(max(1, len(c["text_zh"])) for c in captions)
    cursor, result = start, []
    for i, caption in enumerate(captions):
        stop = end if i == len(captions)-1 else cursor + (end-start)*max(1, len(caption["text_zh"]))/total
        result.append({"start": cursor, "end": stop, "english": caption.get("text_en", ""),
                       "chinese": caption["text_zh"], "speaker": segment.get("speaker", "")})
        cursor = stop
    return result
