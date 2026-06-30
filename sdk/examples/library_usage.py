#!/usr/bin/env python3
"""
Python 库用法示例：作为 SDK 在代码中调用 BiliMix

演示 BiliMixClient 的典型用法，无需命令行。
"""
import time
import sys

from bilimix_cli.client import BiliMixClient, CLIError


def main():
    audio_url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://example.com/podcast.mp3"

    # 1. 创建客户端（连接到 BiliMix 服务）
    client = BiliMixClient("http://localhost:5000")

    # 2. 登录（如服务端启用认证）
    try:
        client.post_json("/api/login",
                          {"username": "admin", "password": "bilimix2024"})
        print("[ok] 登录成功")
    except CLIError as e:
        if e.code == 2:
            print("[warn] 未启用认证或已登录，继续")
        else:
            raise

    # 3. 提交任务
    result = client.post_json("/api/submit", {
        "url": audio_url,
        "process_mode": "smart_translate",
        "skip_confirmation": True,
    })
    task_id = result["task_id"]
    print(f"[ok] 任务已提交: {task_id}")

    # 4. 轮询状态
    print("[..] 处理中", end="", flush=True)
    while True:
        status = client.get(f"/api/task/{task_id}")
        st = status["status"]
        prog = status.get("progress", 0)
        msg = status.get("message", "")
        print(f"\r[..] {prog:3d}% {st} — {msg}" + " " * 20, end="", flush=True)

        if st in ("completed", "error", "cancelled",
                  "awaiting_confirmation", "awaiting_sentence_confirmation"):
            break
        time.sleep(2)
    print()

    # 5. 获取完整结果
    final = client.get(f"/api/task/{task_id}/result")
    if final["status"] != "completed":
        print(f"[err] 任务未完成: {final['status']}")
        sys.exit(1)

    result_obj = final["result"]
    print(f"[ok] 处理完成!")
    print(f"     原始时长: {result_obj['original_duration']}s")
    print(f"     混合时长: {result_obj['mixed_duration']}s")
    print(f"     混合音频: {result_obj['mixed_audio']}")

    # 6. 查看识别的生词
    words = final.get("difficult_words", [])
    print(f"\n[ok] 识别到 {len(words)} 个生词:")
    for w in words[:10]:
        print(f"     {w['english']} → {w['chinese']} ({w.get('type', '')})")
    if len(words) > 10:
        print(f"     ... 还有 {len(words) - 10} 个")

    # 7. 下载混合音频
    basename = result_obj["basename"]
    audio_path = f"/api/audio/{basename}/{basename}_mixed.mp3"
    output_file = f"{basename}_mixed.mp3"

    print(f"\n[..] 下载音频 → {output_file}")
    resp = client.get_raw(audio_path, stream=True)
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    with open(output_file, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded * 100 / total)
                    print(f"\r     {pct:3d}% ({downloaded // 1024}KB)",
                          end="", flush=True)
    print(f"\n[ok] 已保存: {output_file}")


if __name__ == "__main__":
    main()
