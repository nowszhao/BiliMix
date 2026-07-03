# BiliMix CLI SDK

> 面向 AI Agent 的 BiliMix 命令行接口与 Python 客户端库
>
> 将 BiliMix Web App 的全部 REST API 包装为结构化、可脚本化的工具。

---

## 介绍

BiliMix CLI 是 BiliMix 服务的命令行客户端，提供：

- **命令行工具 `bmx`**：默认输出 JSON，支持字段提取、阻塞等��
- **Python 客户端库**：可作为 SDK 在 Python 代码中直接调用
- **AI Agent 友好**：稳定退出码、结构化输出、自描述端点

能力覆盖：音频处理任务全生命周期、播客搜索、翻译、配置管理等。

---

## 安装

```bash
cd sdk/
pip install -e .          # 开发模式
# 或
pip install .              # 安装到 site-packages

# 使用
bmx --help
bmx task submit --url https://example.com/ep.mp3 --wait
```

### 作为 Python 库使用

```python
from bilimix_cli.client import BiliMixClient

client = BiliMixClient("http://localhost:5000")
client.post_json("/api/login", {"username": "admin", "password": "admin123"})

# 提交任务
result = client.post_json("/api/submit", {
    "url": "https://example.com/ep.mp3",
    "title": "My Podcast",
    "skip_confirmation": True,
})
task_id = result["task_id"]

# 轮询状态
import time
while True:
    status = client.get(f"/api/task/{task_id}")
    if status["status"] in ("completed", "error", "cancelled"):
        break
    time.sleep(2)

final = client.get(f"/api/task/{task_id}/result")
print(final["result"]["mixed_audio"])
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BILIMIX_SERVER` | `http://localhost:5000` | 服务地址 |
| `BILIMIX_HOME` | `~/.bilimix` | session 存储目录 |

## 认证

```bash
bmx auth login --username admin --password admin123
# session 自动持��化
```

## 端到端一行命令

```bash
bmx task submit --url https://example.com/ep.mp3 --wait --field result.mixed_audio
```

处理模式固定为 sentence_translate（100% 全文翻译）。

---

## 命令速查

| 命令 | 用途 |
|------|------|
| `bmx auth login/logout/status` | 认证管理 |
| `bmx task submit [--url/--local-path] [--wait]` | 提交任务 |
| `bmx task list` / `status <id>` / `result <id>` | 查询任务 |
| `bmx task cancel <id>` | 终止任务 |
| `bmx task retry <id>` | 断点续传 |
| `bmx task wait <id> [--until STATUS]` | 等待终态 |
| `bmx podcast search <q>` / `rss --url <url>` | 播客搜索 |
| `bmx translate --text <text>` | 句子翻译 |
| `bmx config show/set/reset` | 配置管理 |
| `bmx audio upload <file>` | 上传音频 |
| `bmx api` | 端点目录 |

**全局选项**（可在命令前后任意位置）：
- `--server URL` 服务地址
- `--pretty` 美化 JSON
- `--field PATH` 提取嵌套字段

## 退出码

| 码 | 含义 | 处理建议 |
|----|------|----------|
| 0 | 成功 | 解析 stdout |
| 1 | API 错误 | 修正参数 |
| 2 | 未认证 | 先 `auth login` |
| 3 | 超时 | 检查服务后重试 |
| 4 | 网络错误 | 检查 `--server` |

---

## 目录结构

```
sdk/
├── bilimix_cli/
│   ├── __init__.py
│   ├── __main__.py       # python -m bilimix_cli
│   ├── cli.py            # CLI 主逻辑
│   └── client.py         # Python 库接口
├── docs/
│   └── cli.md            # 详细文档
├── examples/
│   └── library_usage.py  # 示例
├── pyproject.toml        # 打包配置
├── requirements.txt
└── README.md
```

## 部署

```bash
cd sdk/
python -m build              # 打包
pip install dist/bilimix_cli-*.whl  # 安装到其他机器
```

CLI 只需 `requests` 库，不需要 whisperx / torch / pydub 等重量级依赖。

## 文档

详见 [docs/cli.md](docs/cli.md) — 全部命令详细参考。

## License

MIT
