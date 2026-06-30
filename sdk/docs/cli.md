# BiliMix CLI — AI Agent 集成文档

> **面向 AI Agent 的命令行接口**，封装 BiliMix Web API 全部能力。
> 本文档为 Agent 优化：结构化、可检索、含完整工作流示例。

---

## 1. 概述

BiliMix 是英语播客→中英混合音频的智能处理服务。CLI 工具 `bmx` 将其 REST API 包装为
结构化命令，**默认输出 JSON 到 stdout**，便于 AI Agent / 脚本 / `jq` 直接消费。

**核心能力**：

- 提交音频 URL 或本地文件 → 自动转录、识别生词、TTS 合成、生成中英混合音频
- 三种处理模式：生词替换 / 智能翻译 / 句子翻译
- 任务全生命周期管理：提交 / 轮询 / 确认 / 重试 / 删除
- 生词库管理、播客搜索、RSS 解析、配置读写、音频下载

**调用方式**：`./bmx <command> [subcommand] [options]`，或 `python3 cli.py ...`

---

## 2. 快速开始

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BILIMIX_SERVER` | `http://localhost:5000` | BiliMix 服务地址 |
| `BILIMIX_HOME` | `~/.bilimix` | session 等配置存储目录 |

### 依赖

仅需 `requests`（`pip install requests`）+ Python 3.8+ 标准库。

### 认证

若服务端启用认证（`AUTH_ENABLED=True`），首次使用前登录：

```bash
bmx auth login --username admin --password <password>
# session 自动持久化到 ~/.bilimix/session.json，后续命令自动携带
```

检查状态：`bmx auth status`

### 一行命令端到端

```bash
# 提交 URL → 阻塞等待 → 提取混合音频路径
bmx task submit --url https://example.com/ep.mp3 --mode smart_translate --wait \
  --field result.mixed_audio
```

---

## 3. 命令速查表

| 域 | 命令 | 用途 |
|---|---|---|
| **认证** | `auth login` / `logout` / `status` | 登录/登出/状态 |
| **任务** | `task submit` | 提交音频处理任务（支持 `--wait`） |
| | `task list` | 任务列表 |
| | `task status <id>` | 查询状态 |
| | `task result <id>` | 完整结果（转录/生词/翻译/音频路径） |
| | `task cancel <id>` | 终止任务 |
| | `task confirm <id>` | 确认生词后继续 |
| | `task confirm-sentences <id>` | 确认句子翻译后继续 |
| | `task retry <id>` | 断点续传重试 |
| | `task retry-synthesis <id>` | 重试 TTS 合成 |
| | `task delete <id>` | 删除任务及文件 |
| | `task wait <id>` | 等待任务到达终态 |
| **音频** | `audio upload <file>` | 上传本地音频 |
| | `audio download` | 下载音频（按 task-id 或 path） |
| | `audio url` | 获取音频下载 URL |
| **翻译** | `translate word` | 翻译单词/短语 |
| | `translate word-levels` | 查询 BNC/COCA 词频 |
| **播客** | `podcast search <query>` | 搜索播客 |
| | `podcast rss <url>` | 解析 RSS Feed |
| **生词库** | `vocab list` | 生词列表（筛选/分页） |
| | `vocab stats` | 统计 |
| | `vocab toggle-mastered <id>` | 切换掌握状态 |
| | `vocab delete <id>` | 删除生词 |
| **收藏** | `favorites list/add/remove/check` | 播客收藏 |
| **订阅** | `subscriptions list/add/remove` | RSS 订阅 |
| **历史** | `history suggestions/add/clear` | 搜索历史 |
| **最近** | `recent list` | 最近播客 |
| **配置** | `config get` / `config set` | 读写配置 |
| **元信息** | `api` | 服务端端点目录 |

---

## 4. 全局选项

以下选项可在**命令前或后**任意位置出现（`--server`/`--pretty`/`-q`/`--field`）：

