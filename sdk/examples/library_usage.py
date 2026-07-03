#!/usr/bin/env python3
"""
Python SDK usage example — BiliMix task submission and polling.
"""
import time, sys
from bilimix_cli.client import BiliMixClient, CLIError

def main():
    audio_url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://example.com/podcast.mp3"

    client = BiliMixClient("http://localhost:5000")

    # 1. Login
    try:
        client.post_json("/api/login", {"username": "admin", "password": "admin123"})
        print("[ok] Logged in")
    except CLIError:
        print("[warn] Auth not required or already logged in")

    # 2. Submit task (sentence_translate mode, 100% translation)
    result = client.post_json("/api/submit", {
        "url": audio_url,
        "title": "My Podcast",
        "skip_confirmation": True,
    })
    task_id = result["task_id"]
    print(f"[ok] Task submitted: {task_id}")

    # 3. Poll status
    print("[..] Processing", end="", flush=True)
    while True:
        status = client.get(f"/api/task/{task_id}")
        st = status["status"]
        prog = status.get("progress", 0)
        msg = status.get("message", "")
        print(f"\r[..] {prog:3d}% {st} — {msg}" + " " * 20, end="", flush=True)
        if st in ("completed", "error", "cancelled"):
            break
        time.sleep(2)
    print()

    # 4. Get results
    final = client.get(f"/api/task/{task_id}/result")
    if final["status"] != "completed":
        print(f"[err] Task not completed: {final['status']}")
        sys.exit(1)

    r = final["result"]
    print(f"[ok] Done!")
    print(f"     Original: {r['original_duration']}s")
    print(f"     Mixed: {r['mixed_duration']}s")
    print(f"     Audio: {r['mixed_audio']}")
    print(f"     Segments: {r['total_segments']}, Translated: {r['translated_segments']}")

    # 5. Download audio
    output_file = "output_mixed.mp3"
    print(f"[..] Downloading -> {output_file}")
    resp = client.get_raw(r['mixed_audio'].replace('/api/audio/', ''), stream=True)
    with open(output_file, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk: f.write(chunk)
    print(f"[ok] Saved: {output_file}")

if __name__ == "__main__":
    main()
