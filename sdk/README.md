# BiliMix CLI SDK

面向 AI Agent 的 BiliMix 命令行接口与 Python 客户端库。
将 BiliMix Web App 的全部 REST API 包装为结构化、可脚本化的工具。

---

## 安装

```bash
cd sdk/
pip install -e .

bmx --help
```

### Python SDK

```python
from bilimix_cli.client import BiliMixClient

client = BiliMixClient("http://localhost:5050")
client.post_json("/api/login", {"username": "admin", "password": "bilimix2024"})

# 提交任务
result = client.post_json("/api/submit", {
    "url": "https://example.com/ep.mp3",
    "title": "My Podcast",
    "skip_confirmation": True,
})
task_id = result["task_id"]

# 轮询进度
import time
while True:
    task = client.get(f"/api/task/{task_id}")
    if task["status"] in ("completed", "error", "cancelled"):
        break
    time.sleep(2)
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BILIMIX_SERVER` | `http://localhost:5000` | 服务地址 |
| `BILIMIX_HOME` | `~/.bilimix` | session 存储目录 |

## 认证

```bash
bmx auth login --username admin --password bilimix2024
```

---

## 命令速查

### 订阅单集

| 命令 | 说明 |
|------|------|
| `bmx episodes list [--status] [--time-range today\|week\|month\|all]` | 单集列表 |
| `bmx episodes stats [--time-range]` | 统计 + 各订阅未读数 |
| `bmx episodes update <id> --status {unread\|read\|transcribed\|dismissed}` | 更新状态 |
| `bmx episodes mark-read [--rss-url] [--time-range]` | 批量已读 |
| `bmx episodes refresh` | 刷新所有订阅源 |
| `bmx episodes refresh-feed <rss_url>` | 刷新单个订阅源 |

### 任务管理

| 命令 | 说明 |
|------|------|
| `bmx task submit --url <URL> [--wait]` | 提交音频处理 |
| `bmx task submit --local-path <FILE> [--wait]` | 提交本地文件 |
| `bmx task list` | 任务列表 |
| `bmx task status <id>` | 查询状态 |
| `bmx task result <id>` | 获取结果 |
| `bmx task cancel <id>` | 终止任务 |
| `bmx task retry <id>` | 断点续传 |
| `bmx task delete <id>` | 删除任务及文件 |
| `bmx task wait <id> [--until STATUS]` | 阻塞等待 |

### 播客 & 内容

| 命令 | 说明 |
|------|------|
| `bmx podcast search <q>` | 搜索播客 |
| `bmx podcast rss <url>` | 解析 RSS Feed |
| `bmx subscriptions list / add / remove` | RSS 订阅管理 |
| `bmx favorites list / add / remove / check` | 播客收藏管理 |

### 翻译 & 工具

| 命令 | 说明 |
|------|------|
| `bmx translate word <english> [--context]` | 翻译单词/短语 |
| `bmx translate word-levels --words <...>` | 查询 BNC/COCA 词频等级 |
| `bmx audio upload <file>` | 上传音频 |
| `bmx audio download [--task-id\|--path]` | 下载音频 |
| `bmx config get / set` | 配置管理 |
| `bmx api` | API 端点目录 |

### 全局选项

- `--server URL` 服务地址
- `--pretty` 美化 JSON 输出
- `--field PATH` 提取嵌套字段，如 `result.mixed_audio`

---

## 一行完成

```bash
# 提交 + 等待完成 + 提取混合音频 URL
bmx task submit --url https://example.com/ep.mp3 --wait --field result.mixed_audio

# 查今天新增的单集
bmx episodes list --time-range today

# 查看所有未读
bmx episodes stats --time-range all
```

---

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | API 错误 (HTTP 4xx/5xx) |
| 2 | 未认证 (401) |
| 3 | 请求超时 |
| 4 | 网络错误 |

---

## 目录结构

```
sdk/
├── bilimix_cli/
│   ├── cli.py            # CLI 主逻辑
│   └── client.py         # Python SDK
├── README.md
├── requirements.txt
└── setup.py
```

---

## License

MIT
