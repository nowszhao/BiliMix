#!/usr/bin/env python3
"""
生成配音质量测试音频 — 多人对话风格，语速/停顿层次不齐。
使用 macOS 内置 `say` 命令（vanilla OS, 无需联网）。
"""
import subprocess, os, json, sys
from pydub import AudioSegment

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "test_data")
os.makedirs(OUTPUT_DIR, exist_ok=True)
WAV_PATH = os.path.join(OUTPUT_DIR, "pace_test_audio.wav")
META_PATH = os.path.join(OUTPUT_DIR, "pace_test_metadata.json")

VOICES = {
    "samantha": "Samantha",   # en-US 女声（主持人）
    "alex":     "Alex",        # en-US 男声（副主持）
    "daniel":   "Daniel",      # en-GB 男声（嘉宾）
}


def say_tts(voice: str, text: str, out_path: str, rate: int = 0) -> bool:
    aiff_path = out_path.replace(".wav", ".aiff")
    cmd = ["say", "-v", voice, "-o", aiff_path]
    if rate:
        cmd.append(f"--rate={rate}")
    try:
        r = subprocess.run(cmd, input=text, text=True,
                           capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(aiff_path):
            print(f" say failed (rc={r.returncode}): {r.stderr[:200]}")
            return False
        audio = AudioSegment.from_file(aiff_path, format="aiff")
        audio.export(out_path, format="wav")
        os.remove(aiff_path)
        return True
    except Exception as e:
        print(f" say error: {type(e).__name__}: {e}")
        return False


# ── 多人对话素材 ──
# (voice_key, rate, text, pause_after_ms)
# rate -20~+15 为自然范围
segments = [
    # 主持人开场
    ("samantha", 0,  "Welcome back to the show everyone, today we have a special guest with us.", 500),
    ("samantha", 0,  "We are going to talk about a really interesting topic that has been getting a lot of attention lately.", 700),

    # Alex 接话
    ("alex", 10, "That is right, and I am really excited to dive into this. It is something I have been following for a while now.", 600),
    ("alex", 15, "So tell us, what is your take on the whole situation?", 1000),

    # Daniel 思考后回应（慢速）
    ("daniel", -10, "Well, that is a great question. I think there are a few key aspects we need to consider here.", 800),
    ("daniel", -15, "First of all, the technology itself has evolved significantly over the past few years, and that has changed the landscape quite a bit.", 500),
    ("daniel", -10, "But I also think we need to be careful about how we approach some of the challenges that come with it.", 1200),

    # Samantha 追问
    ("samantha", 0, "That is a really good point.", 300),
    ("samantha", 5, "Could you elaborate a bit more on what you mean by challenges?", 800),

    # Alex 接话（稍快）
    ("alex", 20, "Yeah, I was wondering the same thing. Like, what are the main hurdles you see?", 600),

    # Daniel 详细说明
    ("daniel", -10, "Sure. So one of the biggest challenges is making sure the system handles edge cases properly.", 700),
    ("daniel", -5, "For example, when you have multiple speakers with very different speaking styles, the timing can be tricky to manage.", 500),
    ("daniel", -5, "And then there is the issue of pauses. Natural conversation has all sorts of pauses.", 400),
    ("daniel", 0, "Some are short, some are long, and they all serve a purpose in communication.", 2500),

    # 长停顿后恢复
    ("samantha", 0, "That is fascinating. I never really thought about pauses that way before.", 500),
    ("samantha", 5, "So how do you think this impacts the overall listening experience for the audience?", 700),

    # Alex 快速回应
    ("alex", 15, "Honestly, I think most people do not notice it consciously, but they feel it.", 400),
    ("alex", 20, "If the pacing is off, the whole thing just feels off.", 800),

    # Daniel 总结
    ("daniel", -5, "Exactly. And that is why getting the timing right is so crucial for a natural sounding result.", 600),
    ("daniel", 0, "It is the difference between something that sounds like a robot reading a script, and real people having a real conversation.", 800),

    # 快速收尾
    ("alex", 15, "Alright, well that is all we have time for today.", 300),
    ("samantha", 0, "Thank you so much for joining us, this was a great discussion.", 400),
    ("samantha", 0, "And thank you for listening. We will see you next time.", 300),
    ("alex", 10, "Take care everyone.", 200),
]


# ── 生成 ──
if __name__ == "__main__":
    print(f"生成测试音频: {len(segments)} 段, WAV={WAV_PATH}")
    print()

    SR = 24000
    combined = AudioSegment.silent(duration=0, frame_rate=SR)
    meta_segments = []
    cursor_ms = 0

    for i, (voice_key, rate_pct, text, pause_ms) in enumerate(segments):
        voice = VOICES[voice_key]
        out_path = f"/tmp/tts_seg_{i:03d}.wav"

        print(f"  [{i:02d}] {voice_key:8s} rate={rate_pct:3d} pause={pause_ms:4d}ms "
              f"\"{text[:40]:40s}\"", end="")

        ok = say_tts(voice, text.replace("'", ""), out_path, rate=rate_pct)
        if not ok:
            print(f" → FAILED")
            continue

        clip = AudioSegment.from_file(out_path)
        clip_dur = len(clip)
        os.remove(out_path)

        combined += clip
        seg_start = cursor_ms
        seg_end = cursor_ms + clip_dur
        meta_segments.append({
            "index": i,
            "voice": voice_key,
            "rate_pct": rate_pct,
            "text": text,
            "start": round(seg_start / 1000, 3),
            "end": round(seg_end / 1000, 3),
            "duration_ms": clip_dur,
            "pause_after_ms": pause_ms,
        })
        cursor_ms = seg_end

        if pause_ms > 0:
            combined += AudioSegment.silent(duration=pause_ms, frame_rate=SR)
            cursor_ms += pause_ms

        print(f" → {clip_dur}ms")

    total_dur_ms = len(combined)
    total_dur_s = total_dur_ms / 1000
    print(f"\n共计 {len(meta_segments)} 段, 总时长 {total_dur_s:.1f}s")

    combined = combined.set_frame_rate(SR).set_channels(1)
    combined.export(WAV_PATH, format="wav",
                    parameters=["-ar", str(SR), "-ac", "1"])
    print(f"WAV: {WAV_PATH} ({os.path.getsize(WAV_PATH)/1024:.0f} KB)")

    for s in meta_segments:
        s["total_duration"] = round(total_dur_s, 1)

    with open(META_PATH, "w") as f:
        json.dump({
            "description": "BiliMix 配音质量测试音频 — 3 人对话",
            "total_duration_s": round(total_dur_s, 1),
            "segment_count": len(meta_segments),
            "max_pause_ms": max(s["pause_after_ms"] for s in meta_segments),
            "min_pause_ms": min(s["pause_after_ms"] for s in meta_segments),
            "voices_used": sorted(set(s["voice"] for s in meta_segments)),
            "segments": meta_segments,
        }, f, indent=2, ensure_ascii=False)

    print(f"META: {META_PATH}")
    print(f"\n统计:")
    print(f"  总段数: {len(meta_segments)}")
    print(f"  声线: {sorted(set(s['voice'] for s in meta_segments))}")
    print(f"  速率范围: {min(s['rate_pct'] for s in meta_segments)}% ~ "
          f"{max(s['rate_pct'] for s in meta_segments)}%")
    print(f"  停顿范围: {min(s['pause_after_ms'] for s in meta_segments)}ms ~ "
          f"{max(s['pause_after_ms'] for s in meta_segments)}ms")
    print(f"  1~2秒停顿: "
          f"{sum(1 for s in meta_segments if 1000 <= s['pause_after_ms'] <= 2000)} 处")
    print(f"  >2.5秒停顿: "
          f"{sum(1 for s in meta_segments if s['pause_after_ms'] > 2500)} 处")
