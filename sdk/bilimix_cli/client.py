"""
BiliMix HTTP 客户端 — 可作为 Python 库直接 import 使用

示例:
    from bilimix_cli.client import BiliMixClient

    client = BiliMixClient("http://localhost:5000")
    client.post_json("/api/login", {"username": "admin", "password": "xxx"})

    # 提交任务并轮询
    result = client.post_json("/api/submit", {"url": "https://x.com/ep.mp3"})
    task_id = result["task_id"]

    import time
    while True:
        status = client.get(f"/api/task/{task_id}")
        if status["status"] in ("completed", "error", "cancelled"):
            break
        time.sleep(2)

    # 获取完整结果
    final = client.get(f"/api/task/{task_id}/result")
    print(final["result"]["mixed_audio"])

    # 流式下载音频
    resp = client.get_raw(f"/api/audio/{final['result']['basename']}/"
                         f"{final['result']['basename']}_mixed.mp3",
                         stream=True)
    with open("out.mp3", "wb") as f:
        for chunk in resp.iter_content(64 * 1024):
            f.write(chunk)
"""

# 从 cli 模块 re-export，保持单文件实现，避免代码重复
from .cli import BiliMixClient, CLIError, EXIT_OK, EXIT_API_ERROR, EXIT_AUTH, \
    EXIT_TIMEOUT, EXIT_NETWORK, EXIT_DEP, DEFAULT_SERVER, SESSION_DIR

__all__ = [
    "BiliMixClient",
    "CLIError",
    "DEFAULT_SERVER",
    "SESSION_DIR",
    "EXIT_OK",
    "EXIT_API_ERROR",
    "EXIT_AUTH",
    "EXIT_TIMEOUT",
    "EXIT_NETWORK",
    "EXIT_DEP",
]
