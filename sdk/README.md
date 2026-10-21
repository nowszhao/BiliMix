# BiliMix SDK

Python 3.10+ 的命令行与 HTTP 客户端。CLI 默认将 JSON 写入 stdout，进度与诊断写入 stderr。

## 安装与连接

```text
python -m pip install /path/to/BiliMix/sdk
python -m bilimix_cli --server <服务URL> auth status
bmx --server <服务URL> auth login --username <用户名> --password <密码>
```

服务地址优先级：`--server` → `BILIMIX_SERVER` → 保存的配置 → `http://localhost:5000`。配置目录由 `BILIMIX_HOME` 指定，默认 `~/.bilimix`；登录会话按服务器隔离。

客户端无需模型、Torch 或 Hugging Face token。环境与安装见 [安装说明](../docs/installation.md)。

## 精翻配音

```text
bmx --server <服务URL> audio upload source.mp4 --field local_path
bmx --server <服务URL> video analyze --server-path <媒体路径> --wait
bmx --server <服务URL> audio upload refined.ass --field local_path
bmx --server <服务URL> task submit --type video --server-path <媒体路径> --subtitle-path <ASS路径> --dub-manifest dub_manifest.json --analysis-id <分析ID>
bmx --server <服务URL> task wait <task_id>
bmx --server <服务URL> video download --task-id <task_id> -o dubbed.mp4
bmx --server <服务URL> video download-subtitles --task-id <task_id> -o dubbed.ass
```

工作 ASS 和配音 manifest 必须同时提供，人物分析必须对应同一媒体。完整话语只合成一次，显示字幕独立分页。结果中的 `video_result.subtitle_url` 是最终 ASS，`subtitle_layout` 提供布局汇总。

完整流程见 [精翻配音协议](../docs/audio-first-dubbing.md)。

## 常用命令

| 命令 | 用途 |
|---|---|
| `task list / status / result / wait` | 任务状态与结果 |
| `task cancel / retry / redo / delete` | 任务操作 |
| `audio upload / download` | 上传与音频下载 |
| `subtitle parse <路径>` | 校验工作 ASS |
| `config get / set` | 服务设置 |
| `tts synth --ref-audio <WAV> --text <文本> --wait` | 独立声音克隆 |
| `episodes / subscriptions / favorites` | 播客内容管理 |
| `api` | 服务能力目录 |

`--pretty` 美化 JSON；`--field <字段路径>` 提取嵌套字段；`--quiet` 隐藏进度。完整参数以 `bmx <命令> --help` 和 [命令参考](docs/cli.md) 为准。

## Python 客户端

```python
from bilimix_cli import BiliMixClient

client = BiliMixClient("https://your-server.example")
capabilities = client.get("/api")
tasks = client.get("/api/tasks")
```

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | API/任务错误 |
| 2 | 未认证 |
| 3 | 超时 |
| 4 | 网络错误 |
| 5 | 依赖错误 |

## 待确认人物

`completed` 是分析任务完成；`result.analysis.dubbing_ready` 才表示人物与参考可用于配音。未知词完整保留在 `unresolved_segments` 中。听原声及对应参考并取得用户明确选择后，提交 JSON 列表（如 `[{"word_ids":[42],"speaker_id":"SPEAKER_01"}]`）：

```text
bmx --server <服务URL> video confirm-speakers --analysis-id <分析ID> --resolutions decisions.json
bmx --server <服务URL> task result <分析ID> --field result.analysis
```

刷新本地 analysis 后重建 ASS 与 manifest，可复用既有翻译。确认前不得修改 manifest 强行认领未知词；人工确认不会成为克隆参考。
