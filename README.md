# BiliMix — 中英混合播客/视频学习工具

将英文播客/音频/视频通过本地 LLM 逐句翻译为中文，用 Confucius4-TTS 零样本声音克隆朗读中文译文，
组装为中英交替音频（或带字幕的配音视频），让你跨越语言障碍，沉浸式收听海外内容。

## 支持场景

| 场景 | 输入 | 输出 | 说明 |
|------|------|------|------|
| 🎧 音频转录 | URL / 本地文件 | 中英交替 MP3 | 播客、音频课程 |
| 🎬 视频配音 | YouTube / 本地 MP4 / 服务器路径 | 中英配音 MP4 + SRT 字幕 | 含中英双语/纯中文可选 |

## 核心流程

```
音频/视频 → 转录 → 翻译 → 声音克隆 TTS → 组装（音频/视频 + 字幕）
```

1. **Step 0: 素材准备** — URL 下载 或 本地上传，视频可直传或指定服务器路径
2. **Step 1: WhisperX 转录** — 语音转文字，带词级时间戳与说话人分离
3. **Step 2: Ollama LLM 翻译** — 逐句翻译全部句子为中文（100% 覆盖）
4. **Step 3: Confucius4-TTS 合成** — 零样本声音克隆，保留原说话人音色
5. **Step 4: 音频/视频组装** — 中英交替拼接；视频模式额外渲染字幕并合成 MP4

## Web 前端

- **暗色侧边栏** — 更新 / 任务 / 设置 三页导航，活跃项 accent 色高亮
- **更新页** — 播客订阅聚合，按时间（今日/本周/本月/全部）+ 状态筛选单集
- **任务页** — 表格式任务列表，支持状态/类型筛选 + 排序 + 搜索；一键重做、删除
- **新建任务弹窗** — 音频/视频双模式切换；URL/上传/服务器路径多种输入方式；
  背景音乐保留、字幕模式、字幕字号等选项卡片
- **设置页** — 全页布局，双列网格，配置组可折叠；涵盖 TTS、WhisperX、Ollama 等全部参数
- **进度条** — 实时步骤指示 + 断点续传重试
- **结果页** — 原始音频/混合音频对比播放，原文转录 + 翻译句对展示
- **视频详情页** — 原始/配音双标签切换播放，字幕带播放高亮、支持下载

## 技术栈

| 组件 | 用途 |
|------|------|
| WhisperX | 英文语音转文字（词级时间戳 + 说话人分离） |
| Ollama | 本地 LLM 批量句子翻译 |
| Confucius4-TTS-CPU | 零样本多语言声音克隆 TTS |
| FFmpeg | 音频/视频转码、字幕烧录、人声分离 |
| yt-dlp | YouTube 视频下载 |
| Flask | Web 服务 + REST API |
| SQLite | 任务、订阅、搜索历史持久化 |

## 快速开始

### 一键安装（推荐）

```bash
./setup.sh
```

`setup.sh` 会自动完成：
1. 安装 Miniconda（如未安装）并创建 `bilimix` conda env（Python 3.10）
2. 安装系统依赖（FFmpeg / Ollama，macOS 用 brew，Linux 用 apt）
3. 安装 Python 依赖（`requirements.txt` + whisperx + `requirements-tts.txt`）
4. clone Confucius4-TTS-CPU 到 `../Confucius4-TTS-CPU`
5. 从模板 `core/config_local.example.py` 生成 `core/config_local.py`（自动填入检测到的路径）

> 幂等：可重复运行 `./setup.sh`，已完成的步骤会自动跳过。

### 启动

```bash
# 1. 启动 Ollama 服务（首次需要）
ollama serve                   # 或 brew services start ollama

# 2. 拉取翻译模型
ollama pull translategemma:4b

# 3. 激活 conda env 并启动 BiliMix
conda activate bilimix
python services/web_app.py 5555

# 4. 浏览器访问 http://localhost:5555
```

### 启动时依赖检测

> ⚠️ **启动时会强制检测所有依赖**，缺失任何一项服务都会直接退出，**不会静默降级**。

