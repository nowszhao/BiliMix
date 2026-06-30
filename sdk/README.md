# BiliMix CLI SDK

> 面向 AI Agent 的 BiliMix 命令行接口与 Python 客户端库
>
> 将 BiliMix Web App 的全部 REST API 包装为结构化、可脚本化的工具。

---

## 介绍

BiliMix CLI 是 BiliMix 服务的命令行客户端，提供：

- **命令行工具 `bmx`**：默认输出 JSON，支持字段提取、阻塞等待、文件传参
- **Python 客户端库**：可作为 SDK 在 Python 代码中直接调用
- **AI Agent 友好**：稳定退出码、结构化输出、自描述端点

能力覆盖：音频处理任务全生命周期、生词库、播客搜索、翻译、配置管理等。

---

## 安装

### 方式 1：pip 安装（推荐）

```bash
cd sdk/
pip install -e .          # 开发模式（修改源码立即生效）
# 或
pip install .              # 安装到 site-packages
```

安装后获得 `bmx` 命令：

```bash
bmx --help
bmx task submit --url https://x.com/ep.mp3 --wait
```

### 方式 2：直接运行（不安装）

```bash
cd sdk/
python -m bilimix_cli --help
python -m bilimix_cli task list --server http://localhost:5000
```

### 方式 3：作为 Python 库使用

```python
from bilimix_cli.client import BiliMixClient

client = BiliMixClient("http://localhost:5000")

# 登录（如服务端启用认证）
client.post_json("/api/login", {"username": "admin", "password": "xxx"})

# 提交任务
result = client.post_json("/api/submit", {
    "url": "https://example.com/ep.mp3",
    "mode": "smart_translate",
})
task_id = result["task_id"]

# 轮询状态
import time
while True:
    status = client.get(f"/api/task/{task_id}")
    if status["status"] in ("completed", "error", "cancelled"):
        break
    time.sleep(2)

# 获取结果
final = client.get(f"/api/task/{task_id}/result")
print(final["result"]["mixed_audio"])
```

---

## 快速开始

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BILIMIX_SERVER` | `http://localhost:5000` | BiliMix 服务地址 |
| `BILIMIX_HOME` | `~/.bilimix` | session 等配置存储目录 |

### 认证

```bash
bmx auth login --username admin --password <password>
# session 自动持久化，后续命令自动携带
```

### 端到端一行命令

```bash
# 提交 URL → 阻塞等待 → 提取混合音频路径
bmx task submit --url https://example.com/ep.mp3 --mode smart_translate --wait \
  --field result.mixed_audio
```

---

## 命令速查

| 命令 | 用途 |
|---|---|
| `bmx auth login/logout/status` | 认证管理 |
| `bmx task submit [--url/--local-path] [--wait]` | 提交任务 |
| `bmx task list` / `status <id>` / `result <id>` | 查询任务 |
| `bmx task cancel <id>` / `delete <id>` | 终止/删除 |
| `bmx task confirm <id> --words-file F` | 确认生词 |
| `bmx task confirm-sentences <id> --translations-file F` | 确认翻译 |
| `bmx task retry <id>` / `retry-synthesis <id>` | 断点续传 |
| `bmx task wait <id> [--until STATUS]` | 等待终态 |
| `bmx audio upload <file>` / `download` / `url` | 音频文件 |
| `bmx translate word` / `word-levels` | 翻译/词频 |
| `bmx podcast search` / `rss` | 播客搜索 |
| `bmx vocab list/stats/toggle-mastered/delete` | 生词库 |
| `bmx favorites` / `subscriptions` / `history` / `recent` | 收藏/订阅/历史 |
| `bmx config get/set` | 配置管理 |
| `bmx api` | 端点目录 |

**全局选项**（可在命令前后任意位置）：

- `--server URL` 服务地址
- `--pretty` 美化 JSON
- `-q, --quiet` 抑制提示
- `--field PATH` 提取嵌套字段（如 `result.mixed_audio`）

---

## 退出码

| 码 | 含义 | 处理建议 |
|---|---|---|
| 0 | 成功 | 解析 stdout |
| 1 | API 错误 | 读 stderr，修正参数 |
| 2 | 未认证 | 先 `auth login` |
| 3 | 超时 | 检查服务后重试 |
| 4 | 网络错误 | 检查 `--server` |
| 5 | 缺依赖 | `pip install requests` |

---

## 部署到其他环境

### 作为独立 SDK 分发

```bash
# 1. 打包
cd sdk/
python -m build              # 生成 dist/bilimix_cli-1.0.0-py3-none-any.whl

# 2. 分发到目标机器
scp dist/bilimix_cli-*.whl user@target:~/

# 3. 目标机器安装
pip install bilimix_cli-1.0.0-py3-none-any.whl

# 4. 使用（只需 BiliMix 服务可访问即可）
bmx --server http://bilimix-server:5000 task list
```

### 在 Docker 中使用

```dockerfile
FROM python:3.11-slim
COPY sdk/ /app/sdk/
RUN pip install /app/sdk/
ENTRYPOINT ["bmx"]
```

```bash
docker build -t bilimix-cli -f Dockerfile.cli sdk/
docker run --rm bilimix-cli --server http://host.docker.internal:5000 task list
```

### 在 CI/CD 中使用

```yaml
# .github/workflows/test.yml
- name: Install BiliMix CLI
  run: pip install ./sdk

- name: 提交处理任务
  env:
    BILIMIX_SERVER: ${{ secrets.BILIMIX_SERVER }}
  run: |
    bmx auth login --username ${{ secrets.BILIMIX_USER }} \
                   --password ${{ secrets.BILIMIX_PASS }}
    bmx task submit --url $AUDIO_URL --wait --field result.mixed_audio
```

---

## 目录结构

```
sdk/
├── bilimix_cli/              # Python 包
│   ├── __init__.py           # 包入口，导出 BiliMixClient / main
│   ├── __main__.py           # 支持 python -m bilimix_cli
│   ├── cli.py                # CLI 主逻辑（含 BiliMixClient 实现）
│   └── client.py             # Python 库接口（re-export BiliMixClient）
├── docs/
│   └── cli.md                # 完整集成文档（面向 AI Agent）
├── examples/                 # 示例脚本
│   ├── end_to_end.sh         # 端到端处理
│   ├── confirm_flow.sh       # 需确认的流程
│   └── library_usage.py      # Python 库用法
├── pyproject.toml            # 打包配置（含 bmx 入口点）
├── requirements.txt
└── README.md                 # 本文件
```

---

## 与 BiliMix 服务的关系

CLI 是 BiliMix Web App 的**纯客户端**，不依赖服务端代码：

- 通过 HTTP 调用 BiliMix 的 REST API
- 只需 `requests` 库，**不需要** whisperx / torch / pydub 等重量级依赖
- 可部署在任意能访问 BiliMix 服务的机器上

服务端启动：在 BiliMix 主项目运行 `python services/web_app.py`（默认 `localhost:5000`）。

---

## 完整文档

详见 [docs/cli.md](docs/cli.md) — 包含全部命令详细参考、Agent 集成工作流、最佳实践、故障排查。

---

## License

MIT
