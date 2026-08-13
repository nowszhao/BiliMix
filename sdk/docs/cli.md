# BiliMix CLI 使用指南

`bmx` 将 BiliMix Web App 的全部 REST API 包装为结构化、可脚本化的命令行接口，
默认输出 JSON 到 stdout，便于 AI Agent / 管道 / `jq` 直接消费。

## 安装

```bash
cd sdk/
pip install -e .
bmx --help
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BILIMIX_SERVER` | `http://localhost:5000` | 服务地址 |
| `BILIMIX_HOME` | `~/.bilimix` | session / 配置存储目录 |

服务地址优先级：`--server` 命令行 > `BILIMIX_SERVER` 环境变量 > 配置文件 > 默认值。
首次通过 `--server` 指定后地址会自动保存，后续命令无需再传。

## 全局选项

所有子命令（任意位置）都支持：

| 选项 | 说明 |
|------|------|
| `--server URL` | 服务地址 |
| `--pretty` | 美化 JSON 输出 |
| `--field PATH` | 提取嵌套字段，如 `result.mixed_audio`、`video_result.video_url` |
| `-q, --quiet` | 抑制进度等提示信息 |

## 认证

```bash
bmx auth login --username admin --password <password>
bmx auth status          # 查询登录状态
bmx auth logout          # 退出登录并清除本地 session
```

## 任务管理

### 提交任务

```bash
# 音频配音（URL）
bmx task submit --url "https://example.com/ep.mp3" --title "My Podcast" --wait

# 本地音频/视频文件（需先上传）
bmx audio upload ./ep.mp3
bmx task submit --local-path <upload 返回的 local_path> --keep-bgm

# 视频配音（YouTube URL）
bmx task submit --type video --video-url "https://www.youtube.com/watch?v=xxx" \
    --subtitle-mode bilingual --wait

# 视频配音（本地文件）
bmx audio upload ./test.mp4
bmx task submit --type video --local-path <local_path> --subtitle-mode bilingual
```

参数：

| 参数 | 说明 |
|------|------|
| `--type audio\|video` | 任务类型（默认 audio） |
| `--url` | 音频文件 URL |
| `--local-path` | 上传后的本地路径（`audio upload` 返回的 `local_path`） |
| `--video-url` | YouTube 视频 URL |
| `--server-path` | 服务端视频绝对路径（`--type video`） |
| `--title` | 任务标题 |
| `--keep-bgm` | 保留原音频/视频背景音乐 |
| `--duration` | 预知时长（如 `01:23:45` 或 `142`） |
| `--subtitle-mode bilingual\|chinese_only\|none` | 字幕模式（video only，默认 bilingual） |
| `--subtitle-font-size` | 字幕字号（video only，默认 20） |
| `--subtitle-path` | 服务端外部双语字幕文件路径（见下文「外部字幕」） |
| `--ref-select-mode speaker_global\|speaker_local\|segment` | 参考音频选取模式（声音克隆） |
| `--skip-confirm / --no-skip-confirm` | 跳过/要求人工确认翻译（默认跳过） |
| `--wait` | 提交后阻塞等待到终态 |
| `--poll-interval` | 轮询间隔秒数（默认 2.0） |

### 外部字幕（新建视频文件含字幕）

当已有现成的双语字幕（ASS 格式，`||` 分隔英文和中文）时，可跳过转录和翻译，
直接用字幕 + 原视频生成配音视频：

```bash
# 1. 上传字幕文件
bmx audio upload ./subtitle.ass
# 返回 { "local_path": "/abs/path/to/subtitle.ass", ... }

# 2. 可选：先解析/校验字幕
bmx subtitle parse "<上一步的 local_path>"

# 3. 提交视频任务，指定外部字幕
bmx task submit --type video --video-url "https://www.youtube.com/watch?v=xxx" \
    --subtitle-path "<local_path>" --wait
```

`--subtitle-path` 同样适用于音频任务（跳过转录/翻译，直接用字幕生成配音音频）。

### 查看 / 操作任务

```bash
bmx task list [--limit N]        # 任务列表
bmx task status <id>             # 任务状态与进度
bmx task result <id>             # 任务完整结果
bmx task cancel <id>             # 终止任务
bmx task delete <id>             # 删除任务及文件
bmx task redo <id> [--wait]      # 完整重做（清空产物，从头执行）
bmx task retry <id> [--wait]     # 断点续传
bmx task retry-synthesis <id>    # 仅重试 TTS 合成
bmx task reorder <id> --direction up|down   # 调整排队顺序
bmx task wait <id> [--until STATUS]         # 阻塞等待
```

### 确认翻译结果

```bash
bmx task confirm-sentences <id> --translations '{"0": "你好", "2": "世界"}'
bmx task confirm-sentences <id> --translations-file trans.json --wait
```

## 音频 / 视频 / 字幕 / 文件

```bash
# 上传（音频/视频/字幕）
bmx audio upload <file>

# 下载配音/原始音频
bmx audio download --task-id <id> --type mixed -o out.mp3
bmx audio download --path basename/basename_mixed.mp3 -o out.mp3
bmx audio url --task-id <id> [--type mixed|original]

# 下载配音视频 / 字幕
bmx video download --task-id <id> -o dubbed.mp4
bmx video download-srt --task-id <id> -o subtitle.ass

# 解析/校验服务端双语字幕
bmx subtitle parse <local_path>

# 通用附件下载（支持自定义文件名）
bmx file download <path> -o out.mp4 [--name custom.mp4]
```

## 翻译 & 工具

```bash
bmx translate word "vocabulary" [--context "a sentence"]
bmx translate word-levels --words "hello,world"
bmx translate word-levels --words-file words.json
```

## 播客 & 订阅

```bash
bmx podcast search "Python programming"
bmx podcast rss "https://example.com/feed.xml"

bmx subscriptions list / add / remove / refresh
bmx favorites list / add / remove / check

bmx episodes list [--status all|unread|read|transcribed|dismissed] \
    [--time-range today|week|month|all] [--rss-url URL] [--page N] [--page-size N]
bmx episodes stats [--time-range] [--rss-url]
bmx episodes update <id> --status read
bmx episodes mark-read [--rss-url] [--time-range]
bmx episodes refresh              # 刷新所有订阅源
bmx episodes refresh-feed <rss_url>

bmx recent list
```

## 搜索历史

```bash
bmx history suggestions <q>
bmx history add <keyword>
bmx history clear
```

## 配置 & API 元信息

```bash
bmx config get [--key KEY]
bmx config set KEY=VALUE KEY2=VALUE2 ...
bmx config set --file config.json
bmx api
```

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | API 错误（HTTP 4xx/5xx） |
| 2 | 未认证（401） |
| 3 | 请求超时 |
| 4 | 网络错误（无法连接） |
| 5 | 缺少依赖 |

## 一行完成

```bash
# 音频配音：提交 + 等待 + 提取混合音频 URL
bmx task submit --url https://example.com/ep.mp3 --wait --field result.mixed_audio

# 视频配音：本地文件 + 双语字幕 + 等待完成
bmx task submit --type video --local-path ./test.mp4 --subtitle-mode bilingual --wait

# 下载配音视频 + 字幕
bmx video download --task-id <id> -o dubbed.mp4
bmx video download-srt --task-id <id> -o subtitle.ass

# 用外部字幕直接生成配音视频
bmx audio upload ./subtitle.ass
bmx task submit --type video --video-url "https://youtu.be/xxx" \
    --subtitle-path "<local_path>" --wait
```
