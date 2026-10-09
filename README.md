# BiliMix — 中英混合播客/视频学习工具

[English](README.en.md)

将英文播客/音频/视频通过本地 LLM 逐句翻译为中文，用 Confucius4-TTS 零样本声音克隆朗读中文译文，组装为中英交替音频（或带字幕的配音视频）。

## 效果展示

<table>
<tr><th align="center">原始视频</th><th align="center">配音后（中英双语字幕）</th></tr>
<tr>
<td align="center" width="50%"><a href="examples/test1.mp4"><img src="examples/screenshot_original.jpg" alt="原始视频截图" width="100%"></a></td>
<td align="center" width="50%"><a href="examples/test1_mixed.mp4"><img src="examples/screenshot_mixed.jpg" alt="配音后截图" width="100%"></a></td>
</tr>
</table>

## 硬件要求

> 安装前请确认满足最低要求，否则运行会严重受限或失败。

| 资源 | 最低 | 推荐 | 说明 |
|------|:---:|:---:|------|
| CPU | 4 核 | 8 核+ | 转录 8 线程 + TTS 多 Worker + FFmpeg 编码 |
| 内存 | 8 GB | 16 GB+ | Ollama 3-4 GB + TTS 2-4 GB/Worker（默认 2 个） |
| 磁盘 | 15 GB | 50 GB+ | Python 环境 + 模型 ~10 GB + 音视频缓存 |
| GPU | 不需要 | 可选 6 GB+ | 显著加速 TTS 与 WhisperX |

内存是主要瓶颈。低配机器：用 `translategemma:4b`、TTS Worker 降为 1、WhisperX 用 `small`。

## 支持场景

| 场景 | 输入 | 输出 |
|------|------|------|
| 音频转录 | URL / 本地文件 | 中英交替 MP3 |
| 视频配音 | YouTube / 本地 MP4 / 服务器路径 | 中英配音 MP4 + SRT 字幕 |

## 核心流程

```
音频/视频 → WhisperX 转录 → Ollama 逐句翻译 → Confucius4-TTS 声音克隆 → 组装（音频/视频 + 字幕）
```

## 技术栈

WhisperX（词级时间戳 + 说话人分离）· Ollama（本地翻译）· Confucius4-TTS-CPU（零样本声音克隆）· FFmpeg · yt-dlp · Flask · SQLite

## 快速开始

### 一键安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/nowszhao/BiliMix/main/setup.sh | bash
```

自动完成：Miniconda + `bilimix` env（Python 3.10）→ 系统依赖（FFmpeg / Ollama）→ Python 依赖 → clone Confucius4-TTS-CPU 到 `../Confucius4-TTS-CPU` → 生成 `core/config_local.py`。脚本幂等，可重复运行。

已 clone 本仓库时直接运行 `./setup.sh` 亦可。

### 启动

```bash
ollama serve                      # 首次需要，或 brew services start ollama
ollama pull translategemma:4b

conda activate bilimix
python services/web_app.py 5555   # 浏览器访问 http://localhost:5555
```

启动时会强制检测硬件与全部依赖（Python 包、ffmpeg / yt-dlp / whisperx、demucs、Confucius4-TTS 目录、Ollama 可达性），缺失即退出并打印修复命令，不静默降级。

## 前置要求

Python 3.10+（conda）· Ollama 运行中 · Confucius4-TTS-CPU 位于同级目录 · WhisperX · ffmpeg（**必须含 libx264**）· `pip install yt-dlp demucs`

自定义配置编辑 `core/config_local.py`，模板见 `core/config_local.example.py`；多数参数可在 Web 设置页动态修改，无需重启。

## 常见问题

| 问题 | 处理 |
|------|------|
| `conda: command not found` | `source ~/.zshrc` 后重试 |
| Ollama 不可达 | 确认 `ollama serve` 监听 11434 |
| WhisperX not found | 先 `conda activate bilimix` |
| TTS 首次运行慢 | 自动下载 ~3 GB 权重，属正常 |
| 下载慢 / 超时 | setup.sh 自动切换清华镜像；或 `export HF_ENDPOINT=https://hf-mirror.com` |
| 视频组装报「未找到 libx264」 | 系统 FFmpeg 缺 libx264，需重装带编码器的版本 |
| 环境损坏 | 重新运行 `./setup.sh` |

## SDK

```bash
pip install -e sdk/
bmx task submit --url https://example.com/episode.mp3 --wait
bmx task list
bmx task result <task_id>
bmx video download --task-id <task_id> -o dubbed.mp4
bmx tts synth --ref-audio ./ref.wav --text "要合成的话" --wait -o out.wav
```

完整命令见 `sdk/README.md`（CLI 参考 `sdk/docs/cli.md`），接口与配置默认值见 `services/web_app.py` 与 `core/config.py`。

## License

MIT
