# Portable Agent workflow and subtitle layout

两份 skill 的源码在仓库 `skills/`。任何具备文件读写、命令执行能力的 Code Agent 都可以读取 SKILL.md 并运行 Python 脚本，不需要 WorkBuddy、Codex 或某个用户的主目录。Agent 不支持自动发现 skill 时，明确让它读取安装路径的 SKILL.md 即可。

## 客户端：macOS、Linux、Windows

客户端只需要 Python 3.10+ 和轻量 SDK；本地下载网址时再加 yt-dlp/FFmpeg。不安装 Torch、WhisperX、Demucs 或 TTS，也不需要服务器的 Hugging Face token。

以下 `python` 指选定的解释器；按系统使用 `python3` 或 `py -3`，始终在同一环境安装和运行。

```text
python -m pip install /path/to/BiliMix/sdk
python /path/to/BiliMix/scripts/install_skills.py --destination /path/to/agent/skills
python -m bilimix_cli --server https://your-server.example auth status
```

已有安装用 `--update`：安装器先备份旧目录，再替换；不猜 Agent 的安装目录。也可分发以下 ZIP 后解压，使两个 skill 目录保持相邻：

```text
python scripts/package_skills.py --output dist/skills
```

ZIP 仅包含 SKILL.md、维护中的脚本和引用文档，不包含模型、用户配置、cookies、token、缓存、yt_env.json 或视频任务文件。源码不绑定某个 Agent 的模型接口；FULL/翻译仍由当前 Agent 写出规定格式的文本与 JSON。

若 shell 找不到 bmx，使用 `python -m bilimix_cli`。脚本优先寻找所选 Python 的 CLI/模块，然后才检查 PATH；显式 `--bmx` 或 BILIMIX_CLI 可以覆盖。

## 下载与已有视频

每个视频使用独立工作目录。已有 MP4 可跳过本地下载器，直接预分析。公开 YouTube 默认不读取浏览器 cookies；确需登录时才明确选择浏览器/配置或 cookie 文件。

```text
python <精翻skill>/scripts/check_env.py --env-out <work>/yt_env.json
python <精翻skill>/scripts/download.py --url <URL> --output <work> --env-out <work>/yt_env.json
```

检查默认不安装依赖；加 `--install-missing` 才安装独立工具 venv。工具 JSON 记录当前机器的可执行文件位置，换电脑后重新生成。下载显式传递发现的 FFmpeg，支持 Windows Scripts 和 Unix bin 目录，输出文件名处理保留名、非法字符与 UTF-8 文件名长度。下载失败不会拿目录中的另一个 MP4 冒充成功。

## 服务连接、恢复和重复提交

服务选择顺序：`--server` > BILIMIX_SERVER > SDK 保存的配置 > localhost:5000。BILIMIX_HOME 可改变客户端配置目录。SDK 登录会话按服务 URL 隔离；旧版无服务器归属的 session 不迁移发送，启用登录的服务器需要重新登录一次。

analysis_state.json 绑定服务器和媒体 SHA256。分析任务 ID 在等待之前持久化；重跑会继续同一个任务，而不是只因等待中断就再次分析。同一目录的状态不能混给不同视频。

dubbing_state.json 复用相同请求的 task_id。需要新任务时才使用 `--new-task`。服务端提交仍非原子幂等；submission_pending.json 表示提交结果未确认，必须先检查服务端历史，不能自动重发昂贵的合成请求。读取类操作可以有限重试，提交操作不盲目重放。

文件已从服务器清理时，按实际状态决定重传/重新分析；`--fresh-upload` 忽略本地旧上传绑定。工作 ASS 和 manifest 同时用于最终任务；无效外部字幕明确失败，不调用服务端重译。

## 服务端：明确模型依赖

当前完整语音链路实际验证环境是 Linux x86_64、Python 3.10。客户端的跨系统兼容不意味着所有电脑都能直接运行所有模型；Windows 用户可连接服务器，或在支持的 Linux/WSL 环境部署。模型服务器还需要足够内存、模型文件/下载权限、带 libass/libx264/drawtext 的 FFmpeg 7+ 及 FFprobe，以及 Noto Sans CJK SC 字体。

运行环境检查，不会安装依赖、下载模型或提交任务：

```text
python scripts/doctor.py --scope client --server <URL>
python scripts/doctor.py --scope server
python -m pip check
```

