# BiliMix

![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

把英文播客、音频和视频逐句译成中文，再用原说话人的音色朗读出来 —— 默认得到原音色的中文配音，把 `sentence_cn_ratio` 调到 0.5 就是英文、中文交替讲述；视频模式再烧录双语字幕输出 MP4。
除配音流水线外，还内置播客订阅更新、查词与独立语音合成。转录、翻译、合成全部在本机完成，媒体不出本地。

[English](README.en.md)

## 效果

<table>
<tr><th align="center">原始视频</th><th align="center">配音后（中英双语字幕）</th></tr>
<tr>
<td align="center" width="50%"><a href="examples/test1.mp4"><img src="examples/screenshot_original.jpg" alt="原始视频截图" width="100%"></a></td>
<td align="center" width="50%"><a href="examples/test1_mixed.mp4"><img src="examples/screenshot_mixed.jpg" alt="配音后截图" width="100%"></a></td>
</tr>
</table>

点击图片查看完整视频（`examples/test1.mp4`、`examples/test1_mixed.mp4`）。

## 特性

- **原音色配音**：Confucius4-TTS 零样本克隆说话人音色朗读中文译文，逐句替换原声，不是机械念稿
- **中英比例可调**：`sentence_cn_ratio` 控制替换比例 —— 1.0 逐句全中文，0.5 中英交替，0 保留纯英文原声
- **视频配音与字幕**：YouTube / 本地 MP4 / 服务器路径 → 配音 MP4，字幕可选双语、纯中文或不烧录，可叠加可配置水印
- **已有字幕直接配音**：上传双语 `.ass` / `.srt`，跳过转录与翻译，直接进入语音合成
- **播客订阅与更新**：iTunes 搜索 + RSS 订阅，定时抓取更新、未读统计、收藏与搜索历史
- **可脚本化**：任务按步骤落盘、失败从断点重试，长视频分块并行组装；`bmx` CLI 与 REST API 覆盖全流程

```
音频/视频 → WhisperX 转录 → Ollama 逐句翻译 → Confucius4-TTS 音色克隆 → 组装（音频 / 视频 + 字幕）
```

## 硬件要求

> 安装前请先对照最低配置，否则会跑得很慢或直接失败。

| 资源 | 最低 | 推荐 | 说明 |
|------|:---:|:---:|------|
| CPU | 4 核 | 8 核+ | 转录 8 线程 + TTS 多 worker + FFmpeg 编码 |
| 内存 | 8 GB | 16 GB+ | Ollama 3-4 GB + TTS 2-4 GB/worker（CPU 默认 1 个） |
| 磁盘 | 15 GB | 50 GB+ | Python 环境 + 模型约 10 GB + 音视频缓存 |
| GPU | 不需要 | 可选 6 GB+ | 显著加速 TTS 与 WhisperX |

内存是主要瓶颈。低配机器：翻译模型用 `translategemma:4b`、TTS worker 降为 1、WhisperX 用 `small`。

## 快速开始

### 安装

```bash
curl -fsSL https://raw.githubusercontent.com/nowszhao/BiliMix/main/setup.sh | bash
```

已 clone 本仓库时直接运行 `./setup.sh`。脚本幂等，可重复运行，依次完成：

1. Miniconda + `bilimix` 环境（Python 3.10）
2. 系统依赖（FFmpeg、Ollama）
3. Python 依赖（`requirements.txt` + whisperx + `requirements-tts.txt`）
4. clone Confucius4-TTS-CPU 到 `../Confucius4-TTS-CPU`
5. 由模板生成 `core/config_local.py`

### 启动

```bash
ollama serve                        # 首次需要，或 brew services start ollama
ollama pull translategemma:4b

conda activate bilimix
python services/web_app.py 5555     # 端口可省略，默认 5000
```

浏览器打开 http://localhost:5555。

启动时会强制检测硬件与全部依赖（Python 包、ffmpeg / yt-dlp / whisperx、demucs、Confucius4-TTS 目录、Ollama 可达性），缺失即退出并打印修复命令，不静默降级。

## 输入与输出

| 场景 | 输入 | 输出 |
|------|------|------|
| 音频 | 音频 URL / 本地文件 | 中文配音 MP3（`sentence_cn_ratio` < 1 时为中英交替） |
| 视频 | YouTube URL / 本地 MP4 / 服务器路径 | 配音 MP4 + 双语 / 纯中文 SRT 字幕 |
| 已有字幕 | 音视频 + 双语 `.ass` / `.srt` | 跳过转录与翻译，直接生成配音 |

## 命令行

```bash
pip install -e sdk/

# 音频：URL 或本地文件 → 中英交替 MP3
bmx task submit --url https://example.com/episode.mp3 --wait
bmx audio download --task-id <task_id> -o mixed.mp3

# 视频：YouTube 或服务器路径 → 配音 MP4 + 字幕
bmx task submit --type video --video-url "https://www.youtube.com/watch?v=xxx" --wait
bmx video download --task-id <task_id> -o dubbed.mp4
bmx video download-srt --task-id <task_id> -o dubbed.srt

# 独立语音合成：给一段参考音频 + 文本（支持 zh / en / ja / ko）
bmx tts synth --ref-audio ./ref.wav --text "要合成的话" --wait -o out.wav

# 播客：搜索 → 订阅 → 看更新
bmx podcast search "All Ears English"
bmx subscriptions add --rss-url https://example.com/feed.xml
bmx episodes list --status unread

# 查词与配置
bmx translate word "resilient" --context "She is resilient."
bmx config set sentence_cn_ratio=0.5
```

完整命令见 `sdk/README.md` 与 `sdk/docs/cli.md`。服务地址默认 `http://localhost:5000`，可用 `BILIMIX_SERVER` 覆盖。

## 配置

编辑 `core/config_local.py`（模板 `core/config_local.example.py`）调整模型与默认值；多数参数可在 Web 设置页直接修改，无需重启。常用项：`sentence_cn_ratio`（中英比例）、`keep_bgm`（保留背景音）、`ref_select_mode`（参考音频选取）、`whisperx_model`、TTS worker 数、水印开关与文案。

## 常见问题

| 问题 | 处理 |
|------|------|
| `conda: command not found` | `source ~/.zshrc` 后重试 |
| Ollama 不可达 | 确认 `ollama serve` 监听 11434 |
| WhisperX not found | 先 `conda activate bilimix` |
| TTS 首次运行慢 | 自动下载约 3 GB 权重，属正常 |
| 下载慢或超时 | setup.sh 自动切换清华镜像，或 `export HF_ENDPOINT=https://hf-mirror.com` |
| 视频报「未找到 libx264」 | 系统 FFmpeg 缺 libx264，需重装带编码器的版本 |
| 环境损坏 | 重跑 `./setup.sh` |

## 技术栈

WhisperX（词级时间戳，说话人分离可选、需 HF_TOKEN）· Ollama（本地翻译）· Confucius4-TTS-CPU（零样本声音克隆）· FFmpeg · yt-dlp · demucs · Flask · SQLite

## License

MIT
