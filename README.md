<h1 align="center">
  🎧 BiliMix — Bilingual Mix Audio Learning
</h1>

<p align="center">
  <strong>英文播客生词替换工具 — 边听边学，沉浸式记忆</strong>
</p>

<p align="center">
  将英文播客中超出你词汇水平的生僻词汇和短语，自动替换为中文语音，<br/>
  生成「中英混合音频」，实现无痛听力提升。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/WhisperX-词级时间戳-green" alt="WhisperX">
  <img src="https://img.shields.io/badge/Qwen3.5-9B-orange?logo=alibabacloud" alt="Qwen3.5">
  <img src="https://img.shields.io/badge/TTS-声音克隆-purple" alt="TTS">
  <img src="https://img.shields.io/badge/Flask-Web_UI-red?logo=flask" alt="Flask">
</p>

---

## 📖 目录

- [核心功能](#-核心功能)
- [工作原理](#-工作原理)
- [技术架构](#-技术架构)
- [环境要求](#-环境要求)
- [安装部署](#-安装部署)
- [快速开始](#-快速开始)
- [配置说明](#-配置说明)
- [项目结构](#-项目结构)
- [处理流程详解](#-处理流程详解)
- [Web 界面](#-web-界面)
- [性能说明](#-性能说明)
- [常见问题](#-常见问题)

---

## ✨ 核心功能

| 功能 | 描述 |
|------|------|
| 🎯 **智能生词识别** | 基于 Qwen3.5 大模型逐句分析，结合 BNC/COCA 25000 词频表双重过滤，精准识别超出你水平的词汇 |
| 🗣️ **声音克隆 TTS** | 使用 Qwen3-TTS 克隆原始播客说话人的声音，用"原声"朗读中文翻译，听感自然不违和 |
| 📊 **多难度分级** | 支持 CET-4 / CET-6 / IELTS-6 / IELTS-7 / ADVANCED 五档难度，精准匹配你的英语水平 |
| 🔄 **音频无缝拼接** | 精确到毫秒级的时间戳定位与音频替换，保持播客原有节奏 |
| 📚 **生词本导出** | 自动生成结构化生词本（JSON + TXT），含时间戳，方便复习 |
| 🌐 **Web 可视化** | 精美的 Web 界面，支持在线提交、进度追踪、原文转录同步滚动、全屏阅读 |

---

## 🔬 工作原理

```
┌─────────────────────────────────────────────────────────────────────┐
│                          输入: 英文播客音频                            │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │   Step 1: WhisperX    │  音频 → 词级别时间戳
              │   语音转录 + 对齐     │  每个单词精确到毫秒
              └───────────┬───────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │  Step 2: Qwen3.5 LLM │  逐批句子分析
              │  生词识别 + 中文翻译   │  BNC/COCA 词频过滤
              └───────────┬───────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │  Step 3: TTS 语音合成  │  ┌─ Edge-TTS (在线、快速)
              │  中文翻译 → 中文语音   │  └─ Qwen3-TTS (声音克隆)
              └───────────┬───────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │  Step 4: 音频编辑拼接  │  原始英文 + 中文 TTS
              │  精确替换 + 无缝衔接   │  → 中英混合音频
              └───────────┬───────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  输出:                                                               │
│  📁 mixed_audio.mp3    ← 中英混合音频                                 │
│  📁 vocabulary_book    ← JSON + TXT 生词本                           │
│  📁 difficult_words    ← LLM 完整识别结果                             │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🏗️ 技术架构

| 层面 | 技术 | 说明 |
|------|------|------|
| **语音转录** | [WhisperX](https://github.com/m-bain/whisperX) | OpenAI Whisper + 强制对齐，实现词级别时间戳 |
| **大模型推理** | [Ollama](https://ollama.ai/) + Qwen3.5:9B | 本地部署，零延迟，支持批量生词识别 |
| **词频过滤** | BNC/COCA 25000 词频表 | 25000 词头 + 全部词形变化，无需词形还原 |
| **TTS 引擎 A** | [Edge-TTS](https://github.com/rany2/edge-tts) | 微软在线 TTS，毫秒级合成，零部署成本 |
| **TTS 引擎 B** | [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) | 本地声音克隆，12Hz codec，0.6B / 1.7B 参数量 |
| **音频处理** | [pydub](https://github.com/jiaaro/pydub) + FFmpeg | 格式转换、切割、拼接、采样率统一 |
| **Web 后端** | Flask | REST API + 后台线程 + 任务持久化 |
| **Web 前端** | 原生 HTML/CSS/JS | Apple 设计风格，无框架依赖，全屏转录同步 |

### 多环境隔离架构

```
主进程 (Flask / Python 3.x)
    │
    ├── subprocess ──→ whisperx conda 环境 (whisperx_new)
    │                    └─ WhisperX + PyTorch + transformers
    │
    └── subprocess ──→ qwen3-tts conda 环境 (qwen3-tts)
                         └─ Qwen3-TTS + PyTorch + soundfile
                         └─ 通过 JSON 文件传参，stdout 返回结果
```

> 各 AI 模型的依赖存在版本冲突，通过 conda 环境隔离 + subprocess 跨进程调用解决。

---

## 💻 环境要求

### 硬件

| 组件 | 最低配置 | 推荐配置 |
|------|---------|---------|
| CPU | 4 核 | 8 核+ |
| 内存 | 8 GB | 16 GB+ |
| 显卡 | 无（CPU 模式） | NVIDIA GPU 6GB+ 显存 |
| 磁盘 | 10 GB | 20 GB+（含模型） |

### 软件

- Python 3.10+
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) / Anaconda
- [FFmpeg](https://ffmpeg.org/)
- [Ollama](https://ollama.ai/)（本地大模型推理）

---

## 📦 安装部署

### 1. 安装 Ollama & 拉取模型

```bash
# 安装 Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# 拉取 Qwen3.5 模型 (9B 参数，约 5.5GB)
ollama pull qwen3.5:9b

# 验证服务运行
ollama list
```

### 2. 创建 WhisperX 环境

```bash
conda create -n whisperx_new python=3.10 -y
conda activate whisperx_new

pip install whisperx
# 或从源码安装: pip install git+https://github.com/m-bain/whisperX.git
```

### 3. 创建 Qwen3-TTS 环境（可选，声音克隆模式）

```bash
conda create -n qwen3-tts python=3.10 -y
conda activate qwen3-tts

# 安装 Qwen3-TTS 依赖
pip install torch torchaudio soundfile numpy
pip install qwen-tts  # 或按照 Qwen3-TTS 官方文档安装

# 下载模型
# 0.6B (轻量): Qwen/Qwen3-TTS-12Hz-0.6B-Base
# 1.7B (推荐): Qwen/Qwen3-TTS-12Hz-1.7B-Base
```

### 4. 安装主项目依赖

```bash
# 回到主项目目录
cd /path/to/whipserx

pip install flask pydub edge-tts requests
```

### 5. 安装 FFmpeg

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install ffmpeg -y

# macOS
brew install ffmpeg
```

### 6. 修改配置

编辑 `config.py`，根据你的环境修改关键路径：

```python
# WhisperX 可执行文件路径
WHISPERX_BIN = "/path/to/conda/envs/whisperx_new/bin/whisperx"

# Qwen3-TTS 配置（使用声音克隆时）
QWEN3_TTS_MODEL_PATH = "/path/to/Qwen3-TTS-12Hz-1.7B-Base"
QWEN3_TTS_PYTHON = "/path/to/conda/envs/qwen3-tts/bin/python"
QWEN3_TTS_DEVICE = "cuda:0"  # 有 GPU 则用 cuda，否则 cpu
```

---

## 🚀 快速开始

### 方式一：Web 界面（推荐）

```bash
python web_app.py
```

打开浏览器访问 `http://localhost:5000`：

1. 粘贴播客音频 URL（支持 mp3/wav/m4a 等格式的直链）
2. 选择你的英语水平（CET-4 / CET-6 / IELTS 等）
3. 点击「开始生成」
4. 等待处理完成，在线试听混合音频、浏览转录原文和生词本

### 方式二：命令行

```bash
python main.py /path/to/podcast.mp3
```

处理结果输出到 `data/results/{basename}/` 目录：

```
data/results/podcast/
├── podcast_mixed.mp3        # 中英混合音频
├── vocabulary_book.json     # 结构化生词本
├── vocabulary_book.txt      # 文本版生词本
└── difficult_words.json     # LLM 完整识别结果
```

---

## ⚙️ 配置说明

所有配置集中在 `core/config.py` 中：

### 难度等级

```python
# 支持: CET-4, CET-6, IELTS-6, IELTS-7, ADVANCED
DIFFICULTY_LEVEL = "CET-4"
```

| 等级 | 目标用户 | 词频过滤范围 |
|------|---------|-------------|
| CET-4 | 大学四级水平 | 过滤 BNC/COCA 前 2000 词头 |
| CET-6 | 大学六级水平 | 过滤前 3000 词头 |
| IELTS-6 | 雅思 6 分水平 | 过滤前 3000 词头 |
| IELTS-7 | 雅思 7 分水平 | 过滤前 2000 词头 |
| ADVANCED | 高级学习者 | 仅过滤前 1000 功能词 |

### TTS 引擎选择

```python
# "edge-tts": 微软在线 TTS（快速、免费、无需 GPU）
# "qwen3-tts": 本地声音克隆（音色自然、需要更多资源）
TTS_ENGINE = "qwen3-tts"
```

| 特性 | Edge-TTS | Qwen3-TTS |
|------|----------|-----------|
| 合成速度 | 毫秒级 | 秒级（CPU）/ 亚秒（GPU） |
| 音色 | 固定中文女声 | 克隆原始说话人声音 |
| 部署要求 | 联网即可 | 需下载模型 (1~4GB) |
| 适用场景 | 快速预览 | 高质量产出 |

### Edge-TTS 参数

```python
TTS_VOICE = "zh-CN-XiaoxiaoNeural"  # 声音（活泼、有表现力）
TTS_RATE = "+8%"                     # 语速微调
TTS_PITCH = "+2Hz"                   # 音高微调
```

### Qwen3-TTS 参数

```python
QWEN3_TTS_MODEL_PATH = "/path/to/model"  # 模型路径
QWEN3_TTS_DEVICE = "cpu"                  # "cpu" 或 "cuda:0"
QWEN3_TTS_REF_DURATION = 8                # 参考音频时长（秒）
```

### LLM 配置

```python
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3.5:9b"
LLM_BATCH_SIZE = 8   # 每批合并句子数（5~15，越大越快）
```

### 相邻词合并

```python
# 相邻替换点间隔 < 此值时合并为一句话合成
# 例如 "explicit mass-produced" → "露骨的 批量生产的" 一起合成
ADJACENT_MERGE_GAP = 0.12  # 秒
```

---

## 📁 项目结构

```
whipserx/
│
├── main.py                      # 命令行主入口（串联 Step 1-5）
│
├── core/                        # 核心模块
│   ├── config.py                # 全局配置中心
│   ├── config_manager.py        # Web 端配置读写
│   ├── task_manager.py          # 任务状态管理、持久化、恢复
│   └── word_frequency.py        # BNC/COCA 词频数据查询
│
├── pipeline/                    # 处理流水线
│   ├── step1_transcribe.py      # Step 1: WhisperX 语音转录
│   ├── step2_identify_difficult_words.py  # Step 2: LLM 生词识别 + 词频过滤
│   ├── step2b_translate_sentences.py      # Step 2b: 句子翻译模式
│   ├── step3_tts_synthesize.py  # Step 3A: Edge-TTS 中文语音合成
│   ├── step3_tts_qwen.py        # Step 3B: Qwen3-TTS 声音克隆合成
│   ├── step4_audio_editor.py    # Step 4: 音频编辑拼接
│   └── step4b_sentence_mixer.py # Step 4b: 句子翻译模式音频混合
│
├── services/                    # Web 服务
│   ├── web_app.py               # Flask Web 后端（REST API + 后台任务）
│   └── podcast_service.py       # 播客搜索、RSS 解析
│
├── workers/                     # 独立 Worker 进程
│   └── qwen_tts_worker.py       # Qwen3-TTS 独立 Worker（跨 conda 环境）
│
├── tests/                       # 测试脚本
│   ├── test_mixed_replacement.py
│   ├── test_segment_ref.py
│   └── test_voice_clone_chinese.py
│
├── web/                         # 前端静态资源
│   ├── index.html               # 主页面
│   ├── app.js                   # 前端逻辑（任务管理、转录同步、全屏等）
│   └── style.css                # 样式（Apple 设计风格）
│
├── data/                        # 统一数据目录（运行产物）
│   ├── downloads/               # Web 模式下载的音频文件
│   ├── transcripts/             # WhisperX 转录缓存
│   ├── results/                 # 处理结果输出目录
│   │   └── {basename}/
│   │       ├── {basename}_mixed.mp3
│   │       ├── vocabulary_book.json
│   │       ├── vocabulary_book.txt
│   │       ├── difficult_words.json
│   │       ├── ref_audio/       # 声音克隆参考音频
│   │       └── tts_cache/       # Qwen3-TTS 合成缓存
│   └── tts_cache/               # Edge-TTS 全局合成缓存
│
├── BNC_COCA_lists.csv           # BNC/COCA 25000 词频分级表
├── tasks_index.json             # Web 任务持久化索引
├── requirements.txt             # Python 依赖
└── test.sh                      # WhisperX 命令行测试脚本
```

---

## 🔍 处理流程详解

### Step 1: WhisperX 语音转录

- 调用 WhisperX（OpenAI Whisper + 强制对齐算法）
- 输出句子级 segments + **词级别时间戳**（每个单词精确到毫秒）
- 转录结果自动缓存，相同音频不重复处理

### Step 2: LLM 生词识别

- 将 segments 按 `LLM_BATCH_SIZE` 分批，构建 Few-shot Prompt 发送给 Ollama
- Prompt 设计原则：**宁可漏标，不可错标**
- **双重过滤机制**：
  - **Prompt 层**：详细的正/反面示例引导模型判断
  - **后置层**：BNC/COCA 词频表自动剔除被误标的基础词汇
- BNC/COCA 表内置所有词形变化（manage → managed/manages/managing/management），无需词形还原

### Step 3: TTS 语音合成

**Edge-TTS 模式**：
- 调用微软 Edge-TTS 在线服务
- 支持语速/音高微调
- 毫秒级合成，带文件缓存

**Qwen3-TTS 声音克隆模式**：
- 从原始音频中自动选取最优参考片段（不含替换点的最长 segment）
- 使用 `x_vector_only_mode=True` 仅提取音色嵌入，避免英文参考导致中文输出走形
- **相邻词合并**：紧挨着的生词（间隔 < 120ms）拼成一句话合成，语调更自然
- 动态 `max_new_tokens` 估算，短词短语不浪费推理时间
- 三级音频后处理：硬限时长 → 尾部静音裁剪 → 合理长度截断（智能找静音点）

### Step 4: 音频编辑拼接

- 按时间顺序遍历所有替换点
- 精确切割原始音频的对应时间段，替换为 TTS 中文音频
- 自动统一采样率和声道数
- 支持合并组替换（多个连续词用一段合成音频替换）

### Step 5: 生词本生成

- 输出 JSON（结构化）+ TXT（人类可读）两种格式
- 标记每个词是否成功替换（✅/❌）
- 记录音频时间戳，方便回放定位

---

## 🌐 Web 界面

### 功能特性

| 功能 | 描述 |
|------|------|
| 📤 **任务提交** | 输入音频 URL + 选择难度等级，一键开始 |
| 📊 **实时进度** | 5 步骤进度条 + 百分比，实时跟踪处理状态 |
| ⏹️ **任务终止** | 随时终止运行中的任务（支持杀死子进程） |
| 🔊 **双音频对比** | 原始音频 vs 混合音频，直观对比效果 |
| 📝 **转录同步** | 播放时高亮当前句子，自动滚动跟随 |
| 🖥️ **全屏转录** | 转录内容全屏展示，沉浸式阅读体验 |
| 📖 **生词卡片** | 卡片式生词本，清晰展示英文/中文/类型 |
| 📋 **替换详情** | 表格式展示每处替换的时间、原文、译文 |
| 🕐 **历史任务** | 侧边栏查看所有历史任务，支持删除 |

### API 接口

| 路由 | 方法 | 功能 |
|------|------|------|
| `GET /` | GET | Web 主页 |
| `POST /api/submit` | POST | 提交新任务 |
| `GET /api/task/{id}` | GET | 查询任务状态与进度 |
| `GET /api/task/{id}/result` | GET | 获取完整处理结果 |
| `POST /api/task/{id}/cancel` | POST | 终止任务 |
| `DELETE /api/task/{id}` | DELETE | 删除任务及关联文件 |
| `GET /api/tasks` | GET | 获取所有历史任务列表 |
| `GET /api/audio/{filename}` | GET | 音频文件服务 |

---

## ⚡ 性能说明

### 各步骤耗时参考（5 分钟播客，CPU 模式）

| 步骤 | 耗时 | 备注 |
|------|------|------|
| Step 1: 转录 | ~60s | WhisperX base 模型，首次运行需下载 |
| Step 2: 识词 | ~30s | 约 13 批 LLM 调用（100 句 ÷ 8 句/批） |
| Step 3: TTS (Edge) | ~5s | 在线合成，极快 |
| Step 3: TTS (Qwen3) | ~6-10min | CPU 模式下每词约 10s；GPU 下降至 0.3~1s/词 |
| Step 4: 拼接 | ~3s | 纯内存操作 |

### 优化建议

- **GPU 推理**：将 `QWEN3_TTS_DEVICE` 改为 `"cuda:0"` 可获得 **10~50x** 加速
- **小模型**：使用 `Qwen3-TTS-12Hz-0.6B-Base` 代替 1.7B，速度提升约 **3x**
- **Edge-TTS 快速预览**：将 `TTS_ENGINE` 改为 `"edge-tts"` 可秒级完成合成

### 缓存策略

项目内置多层缓存，重复处理效率极高：

| 缓存层 | 位置 | 说明 |
|--------|------|------|
| 转录缓存 | `output/{basename}.json` | 相同音频不重复转录 |
| TTS 缓存 | `tts_cache/` / `result/*/tts_cache/` | 相同文本 + 相同参数不重复合成 |
| 去重合成 | 运行时 | 相同中文文本只合成一次，多处复用 |

---

## ❓ 常见问题

### Q: 为什么有些词没有被替换？

A: 可能的原因：
1. **WhisperX 未能精确对齐**：某些词的时间戳可能不准确或缺失，导致匹配失败
2. **短语跨 segment**：大模型识别的短语横跨两个 segment，滑动窗口未能匹配
3. **词频过滤**：词频表判断该词属于基础词汇，被后置过滤掉

### Q: 声音克隆效果不好怎么办？

A: 尝试以下调整：
1. 确保原始音频质量好（清晰、低噪音）
2. 增大 `QWEN3_TTS_REF_DURATION`（如 10~15 秒），提供更多参考信息
3. 如果说话人有强烈口音，声音克隆效果可能受限

### Q: Qwen3-TTS 合成很慢？

A: CPU 模式下 1.7B 模型确实较慢（每词约 10 秒）。优化方案：
1. **最优**：切换到 GPU（`QWEN3_TTS_DEVICE = "cuda:0"`），提速 10~50 倍
2. **折中**：使用 0.6B 小模型，提速约 3 倍
3. **快速**：切换到 Edge-TTS（`TTS_ENGINE = "edge-tts"`），毫秒级合成

### Q: Ollama 模型可以换吗？

A: 可以。修改 `core/config.py` 中的 `OLLAMA_MODEL`，任何支持 Ollama 的模型都可以使用。推荐：
- `qwen3.5:9b`（默认，平衡性能与质量）
- `qwen3:14b`（更准确，更慢）
- `llama3.1:8b`（英文能力强）

---

## 📄 许可证

本项目仅供学习和个人使用。所使用的第三方模型和服务请遵循各自的许可协议：
- WhisperX: BSD-4-Clause
- Qwen3-TTS: Apache 2.0
- Edge-TTS: MIT
- Ollama: MIT

---

<p align="center">
  <strong>Powered by BiliMix · WhisperX · Qwen · Qwen3-TTS · Edge-TTS</strong>
</p>