检测范围：
- Python 包（flask / pydub / torch / torchaudio / soundfile / transformers）
- CLI 工具（ffmpeg / yt-dlp / whisperx）
- demucs 子进程（`sys.executable -m demucs`）
- Confucius4-TTS 目录 + worker 脚本
- Ollama 服务可达

如果依赖缺失，启动会看到类似错误：
```
============================================================
❌ 启动失败：缺少以下依赖：
  - Python 模块 demucs (pip install demucs)
  - ffmpeg 不在 PATH（brew install ffmpeg）
  - Confucius4-TTS 目录未配置
      安装：git clone https://github.com/nowszhao/Confucius4-TTS-CPU.git ../Confucius4-TTS-CPU
  - Ollama 服务不可达 (http://localhost:11434)
      启动：ollama serve
============================================================
```

### 手动配置

如需自定义配置（如不同模型、不同 Python 环境），编辑 `core/config_local.py`。模板参考 `core/config_local.example.py`。

### Troubleshooting

| 问题 | 解决方案 |
|------|---------|
| `conda: command not found` | `source ~/.zshrc` 或 `source ~/.bashrc` 后重试 |
| Ollama 不可达 | 确认 `ollama serve` 已启动，监听 11434 |
| WhisperX not found | 确保已 `conda activate bilimix`（whisperx 在 env PATH） |
| TTS 首次运行慢 | 自动从 HuggingFace 下载模型权重 ~3GB，属正常 |
| 环境损坏 | 重新运行 `./setup.sh`（幂等，会修复缺失部分） |
| demucs 子进程失败 | 检查 `sys.executable` 是否在 bilimix env 中 |

## 目录结构

```
BiliMix/
├── services/
│   ├── web_app.py                # Flask Web 服务 + REST API
│   └── podcast_service.py        # 播客搜索与 RSS 解析
├── pipeline/
│   ├── step0_video_prepare.py    # 视频下载/预处理 (yt-dlp)
│   ├── step1_transcribe.py       # WhisperX 转录
│   ├── step2b_translate_sentences.py  # LLM 批量逐句翻译
│   ├── step3_tts_confucius.py    # Confucius4-TTS 合成（并行 Worker）
│   ├── step4b_sentence_mixer.py  # 中英交替音频组装
│   ├── step5_video_assemble.py   # 视频字幕烧录与最终组装
│   └── step_vocal_separation.py  # 人声/背景音分离 (demucs)
├── workers/
│   └── confucius_tts_worker.py   # TTS Worker 独立子进程
├── core/
│   ├── config.py                 # 全局配置
│   ├── config_manager.py         # Web 配置管理（读取/更新/写回）
│   ├── database.py               # SQLite 数据库
│   ├── task_manager.py           # 任务状态管理与断点恢复
│   └── llm_utils.py              # Ollama API 调用工具
├── web/
│   ├── index.html                # 单页应用入口
│   ├── style.css                 # 基础样式
│   ├── apple_overrides.css       # Apple HIG 设计覆盖
│   └── js/
│       ├── state.js              # 全局状态
│       ├── utils.js              # 工具函数
│       ├── task.js               # 任务提交/取消/轮询/进度
│       ├── settings.js           # 设置、任务列表、历史、确认弹窗
│       ├── episodes.js           # 更新页、订阅、页面导航
│       ├── podcast.js            # 播客搜索
│       ├── confirm.js            # 翻译确认
│       ├── result.js             # 结果展示
│       └── audio-sync.js         # 音频同步/时间轴
├── sdk/                          # Python CLI SDK
├── config.py                     # 根目录配置入口
├── requirements.txt
└── README.md
```

## 配置说明

所有配置均可在 Web 设置页面动态修改，无需重启服务。

### 核心参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| SKIP_CONFIRMATION | True | 跳过翻译确认环节 |
| SENTENCE_CN_RATIO | 1.0 | 中文翻译比例（固定 100%） |
| SENTENCE_GAP_MS | 400 | 中英交替句间间隔 (ms) |
| SENTENCE_FULL_GAP_MS | 250 | 全翻译模式句间间隔 (ms) |
| SENTENCE_TTS_VOICE_CLONE | True | 翻译 TTS 是否克隆原声 |
| KEEP_BGM | False | 新建任务时默认保留背景音乐 |

