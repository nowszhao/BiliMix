# 连接、人物确认与恢复

## Connection

需要 Python 3.10+。Skill 内置当前轻量 SDK wheel，`workflow.py setup --workdir <目录>` 将 SDK、yt-dlp 和 FFmpeg 工具安装到该项目 `.tools`，不修改系统或 Agent 的 Python。已有环境可跳过 setup。

使用 `python` 时应选定同一解释器，Windows 可用 `py -3`。`.tools` 是本机依赖，换电脑后重新 setup，不把它作为 Skill 内容分发。

服务器优先采用首次 run 的 --server；未指定时遵循 SDK 的 BILIMIX_SERVER、已保存地址及 localhost 默认值。成功连接后绑定项目，不能把另一服务的上传路径、会话或任务 ID 混用。支持显式 BILIMIX_CLI、BILIMIX_YTDLP、BILIMIX_FFMPEG 指定可执行文件，不填特定电脑路径。

需要认证时，使用项目 `.tools/bin/python`（Windows `.tools/Scripts/python.exe`）调用 `-m bilimix_cli --server <URL> auth login --username <用户名> --password <密码>`；已有环境则用其解释器。只使用用户明确提供的账户，避免把实际密码写进共享文件、Skill、项目 JSON 或回复。SDK 会话保存在用户自己的配置目录，按服务器隔离。

首次 run 可指定：--quality 720p/best、--font-size 40、--keep-bgm、--width/--height，以及用户允许的 --cookies-browser 或 --cookies-file。这些选项固定在该项目中，之后用不带重复配置的 run 恢复。公开视频默认不使用浏览器 cookies；登录下载只在需要且用户允许时使用。

## Speakers

分析任务 completed 表示分析结束；analysis.dubbing_ready 才表示满足克隆前提。speaker=null 是未知人物，speaker_verified=false 可能是已沿用原生标签但没有通过声纹复核，两者不同。

程序允许未知片段独立完成 FULL 和精翻；开始合成前集中处理人物问题。打开 `output/speaker_review.html`，用户可以播放原声上下文、比较每人的合格参考，选择人物并下载 decisions.json。该页面只操作本地视频，不连接服务器。

用户明确选择后：

```text
python <skill>/scripts/workflow.py confirm-speakers --workdir <目录> --decisions <用户下载的decisions.json>
python <skill>/scripts/workflow.py run --workdir <目录>
```

Agent 不得自行按语义或候选排序填写选择。确认只修改原本未知的词，目标人物须有合格参考；原词、顺序与时间不变，人工确认不等于声纹通过，也不进入参考池。外部已确认时用 `refresh-analysis --workdir <目录>` 获取最新分析。

确认后翻译自动按原词重新绑定人物，不必重译全部。原先一条未知话语确认后涉及多人时，按报错拆分对应 language/refined.json 和 language/units.json 的受影响话语，保持稳定源 ID、原序与完整覆盖。

reference_missing 不能靠选人物解决。列出缺少合格参考的具体人物，交由原声质量或模型分析维护处理；不自动重跑大型分析，不借其他人的声音。

## Recovery

`status --workdir <目录>` 只读本地项目，不联网。run 每次推进到需要 Agent、需要用户或等待服务端的位置，返回 JSON；等待时适当间隔重跑，不紧密轮询。

- 语言错误：修复当前 input 对应的 output 后继续。输入 input_id 要保持一致。
- 服务端失败：先报告 task_id、step/message；有明确重试依据后执行 `retry --kind analysis|dubbing --workdir <目录>`，继续原任务。不会偷偷创建新任务。
- 提交响应丢失：pending_submission 在发送前已保存，包含提交前已有任务 ID。先用 SDK task list 核对视频标题、媒体路径与任务类型，仅在新增任务中存在唯一匹配时执行 `recover --workdir <目录> --task-id <查到的ID>`。找不到或有多个匹配时保持待核查，不能盲目重发。任务显示视频标题，服务端当前不提供提交原子幂等保证。
- 同一项目不能并发执行，两次运行会由本地进程锁互斥。进程崩溃后系统自动释放锁。
- 改成另一视频、另一服务或另一次配音输出，使用新的工作目录；不复用旧媒体路径和回执。

工作目录只保存当前快照和继续任务所需数据，不写操作流水或历史备份。下载完整后才替换交付文件，中断下载不算完成。
