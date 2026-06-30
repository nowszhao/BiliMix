"""
BiliMix CLI — 面向 AI Agent 的命令行接口 SDK

将 BiliMix Web App 的全部 REST API 包装为结构化、可脚本化的 CLI。
默认输出 JSON 到 stdout，便于 AI Agent / 管道 / jq 直接消费。

作为 Python 包使用:
    from bilimix_cli.client import BiliMixClient
    client = BiliMixClient("http://localhost:5000")
    result = client.get("/api/tasks")

作为命令行使用:
    bmx --help
    python -m bilimix_cli --help
"""

from .client import BiliMixClient, CLIError
from .cli import main

__version__ = "1.0.0"
__all__ = ["BiliMixClient", "CLIError", "main"]
