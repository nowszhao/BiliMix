# BiliMix CLI 使用指南

## 安装

```bash
cd sdk/
pip install -e .
```

## 任务操作

### 提交任务

```bash
bmx task submit --url "https://example.com/podcast.mp3" --title "My Podcast" --wait
```

参数:
- `--url`: 音频文件 URL
- `--local-path`: 本地文件路径（需先 audio upload）
- `--type audio|video`: 任务类型（默认 audio）
- `--video-url`: YouTube 视频 URL
- `--title`: 任务标题
- `--keep-bgm`: 保留原音频/视频背景音乐
- `--duration`: 预知时长
- `--subtitle-mode bilingual|chinese_only|none`: 字幕模式（video only）
- `--subtitle-font-size`: 字幕字号（video only）
- `--skip-confirm / --no-skip-confirm`: 跳过/要求人工确认
- `--wait`: 等待任务完成

处理模式固定为 `sentence_translate`（100% 全文翻译）。

### 查看任务列表

```bash
bmx task list
bmx task list --limit 10
```

### 查看任务状态

```bash
bmx task status <task_id>
```

### 获取任务结果

```bash
bmx task result <task_id>
bmx task result <task_id> --download-audio
```

### 取消任务

```bash
bmx task cancel <task_id>
```

### 重试失败任务

```bash
bmx task retry <task_id>
```

### 完整重做任务

```bash
# 清除所有产物，从头执行 pipeline
bmx task redo <task_id>
# 重做并等待完成
bmx task redo <task_id> --wait
```

### 确认翻译结果

```bash
bmx task confirm <task_id> --skip
```

## 播客操作

### 搜索播客

```bash
bmx podcast search "Python programming"
bmx podcast search "BabyBus" --limit 20
```

### 解析 RSS Feed

```bash
bmx podcast rss --url "https://example.com/feed.xml"
```

## 配置管理

### 查看配置

```bash
bmx config show
```

### 更新配置

```bash
bmx config set confucius_tts_num_workers 2
bmx config set confucius_tts_n_timesteps 25
```

### 恢复默认配置

```bash
bmx config reset
```

## 翻译

### 句子翻译

```bash
bmx translate --text "Hello, how are you?"
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| BILIMIX_URL | http://localhost:5000 | 服务地址 |

## 输出格式

使用 `--pretty` 参数格式化 JSON 输出：

```bash
bmx task list --pretty
bmx config show --pretty
```
