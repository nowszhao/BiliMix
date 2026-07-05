# BiliMix — 中英混合播客学习工具

将英文播客/音频自动转录为文本，通过本地 LLM 逐句翻译为中文，再用 Confucius4-TTS 进行零样本声音克隆合成，
最终组装为中英交替音频，让你跨越语言障碍，沉浸式地收听海外资讯。

## 核心流程

```
音频 → Step 1: WhisperX 转录 → Step 2: Ollama 逐句翻译 → Step 3: Confucius4-TTS 合成 → Step 4: 中英交替组装
```

1. **WhisperX 转录** — 英文语音转文字，带词级时间戳与说话人分离
2. **Ollama LLM 翻译** — 逐句翻译全部英文句子为中文（100% 覆盖）
3. **Confucius4-TTS 合成** — 零样本声音克隆，用原始说话人的音色朗读中文翻译
4. **音频组装** — 按句子级时间戳将英文原文与中文翻译交替拼装输出

## 技术栈

| 组件 | 用途 |
|------|------|
| WhisperX | 英文语音转文字（词级时间戳 + 说话人分离） |
| Ollama | 本地 LLM 批量句子翻译 |
| Confucius4-TTS-CPU | 零样本多语言声音克隆 TTS |
| Flask | Web 服务 + REST API |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Web 服务
python services/web_app.py
# 浏览器访问 http://localhost:5000
# 默认账号: admin / bilimix2024
```

## 目录结构

```
BiliMix/
├── services/
│   ├── web_app.py               # Flask Web 服务 + REST API
│   └── podcast_service.py       # 播客搜索与 RSS 解析
├── pipeline/
│   ├── step1_transcribe.py      # WhisperX 转录
│   ├── step2b_translate_sentences.py  # LLM 批量逐句翻译
│   ├── step3_tts_confucius.py   # Confucius4-TTS 合成（支持并行 Worker）
│   ├── step4b_sentence_mixer.py # 中英交替音频组装
│   └── ref_audio_utils.py       # 参考音频提取（声音克隆用）
├── workers/
│   └── confucius_tts_worker.py  # TTS Worker 独立子进程
├── core/
│   ├── config.py                # 全局配置
│   ├── config_manager.py        # Web 配置管理
│   ├── database.py              # SQLite 数据库（任务、订阅、历史等）
│   ├── task_manager.py          # 任务状态管理与断点恢复
│   └── llm_utils.py             # Ollama API 调用工具
├── web/                         # 前端页面 (HTML/CSS/JS)
├── sdk/                         # Python CLI SDK
├── config.py                    # 根目录配置入口
├── requirements.txt
└── README.md
```

## 配置说明

核心配置项 (`core/config.py`)：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| WHISPERX_MODEL | base | WhisperX 模型大小 |
| WHISPERX_DEVICE | cpu | 转录设备 (cpu/cuda) |
| WHISPERX_BATCH_SIZE | 10 | 转录批处理大小 |
| OLLAMA_MODEL | translategemma:12b | LLM 翻译模型 |
| OLLAMA_BASE_URL | http://localhost:11434 | Ollama 服务地址 |
| LLM_BATCH_SIZE | 5 | 每批合并翻译的句子数 |
| LLM_NUM_PREDICT | 8192 | LLM 单次最大输出 token 数 |
| SENTENCE_CN_RATIO | 1.0 | 翻译比例（固定 100%） |
| TTS_ENGINE | confucius-tts | TTS 引擎 |
| TTS_TEXT_FORMAT | chinese_only | TTS 文本格式 |
| CONFUCIUS4_TTS_DEVICE | cpu | 推理设备 (cpu/cuda) |
| CONFUCIUS4_TTS_N_TIMESTEPS | 40 | 扩散步数（越高音质越好） |
| CONFUCIUS4_TTS_NUM_WORKERS | 2 | 并行 Worker 数（1=串行） |
| CONFUCIUS4_TTS_TEMPERATURE | 0.8 | T2S 采样温度 |
| SENTENCE_GAP_MS | 400 | 中-英句子间间隔 (ms) |
| SENTENCE_FULL_GAP_MS | 250 | 完整句子组间间隔 (ms) |

所有配置项均可在 Web UI 或 CLI 中动态修改。

## 处理模式

仅支持 **全文句子翻译模式**（sentence_translate）：
- 对全部英文句子进行 100% 逐句翻译
- 使用声音克隆技术，用原始说话人音色朗读中文翻译
- 最终输出为「英文原文 → 中文翻译 → 英文原文 → 中文翻译…」交替音频

不再支持单词替换、生词识别等模式。

## 前置要求

1. **Python 3.10+** + conda 环境
2. **Ollama** 服务运行中，已拉取翻译模型（如 `ollama pull translategemma:12b`）
3. **Confucius4-TTS-CPU** 已克隆到 BiliMix 同级目录
4. **WhisperX** 已安装（转录引擎，建议独立 conda 环境）

## API 概览

### 任务管理

| 端点 | 方法 | 说明 |
|------|------|------|
| /api/submit | POST | 提交音频处理任务 |
| /api/upload | POST | 上传本地音频文件 |
| /api/tasks | GET | 获取任务列表 |
| /api/task/<id> | GET | 查询任务状态 |
| /api/task/<id>/result | GET | 获取任务完整结果 |
| /api/task/<id>/cancel | POST | 终止任务 |
| /api/task/<id>/confirm_sentences | POST | 确认句子翻译后继续 |
| /api/task/<id>/retry | POST | 断点续传重试失败任务 |
| /api/task/<id>/retry-synthesis | POST | 仅重试 TTS 合成步骤 |
| /api/task/<id> | DELETE | 删除任务及其文件 |

### 播客 & 内容

| 端点 | 方法 | 说明 |
|------|------|------|
| /api/podcast/search | GET | 搜索播客 |
| /api/podcast/rss | GET | 解析 RSS Feed |
| /api/favorites | GET | 获取播客收藏列表 |
| /api/favorites | POST | 添加播客收藏 |
| /api/favorites | DELETE | 移除播客收藏 |
| /api/favorites/check | GET | 检查是否已收藏 |
| /api/subscriptions | GET | 获取 RSS 订阅列表 |
| /api/subscriptions | POST | 添加 RSS 订阅 |
| /api/subscriptions | DELETE | 移除 RSS 订阅 |
| /api/search-history/suggestions | GET | 获取搜索建议 |
| /api/search-history | POST | 记录搜索关键词 |
| /api/search-history | DELETE | 清空搜索历史 |
| /api/recent-podcasts | GET | 获取最近使用的播客源 |

### 工具 & 配置

| 端点 | 方法 | 说明 |
|------|------|------|
| /api/config | GET | 获取当前配置 |
| /api/config | POST | 更新配置 |
| /api/translate | POST | 翻译单个英文词/短语 |
| /api/word-levels | POST | 查询 BNC/COCA 词频等级 |
| /api/audio/<path> | GET | 提供音频文件流 |
| /api | GET | API 元信息 |

## SDK

Python CLI SDK 位于 `sdk/` 目录：

```bash
pip install -e sdk/
bmx task submit --url https://example.com/episode.mp3 --wait
bmx task list
bmx task result <task_id>
```

详细用法见 `sdk/README.md`。