### TTS (Confucius4)

| 参数 | 默认值 | 说明 |
|------|--------|------|
| CONFUCIUS4_TTS_DEVICE | cpu | 推理设备 |
| CONFUCIUS4_TTS_TEMPERATURE | 0.3 | 采样温度 |
| CONFUCIUS4_TTS_TOP_P | 0.9 | 核采样阈值 |
| CONFUCIUS4_TTS_N_TIMESTEPS | 25 | 扩散步数 |
| CONFUCIUS4_TTS_INFERENCE_CFG_RATE | 0.9 | CFG 引导强度 |
| CONFUCIUS4_TTS_NUM_WORKERS | 2 | 并行 Worker 数 |

### WhisperX

| 参数 | 默认值 | 说明 |
|------|--------|------|
| WHISPERX_MODEL | medium | 模型大小 |
| WHISPERX_DEVICE | cpu | 推理设备 |
| WHISPERX_LANGUAGE | en | 音频语言 |
| WHISPERX_THREADS | 8 | CPU 线程数 |

### Ollama

| 参数 | 默认值 | 说明 |
|------|--------|------|
| OLLAMA_MODEL | translategemma:12b | 翻译模型 |
| OLLAMA_BASE_URL | http://localhost:11434 | 服务地址 |
| LLM_BATCH_SIZE | 5 | 每批合并翻译句数 |

## API 概览

### 任务管理

| 端点 | 方法 | 说明 |
|------|------|------|
| /api/submit | POST | 提交处理任务（音频/视频） |
| /api/upload | POST | 上传本地文件 |
| /api/tasks | GET | 获取任务列表 |
| /api/task/<id> | GET | 查询任务状态与进度 |
| /api/task/<id>/result | GET | 获取任务完整结果 |
| /api/task/<id>/cancel | POST | 终止任务 |
| /api/task/<id>/confirm_sentences | POST | 确认翻译后继续 |
| /api/task/<id>/retry | POST | 断点续传重试 |
| /api/task/<id>/redo | POST | 完整重做（清空所有产物，仅保留源文件） |
| /api/task/<id> | DELETE | 删除任务及其所有文件 |

### 播客 & 订阅

| 端点 | 方法 | 说明 |
|------|------|------|
| /api/podcast/search | GET | 搜索播客 |
| /api/podcast/rss | GET | 解析 RSS Feed |
| /api/subscriptions | GET/POST/DELETE | 订阅管理 |
| /api/favorites | GET/POST/DELETE | 播客收藏 |
| /api/search-history | GET/POST/DELETE | 搜索历史 |
| /api/recent-podcasts | GET | 最近使用的播客源 |

### 工具 & 配置

| 端点 | 方法 | 说明 |
|------|------|------|
| /api/config | GET | 获取全部配置 |
| /api/config | POST | 更新配置（写回文件） |
| /api/translate | POST | 翻译英文词/短语 |
| /api/word-levels | POST | 查询 BNC/COCA 词频等级 |
| /api/audio/<path> | GET | 提供音频文件流 |
| /api | GET | API 元信息 |

## 前置要求

1. **Python 3.10+** + conda 环境
2. **Ollama** 服务运行中，已拉取翻译模型
3. **Confucius4-TTS-CPU** 已克隆到 BiliMix 同级目录
4. **WhisperX** 已安装（转录引擎，建议独立 conda 环境）
5. **ffmpeg** 系统已安装
6. **yt-dlp** (pip install yt-dlp) — 视频配音必需
7. **demucs** (pip install demucs) — 背景音乐保留必需

## SDK

```bash
pip install -e sdk/
bmx task submit --url https://example.com/episode.mp3 --wait
bmx task list
bmx task result <task_id>
bmx video submit --url https://www.youtube.com/watch?v=xxx
bmx download <task_id>
```

## License

MIT
