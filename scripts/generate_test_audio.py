#!/usr/bin/env python3
"""
生成配音质量测试音频 — 2分钟，多说话人/语速/停顿混合。
使用 macOS 内置 `say` 命令（vanilla OS, 无需联网）。
"""
import subprocess, os, tempfile, json, sys, re
from pydub import AudioSegment

# ── 输出路径 ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "test_data")
os.makedirs(OUTPUT_DIR, exist_ok=True)
WAV_PATH = os.path.join(OUTPUT_DIR, "pace_test_audio.wav")
META_PATH = os.path.join(OUTPUT_DIR, "pace_test_metadata.json")

# macOS `say` 可用声线
VOICES = {
    "samantha": "Samantha",    # en-US 女声（自然）
    "alex":     "Alex",         # en-US 男声
    "daniel":   "Daniel",       # en-GB 男声
    "karen":    "Karen",        # en-AU 女声
    "fiona":    "Fiona",        # en-GB 女声（英式）
    "fred":     "Fred",         # en-US 男声（较自然）
}


def say_tts(voice: str, text: str, out_path: str, rate: int = 0) -> bool:
    """用 macOS say 生成语音"""
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
    except subprocess.TimeoutExpired:
        print(" say timeout")
        return False
    except Exception as e:
        print(f" say error: {type(e).__name__}: {e}")
        return False


# ── 素材定义 ──
# (voice_key, rate_wpm, text, pause_after_ms)
# rate: 标准语速约 180 wpm。此处为偏移值：-40=慢, 0=正常, +40=快
segments = [
    # ── 开场：Samantha (美式女声) 正常 ──
    ("samantha", 0,    "Welcome to the BiliMix audio synthesis test suite.",                 300),
    ("samantha", 0,    "This recording contains multiple speakers, varied speaking rates, and irregular pauses.", 600),
    ("samantha", 20,   "Our goal is to stress test the vocal separation, transcription, translation, and mixing pipeline.", 400),

    # ── Alex (美式男声) 快速 ──
    ("alex",    50,    "Hey there! This is fast speech. Can you keep up? Let's see how the system handles quick bursts of energy!", 500),
    ("alex",    50,    "Boom. Short. Fast.",                                                   200),
    ("alex",    60,    "Rapid fire. One after another. No time to breathe!",                    800),

    # ── Daniel (英式男声) 慢速 ──
    ("daniel", -30,    "Now we have a rather lengthy sentence that meanders through several subordinate clauses, with the express purpose of challenging the system's ability to handle extended speech patterns gracefully.", 1200),

    # ── Karen (澳式女声) 正常 ──
    ("karen",   0,     "A brief pause.",                                                       250),
    ("karen",  10,     "This sentence follows a very short gap, testing tight timing alignment.", 600),

    # ── Fred (美式男声) 中速 ──
    ("fred",    0,     "Testing medium pace speech with natural cadence in this passage.",       500),
    ("fred",   25,     "Slightly faster now! Just enough to feel the difference.",              1000),

    # ── Samantha 再次，稍慢 ──
    ("samantha", -20,  "Now returning to the original voice, but speaking a bit slower this time around.", 400),

    # ── Fiona (英式女声) ──
    ("fiona",   0,     "And now for something completely different — a British female voice.",   700),
    ("fiona",   0,     "Short.",                                                                200),
    ("fiona",   0,     "Medium length sentence here.",                                          500),
    ("fiona",   0,     "This is a longer passage aimed at testing how well the system can maintain consistent quality across an extended utterance.", 300),

    # ── Alex 再次快速 + 极短停顿 ──
    ("alex",    70,    "Quick!",                                                                150),
    ("alex",    70,    "Fast!",                                                                150),
    ("alex",    70,    "Tight!",                                                                200),

    # ── 超长停顿测试（5秒） ──
    ("daniel", -20,    "Long pause incoming. Get ready.",                                       5000),

    # ── Karen 恢复 ──
    ("karen",   0,     "Did you notice that five second gap? That's for testing silence handling.", 800),
    ("karen",   0,     "Short one.",                                                            150),
    ("karen",   0,     "Another.",                                                              150),
    ("karen",   0,     "And another.",                                                          400),

    # ── Samantha 超慢 ──
    ("samantha", -50,  "Testing the lower bound of the speaking rate — very slow and deliberate articulation.", 2000),

    # ── 交替短句 + 2秒停顿 ──
    ("fiona",   0,     "Two second silence upcoming.",                                          2000),
    ("fred",    0,     "And we are back. That was another extended pause.",                      600),
    ("fred",    0,     "Here.",                                                                 200),
    ("fred",    0,     "There.",                                                                500),
    ("fred",    0,     "Everywhere.",                                                           400),

    # ── Daniel 中速 ──
    ("daniel",  0,     "This concludes the main section of our test recording.",                 400),

    # ── 结尾加速 ──
    ("alex",    80,    "Finishing strong! Fast and furious! Last few sentences coming right up!", 300),
    ("alex",    80,    "Three.",                                                                150),
    ("alex",    80,    "Two.",                                                                  150),
    ("alex",    80,    "One.",                                                                  200),
    ("samantha", 0,    "Test complete. Audio synthesis check finished.",                         200),
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

        print(f"  [{i:02d}] {voice_key:7s} rate={rate_pct:3d} pause={pause_ms:4d}ms "
              f"\"{text[:40]:40s}\"", end="")

        ok = say_tts(voice, text, out_path, rate=rate_pct)
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
            "description": "BiliMix 配音质量测试音频 — 多说话人/语速/停顿混合",
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
    print(f"  长停顿(>2s): "
          f"{sum(1 for s in meta_segments if s['pause_after_ms'] > 2000)} 处")
    print(f"  短停顿(<300ms): "
          f"{sum(1 for s in meta_segments if s['pause_after_ms'] < 300)} 处")
