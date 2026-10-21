---
name: nows-ytb-dubbing
description: 为 YouTube 或本地英文视频制作精翻双语字幕及中文配音。当前 Code Agent 完成 FULL 断句和中文精翻，BiliMix 完成人物声纹分析、完整话语配音与画面适配；支持只生成字幕。
---

# 视频精翻与中文配音

主流程：**视频 → 人物分析 → FULL 完整断句 → 中文精翻 → 服务端配音 → 交付**。

翻译使用当前 Agent 的模型；BiliMix 负责人物分析、固定参考、TTS、混音和画面变速。需要能读写文件、执行 Python 3.10+ 命令的 Agent。客户端不需要模型或 Hugging Face token。

## 开始

每个视频一个工作目录。`<skill>` 是本次实际安装目录，`python` 是本次选定解释器。先选用现有可用环境；缺 SDK/下载工具时执行一次 `setup`，依赖只安装到工作目录 `.tools`：

```text
python <skill>/scripts/workflow.py setup --workdir <工作目录>
python <skill>/scripts/workflow.py run --input <YouTube网址或本地视频> --server <BiliMix地址> --workdir <工作目录>
```

已有 SDK 和工具可直接 `run`。服务器来自用户配置、环境变量或 SDK 配置，不猜 IP。需要登录时阅读 [连接与恢复](references/operations.md#connection)。默认 720p、关闭背景混音、字幕字号 40，可在首次 `run` 时覆盖。

分析和配音任务名称均使用 YouTube 原始视频标题；本地视频使用文件名（去扩展名）。内部项目 ID 不放进显示名称。

## 按返回结果推进

后续只需重复：`python <skill>/scripts/workflow.py run --workdir <工作目录>`。

| 返回 stage | 当前 Agent 的下一步 |
|---|---|
| `full` | 阅读 [FULL 规则](references/language.md#full) 和返回的 input，写入指定 output |
| `translate` | 阅读 [精翻规则](references/language.md#translate)，直接产出完整中文配音话语 |
| `analysis_waiting` / `dubbing_waiting` | 告知当前阶段，适当间隔后继续同一项目；不要新建任务 |
| `speaker_review` | 打开返回的本地听审页，请用户听原声后明确确认；见[人物确认](references/operations.md#speakers) |
| `reference_missing` | 报告缺少合格同人参考；停止配音，不能借其他声线 |
| `submission_unknown` / `*_failed` / `error` | 按[恢复说明](references/operations.md#recovery)处理原任务 |
| `completed` | 交付返回的 artifacts |

脚本负责下载、上传、校验、时间继承、ASS/manifest 生成、任务记录与结果下载。不要手工拼这些步骤，也不直接调用服务端翻译。脚本本身不会调用 Agent 模型；`agent_write` 需要你完成语言工作后再继续。

## 必须保留的质量约束

- FULL 全量处理，保留源词与顺序，只调整标点及话语边界；ASR 纠错在精翻中完成。
- 同一人物的相邻内容可组成完整话语；真实换人和待确认片段必须分开。分块大小不决定断句。
- 精翻直接写完整、自然、口语化中文，不为原时间窗删内容或加速。保持全片术语一致。
- 不按语义、前后人物或候选排序猜声线；人工确认与声纹复核分开记录。
- 一个完整中文话语一次 TTS；显示字幕独立分页，每种语言最多两行、合计四行、高度预算 25%。
- 音频优先，视频适配音频。工作 ASS 用于导入，成片须使用服务端返回的最终字幕。

## 交付与其他模式

正常交付 `output/dubbed.mp4`、`dubbed.mp3`、`dubbed.ass`；工作 ASS 和 manifest 保存在同目录供审校。说明未确认或未通过声纹复核的片段，不把人物绑定描述成百分之百准确。

只生成字幕时，在新项目首次运行中指定 `--mode subtitles`，按[纯字幕说明](references/subtitles.md)执行；该模式不连接 BiliMix。状态查询 `workflow.py status --workdir <工作目录>` 只读取本地项目。

`project.json` 管进度，`analysis.json` 保存源分析，`language/` 保存两步语言结果，`output/` 保存交付。来源 ID 和时间由程序维护，不删改词来绕过校验，不在 Skill 或项目中保存密码、Token 或会话。
