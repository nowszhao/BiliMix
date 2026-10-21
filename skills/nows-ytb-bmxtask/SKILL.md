---
name: nows-ytb-bmxtask
description: Analyze speakers in a local or downloaded video and submit locally refined bilingual ASS and complete Chinese utterances to a configurable BiliMix dubbing server. Use with nows-ytb-bilingualex for stable per-speaker Chinese voice cloning.
---

# 精翻 → 稳定人物声线 → BiliMix

翻译和完整中文话语由 Code Agent 的模型在本地完成；服务端负责人物分析、中文声音克隆、混音和视频变速。保留 FULL 和精翻，不回退到服务端重译。显示字幕分页不拆 TTS、不添加句内停顿。

## 可移植环境

需要能读写文件并执行命令的 Agent，以及 Python 3.10+。以下 `python` 代表本次选定的解释器；系统命令可能是 `python3` 或 Windows 的 `py -3`。所有命令使用同一 Python 环境。

- 安装 BiliMix 仓库的 SDK：`python -m pip install <BiliMix>/sdk`。
- 服务地址来自用户配置：`--server <URL>` 或 `BILIMIX_SERVER`，不猜固定 IP。省略时采用 SDK 保存的地址/localhost 默认值。
- 可用 `python -m bilimix_cli`；脚本优先选同一环境中的 CLI/模块，支持 `--bmx <可执行文件>` 或 `BILIMIX_CLI`。
- 配套 `nows-ytb-bilingualex` 通常与本 skill 同级；若 Agent 分开安装，使用它的实际路径，不猜用户主目录。
- 客户端不需要 Torch、WhisperX、TTS 或 Hugging Face token；这些在服务端配置。下载网址才需要本地 yt-dlp/FFmpeg，已有 MP4 可跳过下载。

默认下载 720p、关闭 BGM、请求字号 40；用户可覆盖。实际字号会结合分辨率及字幕高度预算适配。

## 流程

1. 输入可为已有 MP4，或由配套 skill 下载的视频。读取标题/简介/章节整理术语；纯音乐、无台词视频不提交配音。每个视频使用独立工作目录。
2. 检查 `python -m bilimix_cli --server <URL> auth status`。需要登录时按用户提供的账号执行 auth login，不在日志或工作文件保存密码。
3. 上传并预分析：

```text
python <本skill>/scripts/bmx_video_task.py --video <MP4> --workdir <工作目录> --server <URL> --analyze-only
```

服务需支持音频优先协议 1。分析任务 ID 在提交后立即写入 `analysis_state.json`；等待被中断时重跑本命令，继续同一分析。失败任务用 SDK 的 task status/retry 排查，不盲目再提交。

4. 阅读配套 SKILL.md，执行 import_analysis → FULL 语义整理 → 精翻 → 完整中文话语重组，得到工作 ASS 和 `dub_manifest.json`。
5. 一次任务引用同一视频、精翻 ASS 和 manifest：

```text
python <本skill>/scripts/bmx_video_task.py --video <MP4> --workdir <工作目录> --server <URL> --subtitle <工作ASS> --dub-manifest <dub_manifest.json>
```

人物身份来自原声分析，同一人物全片固定使用合格参考。短答也不借另一人的声线。只改 ASS Name 不等于人物识别正确。

6. 跟踪任务，下载最终视频、主音频及服务端最终 ASS。以 `video_result.subtitle_layout` 检查分页数量、最大行数和高度预算。字幕时间按配音后的音频轴生成。

## 状态和交付

脚本 stdout 是 JSON，诊断在 stderr。状态文件绑定服务器和媒体哈希，不能把另一服务器的上传路径拿来复用。

- `--dry-run`：无网络、无写文件；`--no-submit`：上传并校验，不创建配音任务。
- `dubbing_state.json` 记录已提交任务；相同请求重跑会返回已有 task_id。要创建另一条任务时才用 `--new-task`。
- `submission_pending.json` 表示提交响应未确认。先查服务器 task list，避免网络中断后重复付出合成成本；不是服务端原子幂等保证。
- `--fresh-upload` 丢弃本地上传/分析绑定。文件已被服务端清理时按实际状态决定是否重传、重新分析。
- 参考不足、人物不明、TTS 缺失或外部精翻文件无效必须明确失败，不能换声音、漏句或自动重译。

检查 `speaker_verification_summary` 和 `speaker_verified`，随成片列出低置信人物的源时间与话语。固定参考绑定正确不代表人物模型全部识别正确。Demucs 分离人声/背景，pyannote 区分人物；不要混淆。

精简命令参考见 [references/bmx_reference.md](references/bmx_reference.md)。