| 选项 | 说明 |
|---|---|
| `--server URL` | 服务地址，覆盖 `BILIMIX_SERVER` |
| `--pretty` | 美化 JSON 输出（人类可读） |
| `-q, --quiet` | 抑制 stderr 提示信息 |
| `--field PATH` | 提取 JSON 嵌套字段，如 `result.mixed_audio`、`tasks.0.task_id` |

**`--field` 用法**：按点号路径取值，支持数组索引。提取标量直接打印，提取对象/数组仍输出 JSON。

```bash
bmx task result <id> --field result.mixed_audio          # → /data/.../mixed.mp3
bmx task list --field tasks.0.task_id                     # → t1a2b3c
bmx vocab stats --field total                             # → 42
```

---

## 5. 输出与退出码

### 输出约定

- **stdout**：纯 JSON（默认紧凑，`--pretty` 美化）或 `--field` 提取的标量
- **stderr**：进度、警告、错误信息（不影响 stdout 解析）
- **二进制**：`audio download` 写文件，stdout 仅输出元信息 JSON

### 退出码

| 码 | 含义 | Agent 处理建议 |
|---|---|---|
| `0` | 成功 | 解析 stdout 继续 |
| `1` | API 错误（HTTP 4xx/5xx） | 读 stderr 错误信息，可能需修正参数重试 |
| `2` | 未认证（401） | 先 `auth login` 再重试 |
| `3` | 请求超时 | 检查服务可用性后重试 |
| `4` | 网络错误（无法连接） | 检查 `--server` / 服务是否运行 |
| `5` | 缺少依赖 | 提示 `pip install requests` |
| `130` | Ctrl+C 中断 | 用户主动取消 |

---

## 6. 命令详细参考

### 6.1 auth — 认证管理

#### `auth login`

```bash
bmx auth login --username <user> --password <pass>
```

**输出**：`{"ok": true, "message": "登录成功"}`

session cookie 自动持久化到 `$BILIMIX_HOME/session.json`，后续命令自动携带。

#### `auth logout`

清除本地 session 并通知服务端。

#### `auth status`

```bash
bmx auth status
# {"authenticated": true, "auth_enabled": true, "username": "admin"}
```

---

### 6.2 task — 任务管理（核心）

#### `task submit`

提交音频处理任务。

```bash
bmx task submit --url <audio_url> [选项]
bmx task submit --local-path <path> [选项]    # 需先 audio upload
```

| 选项 | 说明 |
|---|---|
| `--url` / `--local-path` | 音频来源（二选一，必填） |
| `--mode` | `word_replace` / `smart_translate` / `sentence_translate` |
| `--difficulty` | `CET-4` / `CET-6` / `IELTS-6` / `IELTS-7` / `ADVANCED` |
| `--skip-confirm` | 跳过确认环节（默认行为） |
| `--no-skip-confirm` | 要求人工确认（任务会暂停在 awaiting_* 状态） |
| `--title` | 任务标题 |
| `--wait` | 提交后阻塞轮询直到终态 |
| `--poll-interval` | 轮询间隔秒数（默认 2.0） |

**输出**：`{"task_id": "<id>", "message": "任务已提交"}`

加 `--wait` 时，进度打 stderr，终态后输出完整 result JSON。

#### `task wait`

等待任务到达终态或指定状态。

```bash
bmx task wait <task_id> [--until <status>] [--poll-interval 2.0]
```

`--until` 可指定任意状态（如 `awaiting_confirmation`），到达后立即返回当前 result。

**终态状态**：`completed` / `error` / `cancelled` / `awaiting_confirmation` /
`awaiting_sentence_confirmation`

#### `task list`

```bash
bmx task list
# {"tasks": [{"task_id": "...", "status": "completed", ...}, ...]}
```

#### `task status <id>`

轻量状态查询（不含转录文本等大字段）。

```bash
bmx task status <id>
# {"task_id": "...", "status": "processing", "step": "transcribe",
#  "progress": 25, "message": "...", "process_mode": "smart_translate"}
```

#### `task result <id>`

完整结果，含转录文本、segments、生词、翻译、替换、音频路径。

```bash
bmx task result <id>
```

**关键字段**：

