# 通用 BiliMix 客户端命令

`python -m pip install <仓库>/sdk` 安装 CLI 和 Python SDK。`bmx` 与 `python -m bilimix_cli` 等价；后者不依赖 shell PATH。下面可把 `bmx` 替换为该模块调用。

服务地址优先级：`--server` > `BILIMIX_SERVER` > SDK 本地配置 > `http://localhost:5000`。配置目录可用 `BILIMIX_HOME` 指定，默认用户自己的 `.bilimix`。会话按服务器隔离；升级到 1.1 后旧的无归属 session 不会自动发送给服务器，需要按服务器登录一次。

```text
bmx --server <URL> api --field dubbing_protocol
bmx --server <URL> auth status
bmx --server <URL> auth login --username <用户名> --password <密码>
```

不要在共享说明、命令历史示例或日志中放真实凭据。auth status 的 `server` 表示 CLI 实际连接地址，供工作流绑定状态使用。

## 上传、预分析和提交

```text
bmx --server <URL> audio upload <MP4或ASS> --field local_path
bmx --server <URL> video analyze --server-path <上传返回路径>
bmx --server <URL> task wait <分析ID>
bmx --server <URL> task result <分析ID>
```

始终使用上传返回的 `local_path`，不要拼接服务端磁盘路径。人物分析位于 `result.analysis`。完整本地工作流优先使用 bmx_video_task.py，能在等待前保存分析 ID。

```text
bmx --server <URL> subtitle parse <工作ASS的服务端路径>
bmx --server <URL> task submit --type video --server-path <MP4路径> --subtitle-path <工作ASS路径> --dub-manifest <本地manifest.json> --analysis-id <分析ID> --ref-select-mode speaker_global --subtitle-mode bilingual --subtitle-font-size 40
```

工作 ASS 每行用 `EN || ZH` 保留导入契约。`count == bilingual_count > 0` 才通过。无效文件明确失败，不回退服务端翻译；字幕不代替原声人物分析。

显示分页与 TTS 单位分开。默认中文最多 2 行、英文最多 2 行、合计最多 4 行，估算文字高度不超过画面 25%；字号过大时适配高度。主音频不会因分页被切成小段。

## 状态和下载

```text
bmx --server <URL> task status <task_id>
bmx --server <URL> task wait <task_id>
bmx --server <URL> task result <task_id>
bmx --server <URL> video download --task-id <task_id> -o dubbed.mp4
bmx --server <URL> video download-srt --task-id <task_id> -o dubbed.ass
```

`task result` 的 segments 是预览，完整 units/翻译/时间映射及计数用于核对。工作 ASS 保留源时间，下载的最终 ASS 对应配音时间，不能互换。

服务端任务提交不去重。包装脚本用本地回执复用相同请求；若提交后响应丢失，先查 task list，再决定是否新建。只对读取类操作作有限重试，不盲目重放提交。
