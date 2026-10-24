# 安装与配置

## 客户端

Python 3.10+；SDK 通过 `python -m pip install ./sdk` 安装。`bmx` 与 `python -m bilimix_cli` 等价。下载 URL 时另装 yt-dlp/FFmpeg；已有 MP4 可直接上传。客户端无需模型或 HF token。

`python scripts/install_skills.py --destination <Agent技能目录>` 安装两个 Skill；加 `--update` 替换为当前源码，不保留旧目录。Skill 的脚本路径以实际安装位置为准，工作文件写入独立工作目录。

服务地址优先级：`--server` → `BILIMIX_SERVER` → 保存的地址 → `http://localhost:5000`。客户端配置根目录默认 `~/.bilimix`，可由 `BILIMIX_HOME` 指定。

| 文件 | 用途 |
|---|---|
| `config.json` | 服务地址 |
| `sessions/<服务标识>.json` | 该服务器的登录会话，权限 0600 |

```text
bmx --server <服务URL> auth status
bmx --server <服务URL> auth login --username <用户名> --password <密码>
```

公开 YouTube 默认不读取浏览器 cookies。需要登录时明确选择 `--cookies-browser` 或 `--cookies-file`。`check_env.py` 生成的 `yt_env.json` 保存本机工具位置，换电脑后重新生成；`--install-missing` 可安装独立工具环境。

## 模型服务器

服务端目标环境为 Linux x86_64、Python 3.10。`setup.sh` 安装系统工具、Python 依赖与 TTS 源码；语音依赖统一由 `constraints-speech.txt` 约束。需要 FFmpeg 7+ 的 libx264、libass、drawtext 与文件式 filter_complex 支持，另需 FFprobe 和中文字体。

```text
./setup.sh
conda activate bilimix
python -m pip check
python scripts/doctor.py --scope server
python services/web_app.py 5000
```

HF 模型需下载权限；完整模型缓存可离线加载。GPU 型号与驱动应匹配所安装的 Torch/CUDA 运行时。

## 服务端配置

| 文件 | 职责 |
|---|---|
| `core/config.py` | 随源码分发的默认值 |
| `core/config_local.py` | 机器专用模型路径、解释器与部署参数，可从 `.example.py` 模板创建 |
| `data/settings.json` | Web 设置的持久化值，默认权限 0600 |
| 服务管理器的环境文件 | `HF_TOKEN` 等凭据和进程环境 |

加载顺序为默认值 → `config_local.py` → `settings.json`。Web 设置只接受已定义字段和类型，原子保存；源码和机器配置不被 Web 改写。ASR/TTS 路径的显式环境变量在配置加载后生效。

| 环境变量 | 用途 |
|---|---|
| `BILIMIX_DATA_DIR` | 数据根目录，默认仓库 `data/` |
| `BILIMIX_SETTINGS_FILE` | Web 设置 JSON，默认数据目录下 `settings.json` |
| `BILIMIX_HOST` / `BILIMIX_PORT` | 监听地址/默认端口；启动命令端口优先 |
| `BILIMIX_ASR_PYTHON` | ASR/人物分析运行时 |
| `BILIMIX_WHISPERX_BIN` | WhisperX 命令 |
| `BILIMIX_TTS_PYTHON` / `BILIMIX_TTS_ROOT` | TTS 解释器/源码目录 |
| `HF_TOKEN` | 受限模型访问凭据 |
| `HF_HOME` / `HF_HUB_CACHE` / `XDG_CACHE_HOME` | 模型缓存位置 |

`config_local.py`、`settings.json`、凭据文件与客户端会话不随发布包分发。`doctor.py` 只检查能力，不下载模型或创建配音任务；凭据检查只输出是否配置。

任务调度由单个服务进程持有，HTTP 请求可使用多个线程。不要增加多个 WSGI worker，否则各进程将各自启动模型任务。通过 `services.web_app.create_app()` 构建应用时不会启动后台刷新；自定义启动器须在接收请求前显式调用 `services.bootstrap.start_runtime()`。

## systemd 示例

将下列路径替换成实际部署目录与解释器。环境文件由部署者创建并设为 0600。

```ini
[Unit]
Description=BiliMix
After=network.target

[Service]
WorkingDirectory=/srv/BiliMix
ExecStart=/srv/bilimix-env/bin/python services/web_app.py 5000
EnvironmentFile=/etc/bilimix/service.env
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 发布

在 Python 3.10+ 构建环境执行：

```text
python -m pip install ./sdk setuptools wheel
python -m pytest tests -q
python scripts/release.py --output dist
```

构建会检查共享字幕布局、生成 SDK wheel、Skill ZIP 和完整源码，写出版本与 SHA256 清单。成功后发布目录整体替换，仅保留当前发布内容；构建失败保留现有发布目录。

当前统一 Skill 是 `nows-ytb-dubbing`。只安装 Skill ZIP 时可直接运行其中的 `scripts/workflow.py setup --workdir <项目目录>`，使用随包轻量 SDK wheel 初始化隔离客户端环境，无须在另一台电脑上寻找 BiliMix 源码路径。随后按 SKILL.md 的单一入口完成 FULL、精翻和配音。setup 安装客户端工具；它不在服务端部署模型或重启服务。