```jsonc
{
  "task_id": "...",
  "status": "completed",
  "process_mode": "smart_translate",
  "transcription_text": "...",        // 完整转录
  "segments": [{"text": "...", "start": 0.0, "end": 2.5}],
  "difficult_words": [{"english": "...", "chinese": "...", "type": "word"}],
  "translations": {"3": "中文翻译", "7": "..."},  // 句子翻译模式
  "translated_indices": [3, 7],
  "sentence_pairs": [{"index": 3, "english": "...", "chinese": "...",
                       "start": 10.0, "end": 15.0}],
  "result": {
    "basename": "...",
    "original_audio": "/data/.../orig.mp3",
    "mixed_audio": "/data/.../mixed.mp3",
    "original_duration": 180.5,
    "mixed_duration": 210.3
  }
}
```

#### `task cancel <id>`

```bash
bmx task cancel <id>
# {"message": "任务终止请求已发送"}
```

#### `task confirm <id>`

`word_replace` 模式下确认生词后继续。支持 JSON 字符串或文件。

```bash
bmx task confirm <id> --words '[{"english":"villain","chinese":"反派","type":"word"}]'
bmx task confirm <id> --words-file words.json --wait
```

`--words-file` 读 JSON 数组文件，格式同 `task result` 返回的 `difficult_words`。

#### `task confirm-sentences <id>`

`sentence_translate` / `smart_translate` 模式下确认翻译后继续。

```bash
bmx task confirm-sentences <id> \
  --translations '{"3":"这是中文翻译","7":"另一句"}' \
  --indices '[3,7]' --wait

# 或从文件
bmx task confirm-sentences <id> --translations-file trans.json --wait
```

`--translations` 是 `{句子索引字符串: 中文翻译}` 映射。`--indices` 可选，缺省时取 translations 的 key。

#### `task retry <id>`

断点续传：自动检测已完成步骤并跳过。

```bash
bmx task retry <id> --wait
```

#### `task retry-synthesis <id>`

仅重试失败的 TTS 合成部分（保留已成功的）。

#### `task delete <id>`

删除任务及其所有关联文件（下载、结果、转录缓存）。

```bash
bmx task delete <id>
# {"message": "任务已删除", "cleaned_files": ["data/results/xxx/", ...]}
```

---

### 6.3 audio — 音频文件

#### `audio upload <file>`

上传本地音频到服务端。

```bash
bmx audio upload /path/to/ep.mp3
# {"ok": true, "local_path": "/data/downloads/ep.mp3",
#  "filename": "ep.mp3", "size_mb": 45.2}
```

返回的 `local_path` 可直接传给 `task submit --local-path`。

#### `audio download`

下载音频文件。

```bash
# 通过任务 ID（自动解析路径）
bmx audio download --task-id <id> --type mixed -o out.mp3
bmx audio download --task-id <id> --type original -o orig.mp3

# 直接指定路径
bmx audio download --path <basename>/<basename>_mixed.mp3 -o out.mp3
```

`--type`：`mixed`（中英混合，默认） / `original`（原始音频）。

下载进度打 stderr，stdout 输出 `{"ok": true, "path": "out.mp3", "size_bytes": N}`。

#### `audio url`

获取音频的可访问 URL（不下载）。

```bash
bmx audio url --task-id <id> --type mixed
# http://localhost:5000/api/audio/xxx/xxx_mixed.mp3
```

---

### 6.4 translate — 翻译工具

#### `translate word`

```bash
bmx translate word "villain"
bmx translate word "give up" --context "Don't give up on your dreams."
# {"english": "give up", "chinese": "放弃"}
```

#### `translate word-levels`

批量查询 BNC/COCA 词频等级。

```bash
bmx translate word-levels --words "villain,serendipity,ephemeral"
bmx translate word-levels --words-file words.json   # JSON 数组
# {"levels": {"villain": "5k", "serendipity": "10k"},
#  "level_nums": {"3k": 3000, "5k": 5000, ...}}
```

---

### 6.5 podcast — 播客搜索

#### `podcast search <query>`

通过 iTunes 搜索播客。

```bash
bmx podcast search "lex fridman"
```

