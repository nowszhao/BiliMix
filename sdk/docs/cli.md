# BiliMix 命令参考

`bmx` 与 `python -m bilimix_cli` 使用相同参数。所有命令支持 `--server URL`、`--pretty`、`--field PATH` 和 `--quiet`。

## 认证

```text
bmx --server <URL> auth status
bmx --server <URL> auth login --username <用户名> --password <密码>
bmx --server <URL> auth logout
```

`--server` 显式指定后保存到客户端配置；`BILIMIX_SERVER` 可指定进程默认地址。会话按服务器隔离，目录由 `BILIMIX_HOME` 指定。

## 精翻视频

```text
bmx audio upload source.mp4 --field local_path
bmx video analyze --server-path <媒体路径> --wait
bmx audio upload refined.ass --field local_path
bmx subtitle parse <ASS路径>
bmx task submit --type video --server-path <媒体路径> --subtitle-path <ASS路径> --dub-manifest dub_manifest.json --analysis-id <分析ID> --subtitle-mode bilingual --subtitle-font-size 40
```

`--dub-manifest` 读取本地 JSON；`--server-path`、`--subtitle-path` 使用上传返回的服务端路径。外部精翻必须同时有工作 ASS、manifest 与已完成的源分析，同一人物全片固定参考。

| 参数 | 用途 |
|---|---|
| `--subtitle-mode bilingual\|chinese_only\|none` | 显示语言 |
| `--subtitle-font-size <整数>` | 请求字号上限，实际受画面高度预算约束 |
| `--keep-bgm` | 保留原背景音 |
| `--wait` | 提交后等待任务结束 |

配套 Skill 包装脚本可保存上传、分析与提交状态，适合由 Agent 执行完整流程。

## 服务端自动处理

```text
bmx task submit --type audio --url https://example.com/episode.mp3 --wait
bmx task submit --type video --video-url https://www.youtube.com/watch?v=VIDEO_ID --wait
```

这类任务由服务器转录和翻译。Agent 精翻任务应使用上一节的外部输入。

## 任务

```text
bmx task list --limit 20
bmx task status <task_id>
bmx task result <task_id>
bmx task wait <task_id>
bmx task cancel <task_id>
bmx task retry <task_id>
bmx task redo <task_id>
bmx task delete <task_id>
bmx task reorder <task_id> --direction up
```

`retry` 继续失败步骤；`redo` 清理任务产物后重做；`delete` 删除任务及其文件。读取结果时，`segments` 是预览，完整 manifest、翻译和时间映射用于核对。

## 下载

```text
bmx audio download --task-id <task_id> -o dubbed.mp3
bmx video download --task-id <task_id> -o dubbed.mp4
bmx video download-subtitles --task-id <task_id> -o dubbed.ass
bmx file download <服务端路径> -o output.bin
```

`video_result.subtitle_url` / `subtitle_path` 指向配音时间轴 ASS，`subtitle_layout` 是显示布局汇总。

## 独立 TTS

```text
bmx tts synth --ref-audio reference.wav --text "完整中文话语。" --lang zh --wait -o speech.wav
bmx tts list
bmx tts status <job_id>
bmx tts result <job_id>
bmx tts download <job_id> -o speech.wav
bmx tts cancel <job_id>
bmx tts retry <job_id>
bmx tts delete <job_id>
```

`--ref-audio` 上传本地参考；`--ref-path` 使用上传后路径。`--text-file` 按行读取批量文本。`--lang` 支持 `auto`、`zh`、`en`、`ja`、`ko`。

## 设置与内容

```text
bmx config get
bmx config set sentence_cn_ratio=1.0
bmx episodes list
bmx subscriptions list
bmx favorites list
bmx podcast search <关键词>
bmx api
```

更多筛选和批量操作见各子命令 `--help`。

## 待确认人物

`completed` 是分析任务完成；`result.analysis.dubbing_ready` 才表示人物与参考可用于配音。未知词完整保留在 `unresolved_segments` 中。听原声及对应参考并取得用户明确选择后，提交 JSON 列表（如 `[{"word_ids":[42],"speaker_id":"SPEAKER_01"}]`）：

```text
bmx --server <服务URL> video confirm-speakers --analysis-id <分析ID> --resolutions decisions.json
bmx --server <服务URL> task result <分析ID> --field result.analysis
```

刷新本地 analysis 后重建 ASS 与 manifest，可复用既有翻译。确认前不得修改 manifest 强行认领未知词；人工确认不会成为克隆参考。

配音参考模式通过 `bmx task submit --tts-reference-mode speaker|segment` 选择，默认 `speaker`（同人物固定参考）。`segment` 使用每个完整配音句对应的原声，尽量保留语气和节奏；不合格片段回退到同人物固定参考。该选项随任务保存，重试和重做沿用。`bmx task result <task_id>` 的 `tts_reference_manifest.units` 记录每句的实际模式、原声范围和回退原因；服务端需在 `bmx api` 中声明 `tts_reference_modes` 包含 `segment`。