服务端的配置导入会确保数据目录存在；doctor 不加载语音模型。`token_configured` 只报告布尔值，不输出凭据；token 或缓存存在也不等于账号已有受限仓库访问权。

可配置项：

| 配置 | 作用 |
|---|---|
| BILIMIX_SETTINGS_FILE | Web 设置 JSON 路径；默认数据目录 settings.json |
| BILIMIX_DATA_DIR | 数据和结果根目录；默认仓库 data/ |
| BILIMIX_HOST / BILIMIX_PORT | 监听地址和默认端口；命令行端口仍优先 |
| BILIMIX_ASR_PYTHON | 人物分析/声纹复核解释器；默认服务解释器 |
| BILIMIX_WHISPERX_BIN | WhisperX 可执行文件；复杂命令可在 config_local.py 配置为参数列表 |
| BILIMIX_TTS_PYTHON / BILIMIX_TTS_ROOT | TTS 解释器和源码；默认当前解释器和同级源码目录 |
| HF_TOKEN | 受限模型所需访问凭据，放环境/服务管理器中 |
| HF_HOME / HF_HUB_CACHE / XDG_CACHE_HOME | 自定义模型缓存位置；分析与 worker 共用解析规则 |

Web 界面设置原子写入数据目录 settings.json，文件权限 0600，不再改动源码 config.py 或机器配置。初次保存会保留当前全部界面设置，后续仅合并改动字段；不同线程的修改不会丢失，JSON 中的引号/换行也不会成为可执行 Python。该文件可能含登录设置，已从版本控制与发布包排除。机器配置放 core/config_local.py，不分发已配置文件。示例留空解释器/源码路径就能采用通用默认值。生产服务应配置自己的登录凭据；技能包和示例不会携带部署账号。

服务端安装使用 requirements.txt、requirements-tts.txt 和统一 constraints-speech.txt。已验证组合为 Torch/Torchaudio 2.8、WhisperX 3.8.6、pyannote.audio 4.0.4、NumPy 2.2.6、Transformers 4.57.6；安装后运行 pip check。setup.sh 不再安装 WhisperX 后又降级 Torch/NumPy；新 clone 的 TTS 源码固定到已验证提交，已有源码和模型不覆盖。

这些约束来自实际完成本片合成且 pip check 通过的运行时，不等于所有 GPU/平台已验证。升级模型栈时应重新验证真人声样本。独立的 ASR/TTS 解释器仍可配置，不要求用户采用特定 conda 环境名。

上游依赖依据：[WhisperX 声明](https://github.com/m-bain/whisperX/blob/main/pyproject.toml)、[TorchCodec 版本兼容及 CPU 安装](https://github.com/meta-pytorch/torchcodec#compatibility-with-torch-versions)。

## 字幕遮挡的修复

完整话语是 TTS 单位；显示字幕分页只改变画面文字和显示区间，既不重译也不重新合成音频。

默认每种语言最多 2 行、总共最多 4 行，估算文字高度不超过视频 25%。按真实宽高与字号显式换行，关闭不可预测的 smart wrap；超大请求字号作为上限适配小画面，长词/URL 也有有界换行。每种语言的全部文字和顺序在分页前后校验，页间连续，整体起止不变。

整片与分块调用同一个字幕生成器。最终 ASS 附带 .layout.json，任务结果 video_result.subtitle_layout 暴露汇总。原工作 ASS 是 EN || ZH 导入格式；仅交字幕时用 skill 的 render_subtitles.py 另生成可观看版本，不把工作 ASS 直接当版式验收成品。源字幕时间重叠会明确报错，避免多个事件叠加遮屏。

布局源码是 pipeline/subtitle_layout.py。精翻 skill 中附带同一纯 Python 模块供独立使用；`python scripts/sync_skill_layout.py --check` 检查一致性，打包前必须通过。

## 验证

```text
python -m pytest tests -q
python scripts/sync_skill_layout.py --check
python scripts/package_skills.py --output dist/skills
```

新增回归运行真实 CLI 与临时 HTTP 服务，覆盖干净 HOME、无关 cwd、中文/空格路径、换服务器、任务恢复、重复提交和提交响应不明；模型服务未被模拟结果冒充。布局另测真实长句、小画面、竖屏、大字号、英文长词及原文完整性。CI 配置包含 Windows/macOS/Linux、Python 3.10/3.12，也检查独立配置存储；只有实际运行的平台才能报告为实测通过。