#### `podcast rss <url>`

解析 RSS Feed 获取单集列表。

```bash
bmx podcast rss "https://lexfridman.com/feed/podcast/"
```

---

### 6.6 vocab — 生词库

#### `vocab list`

```bash
bmx vocab list --filter-mastered unmastered --sort-by encounter_count \
  --page 1 --page-size 20 --search "abandon"
```

| 选项 | 值 |
|---|---|
| `--sort-by` | `last_seen_at` / `encounter_count` / `frequency_level` / `english` |
| `--sort-order` | `asc` / `desc` |
| `--filter-mastered` | `all` / `mastered` / `unmastered` |
| `--filter-type` | `word` / `phrase` / `idiom` / `collocation` |
| `--filter-freq` | 词频等级 |
| `--search` | 搜索单词或释义 |
| `--page` / `--page-size` | 分页 |

#### `vocab stats`

```bash
bmx vocab stats
# {"total": 156, "unmastered": 89, "mastered": 67, "recent_7days": 12}
```

#### `vocab toggle-mastered <id>` / `vocab delete <id>`

---

### 6.7 favorites / subscriptions / history / recent / config

#### `favorites`

```bash
bmx favorites list
bmx favorites add --rss-url <url> --title "..." --author "..." --image "..."
bmx favorites remove --rss-url <url>
bmx favorites check --rss-url <url>   # {"is_favorite": false}
```

#### `subscriptions`

```bash
bmx subscriptions list
bmx subscriptions add --rss-url <url> --title "..."
bmx subscriptions remove --rss-url <url>
```

#### `history`

```bash
bmx history suggestions <prefix>     # {"suggestions": ["lex fridman", ...]}
bmx history add <keyword>
bmx history clear
```

#### `recent list`

最近使用的播客源列表。

#### `config`

```bash
bmx config get                          # 全部配置（美化输出）
bmx config get --key DIFFICULTY_LEVEL    # {"DIFFICULTY_LEVEL": "CET-4"}
bmx config set DIFFICULTY_LEVEL=CET-6 SKIP_CONFIRMATION=false LLM_BATCH_SIZE=10
bmx config set --file config.json       # 批量从 JSON 文件
```

`set` 的 VALUE 会自动尝试解析为 JSON 类型（数字/布尔/null），解析失败则视为字符串。

#### `api`

返回服务端全部端点元信息。

```bash
bmx api --field total_endpoints    # → 20
```

---

## 7. Agent 集成工作流

### 7.1 端到端处理（最常见）

```bash
# 提交 → 等待完成 → 拿到混合音频路径
MIXED=$(bmx task submit --url "$URL" --mode smart_translate --wait \
  --field result.mixed_audio)

# 下载
bmx audio download --path "$(echo $MIXED | sed 's|.*/api/audio|/api/audio|')" \
  -o output.mp3
# 或更简单：
bmx audio download --task-id <id> --type mixed -o output.mp3
```

### 7.2 需人工确认的流程

当 `skip_confirmation=False` 时，任务会暂停在 `awaiting_confirmation` 状态：

```bash
# 1. 提交（要求确认）
TID=$(bmx task submit --url "$URL" --no-skip-confirm | jq -r .task_id)

# 2. 等待到暂停状态
bmx task wait "$TID" --until awaiting_confirmation

# 3. 获取 AI 识别的生词，Agent 可编辑后回写
bmx task result "$TID" --field difficult_words > words.json
# (Agent 在此编辑 words.json：删除误标、补充新生词)

# 4. 提交确认并等待完成
bmx task confirm "$TID" --words-file words.json --wait

# 5. 下载结果
bmx audio download --task-id "$TID" --type mixed -o final.mp3
```

### 7.3 本地文件处理

```bash
# 上传 → 用返回的 local_path 提交
LP=$(bmx audio upload /local/path/ep.mp3 | jq -r .local_path)
bmx task submit --local-path "$LP" --mode smart_translate --wait
```

### 7.4 搜索播客 → 选集 → 处理

