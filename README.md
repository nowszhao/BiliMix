# BiliMix — 双语混音学习工具

将英文播客/音频自动转录、全文翻译为中文，并通过 Confucius4-TTS 进行零样本声音克隆合成，
最终组装为中英交替音频，用于沉��式英语听力训练。

## 核心流程

```
音频 → Step 1: WhisperX 转录 → Step 2: Ollama 全文翻译(100%) → Step 3: Confucius4-TTS 合成 → Step 4: 音频组装
```

## 技术栈

| 组件 | 用途 |
|------|------|
| WhisperX | 英文语音转文字（词级时间戳） |
| Ollama | 本地 LLM 句子翻译 |
| Confucius4-TTS-CPU | 零样本多语言声音克隆 TTS |
| Flask | Web 服务 + REST API |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Web 服务
python services/web_app.py
# 浏览器访问 http://localhost:5000
# 默认账号: admin / admin123
```

## 目录结构

```
BiliMix/
├── services/
│   ├── web_app.py               # Flask Web 服务
│   └── podcast_service.py       # 播客搜索与 RSS 解析
├── pipeline/
│   ├── step1_transcribe.py      # WhisperX 转录
│   ├── step2b_translate_sentences.py # LLM 100% 句子翻译
│   ├── step3_tts_confucius.py   # Confucius4-TTS 合成 (支持并行)
│   ├── step4b_sentence_mixer.py # 中英交替音频组装
│   └── ref_audio_utils.py       # 参考音频提取工具
├── workers/
│   └── confucius_tts_worker.py  # TTS Worker 子进程
├── core/
│   ├── config.py                # 全局配置
│   ├── config_manager.py        # Web 配置管理
│   ├── database.py              # SQLite 任务数据库
│   ├── task_manager.py          # 任务状态管理
│   └── llm_utils.py             # Ollama API 工具
└── web/                         # 前端页面 (HTML/CSS/JS)
```

## 配置说明

核心配置项 (`core/config.py`)：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| OLLAMA_MODEL | qwen3:8b | LLM 翻译模型 |
| OLLAMA_BASE_URL | http://localhost:11434 | Ollama 服务地址 |
| CONFUCIUS4_TTS_DEVICE | cpu | 推理设备 (cpu/cuda) |
| CONFUCIUS4_TTS_N_TIMESTEPS | 25 | 扩散步数（音质参数，越高越好） |
| CONFUCIUS4_TTS_NUM_WORKERS | 2 | 并行 Worker 数（1=串行，建议 2-3） |
| CONFUCIUS4_TTS_NUM_BEAMS | 1 | 束搜索宽度（1=贪心解码） |
| CONFUCIUS4_TTS_TEMPERATURE | 0.8 | T2S 采样温度 |
| SENTENCE_CN_RATIO | 1.0 | 翻译比例（固定 100%） |
| SENTENCE_VOICE_CLONE | True | 启用声音克隆 |

## 处理模式

仅支持 **100% 句子翻译模式**：全文逐句翻译为中英交替音频。不再支持单词替换和智能翻译模式。

## 前置要求

1. **Python 3.10+** + conda 环境
2. **Ollama** 服务运行中，已拉取翻译模型 (如 `ollama pull qwen3:8b`)
3. **Confucius4-TTS-CPU** 已克隆到 BiliMix 同级目录
4. **WhisperX** 已安装（转录引擎）

## API 概览

| 端点 | 方法 | 说明 |
|------|------|------|
| /api/config | GET/POST | 获取/更新配置 |
| /api/submit | POST | 提交处理任务 |
| /api/tasks | GET | 获取任务列表 |
| /api/task/<id> | GET | 获取任务状态 |
| /api/task/<id>/cancel | POST | 取消任务 |
| /api/task/<id>/retry | POST | 重试失败任务 |
| /api/podcast/search | GET | 搜索播客 |
| /api/podcast/rss | GET | 解析 RSS Feed |
| /api/audio/<path> | GET | 提供音频文件 |

## SDK

Python SDK 位于 `sdk/` 目录，提供 CLI 工具：

```bash
pip install -e sdk/
bmx task submit --url https://example.com/episode.mp3 --wait
bmx task list
bmx task result <task_id>
```