```bash
# 搜索
bmx podcast search "lex fridman" | jq -r '.results[0].feedUrl'

# 解析 RSS 获取单集
bmx podcast rss "https://lexfridman.com/feed/podcast/" \
  | jq -r '.episodes[0].enclosure.url'

# 提交单集
bmx task submit --url <episode_url> --wait
```

### 7.5 错误恢复

```bash
# 任务失败 → 断点续传（自动跳过已完成步骤）
bmx task retry <failed_task_id> --wait

# 仅 TTS 合成失败 → 只重试合成
bmx task retry-synthesis <task_id> --wait
```

### 7.6 批量生词查询

```bash
# 从转录文本提取生词并查询词频
bmx task result <id> --field difficult_words \
  | jq -r '.[].english' \
  | jq -s '.' \
  > words.json

bmx translate word-levels --words-file words.json
```

---

## 8. Agent 最佳实践

### 8.1 字段提取优于全量解析

```bash
# ❌ 全量解析后再提取
bmx task result <id> | jq -r .result.mixed_audio

# ✅ 直接字段提取（更省 token，stdout 更干净）
bmx task result <id> --field result.mixed_audio
```

### 8.2 用 `--wait` 减少轮询代码

```bash
# ❌ 手动轮询
TID=$(bmx task submit --url X | jq -r .task_id)
while true; do
  S=$(bmx task status $TID | jq -r .status)
  [ "$S" = "completed" ] && break
  sleep 2
done

# ✅ 一行搞定
bmx task submit --url X --wait --field result.mixed_audio
```

### 8.3 文件传递复杂参数

确认类命令的 `difficult_words` / `translations` 可能很长，用 `--*-file`：

```bash
bmx task confirm <id> --words-file /tmp/words.json --wait
```

### 8.4 退出码驱动重试逻辑

```bash
bmx task list 2>/dev/null
case $? in
  0)   echo "OK" ;;
  2)   bmx auth login --user admin --pass xxx && bmx task list ;;
  4)   echo "服务未运行，请启动 web_app.py" ;;
  *)   echo "其他错误" ;;
esac
```

### 8.5 `--quiet` 配合脚本

脚本中加 `-q` 抑制 stderr 噪音，stdout 仍为纯 JSON：

```bash
RESULT=$(bmx -q task submit --url X --wait)
```

---

## 9. 故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| exit 4 / "无法连接" | 服务未运行或地址错误 | 启动 `python services/web_app.py`，检查 `--server` |
| exit 2 / "未登录" | session 过期或未登录 | `bmx auth login` |
| exit 1 + "任务状态为 X，不在等待确认状态" | 确认命令状态不匹配 | 先 `task status` 确认状态 |
| `--field` 返回 `null` | 路径错误或字段不存在 | 先不加 `--field` 看完整 JSON 结构 |
| 任务卡在 `awaiting_*` | 启用了确认环节 | 用 `task confirm` 或 `task confirm-sentences` 继续 |
| 任务 error | 处理失败 | `task retry <id>` 断点续传 |

---

## 10. 与 Web API 的对应关系

CLI 是 Web API 的薄封装，每个命令对应一个 HTTP 调用：

| CLI | HTTP |
|---|---|
| `task submit --url X` | `POST /api/submit` `{"url": "X"}` |
| `task status <id>` | `GET /api/task/<id>` |
| `task result <id>` | `GET /api/task/<id>/result` |
| `task cancel <id>` | `POST /api/task/<id>/cancel` |
| `task confirm <id> --words W` | `POST /api/task/<id>/confirm` `{"difficult_words": W}` |
| `vocab list --filter-mastered X` | `GET /api/vocabulary?filter_mastered=X` |
| ... | ... |

完整端点列表：`bmx api`

如需直接 HTTP 调用（绕过 CLI），参考 `bmx api` 输出的端点目录。

---

## 11. 参考实现

- CLI 源码：`cli.py`
- Web API 源码：`services/web_app.py`（`/api` 端点含完整自描述）
- 配置项：`core/config.py`
- 入口脚本：`bmx`（shell 包装，`chmod +x` 后可直接 `./bmx`）
