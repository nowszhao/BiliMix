---
name: nows-ytb-bilingualex
description: Download a YouTube video and produce refined English–Chinese subtitles and complete Chinese dubbing units. Use for YouTube bilingual precision translation and natural spoken Chinese, including the BiliMix audio-first dubbing workflow.
---

# YouTube 精翻与完整话语

保留精翻和语义重组：同一人的多个字幕碎片可合并成完整话语。中文自然语速优先，画面随后变速适配。人物身份、配音话语、显示字幕分别保存；字幕切换不得触发额外 TTS 或句内停顿。

翻译与中文话语重组由本地当前 Agent 的模型执行；BiliMix 服务端负责原声分析、声线绑定、配音与音画合成。提交 manifest 后不再调用服务端翻译覆盖精翻。

## 可移植环境

需要能读写文件并执行命令的 Code Agent，以及 Python 3.10+。以下 `python` 代表选定解释器；macOS/Linux 可能使用 `python`，Windows 可用 `py -3`。本 skill 不调用特定 Agent API，语义整理与翻译由当前 Agent 的模型写文件完成。

默认下载 720p。公开视频先不带 cookies；仅在需要且用户允许时指定 `--cookies-browser <浏览器[:配置]>` 或 `--cookies-file <文件>`，不假设已安装 Chrome。

```text
python <本skill>/scripts/check_env.py --env-out <工作目录>/yt_env.json
python <本skill>/scripts/download.py --url <URL> --quality 720p --output <工作目录> --env-out <工作目录>/yt_env.json
```

缺依赖时可选择 `check_env.py --install-missing`，安装到独立工具 venv；不修改 Agent 自带环境。`yt_env.json` 是本机工具发现结果，换电脑后重新生成，不随 skill 分发。已有本地 MP4 可直接进入人物分析，不需要浏览器或下载器。

视频 metadata 保存为 video_info.json，结合简介、章节和原始 ASR 作术语纠错；不用 YouTube 机器翻译。脚本路径以本次安装目录为准，工作产物放独立工作目录。

## 用于 BiliMix 多人物配音：先拿人物分析

按实际安装路径调用配套 `nows-ytb-bmxtask/scripts/bmx_video_task.py --video <MP4> --workdir <工作目录> --server <URL> --analyze-only`。两份 skill 不相邻时使用各自实际路径，不猜用户目录。它上传一次并等待异步人物分析，写入视频目录 `analysis.json` 和 `analysis_state.json`；后续提交会复用上传路径。

分析与配音任务都使用视频原始标题。保留下载生成的 `video_info.json` 和 `manifest.json` 供提交脚本读取；视频转移到其他目录且没有元数据时，显式传入 `--title <原始标题>`。纯本地视频无标题元数据时使用文件名（去扩展名）。

```bash
python <skill>/scripts/import_analysis.py <工作目录>/analysis.json --workdir <工作目录>
python <skill>/scripts/refine_srt.py prepare-full <工作目录>/transcript_official.srt <工作目录>/raw_aligned.srt --workdir <工作目录>
```

原始 YouTube ASR 保留供内容核对；配音流程使用后端带词级定位和全片人物身份的 canonical 源行。`>>` 只表示可能换人，不能当作 A/B 的声纹识别。人物未解析或参考不足要报告具体原因，不能借另一人的声线。

检查 `speaker_verification_summary` 和词级 `speaker_verified`。未通过声纹复核的词沿用原生标签，不能把它们描述为人工确认或百分之百正确；交付时列出涉及的源时间和话语，尤其是短答、插话、片尾多人致谢。不得凭问题/回答的语义改写人物标签。这些词不进入克隆参考池。

## FULL 语义整理：必须保留

阅读 `references/refine_prompt_full.md`。模型逐块写 `refine_parts/refined_NN.txt`，格式 `S<seq>	<完整英文话语>`，每块独立连续编号。

- 源词与顺序完全保留（uh/um/重复词也保留），只改标点；真正 ASR 纠错留给翻译。
- 以完整话语为先，5–16 词只是建议；不要为凑词数截断未完成表达。
- 同人相邻源行可以合并；遵守分块头的 speaker/turn 信息，禁止跨人物合成一段。自然短答保持独立，不与另一人合并。
- 不输出时间戳；脚本使用真实词级定位，保存人物与来源所有权。

```bash
python <skill>/scripts/check_refined.py --workdir <工作目录> --mode full
python <skill>/scripts/refine_srt.py apply --workdir <工作目录>
```

缺块、词序漂移、跨人合句均不可继续。修复对应模型输出后重检，不机械补词掩盖漂移。

## 精翻

```bash
python <skill>/scripts/split_translation.py <工作目录>/transcript_refined.srt --lines-per-part 70 --out <工作目录>
```

阅读 `references/translation_prompt.md`，按块写 `parts/trans_NN.txt`：`idx	start	end	修正英文	中文`。保留 idx/工作时间戳/行数，先逐行建立可靠来源；翻译自然口语，术语统一，删除舞台指示和章节泄漏。不为了原时间窗删内容或强行压语速。

```bash
python <skill>/scripts/verify_translation.py --workdir <工作目录>
python <skill>/scripts/assemble_final.py --workdir <工作目录>
```

ASS Name 继承人物。这个 ASS 是精翻工作产物；最终交付 ASS 由 BiliMix 按实际配音时间重建。`reanchor_timestamps.py` 只能修工作翻译的源时间，不能改最终交付时间轴。

## 中文完整话语组装：配音前必做

```bash
python <skill>/scripts/build_dub_manifest.py prepare --workdir <工作目录>
```

通读生成的 `dub_unit_input.json`，模型写 `dub_units_plan.json`：

```json
{"units":[{"source_indices":[1,2],"text_en":"corrected full English","text_zh":"精翻后完整自然的一句话。","boundary_after":"sentence_end","captions":[{"text_en":"...","text_zh":"精翻后完整自然的一句话。"}]}]}
```

- source_indices 按原序完整覆盖全部输入各一次，只合并同人相邻行；不得把 A→B→A 重排。
- 中文可按语义自然重写，不能只把残句用空格拼接；不漏语义、不增加原文没有的信息。
- 一条 unit 是一次 TTS。显示需要多条 captions 时，中文连接必须恰好等于 text_zh；只影响显示，不产生新声音或额外停顿。
- captions 仅控制显示。优先按自然从句写较短的中英文页；可先以中文约 15–28 字、英文约 8–20 词作为草稿参考，最终由实际分辨率/字号校验。服务端自动分页兜底：每种语言最多 2 行、总共最多 4 行，文字高度预算 25%；不为字幕分页改动完整 TTS。
- boundary_after 可用 continuation/sentence_end/turn_end；以真实话语关系判断。
- 超出引擎长度时在自然从句边界拆分，不能无限长整段合成。

```bash
python <skill>/scripts/build_dub_manifest.py apply --workdir <工作目录>
```

输出 `dub_manifest.json`，包含源媒体哈希、analysis ID、每句人物和源词 ID。由提交脚本送给 BiliMix，不手填或伪造人物/参考质量。

## 只生成字幕

不做配音时仍可从 YouTube 官方 ASR 经 `aggregate_srt.py` → FULL → 精翻 → assemble。工作 ASS 是 `EN || ZH` 导入/审校格式；另生成供观看的分页 ASS：

```text
python <本skill>/scripts/render_subtitles.py --input <工作ASS> --output <观看用ASS> --width <视频宽> --height <视频高> --font-size 40
```

使用真实分辨率；保留工作 ASS 不覆盖。渲染器不需要 BiliMix 或模型依赖；若源时间重叠会明确报错，先整理 FULL/时间轴，不能让多条字幕堆叠遮屏。缺少音频人物分析的路径只交字幕，不宣称多人物声线已验证。滚动 ASR 的时间单调/格式校验仍需执行。

## 交付

返回 MP4、精翻工作 ASS、dub_manifest。配音完成后返回服务端最终视频和配音时间轴 ASS。明确合成/参考不足等错误，不把缺失台词当成功。

只交字幕时，优先交付观看用分页 ASS；工作 ASS 标明导入/审校用途，保留源定位。

## 人物待确认的处理

分析任务 `completed` 表示词流分析已完成，配音还须满足 `dubbing_ready=true`。检查 `unresolved_segments`（源时间、原文、word_ids、候选人物）和 `missing_reference_speakers`。未知词仍完整保留，可作为独立片段继续 FULL 和精翻；不得把它并入已知人物话语。`speaker_verified=false` 本身不等于未知，人物是否未知看 `speaker=null`。

服务端先用该片段自身的声纹和有边界限制的上下文复核；证据不足时由用户听原声和人物参考后明确确认。当前 Agent 不得按语义、前后人物或候选排序代替用户做决定。用户明确确认后写 JSON 列表，例如 `[{"word_ids":[42],"speaker_id":"SPEAKER_01"}]`，再执行：

```text
bmx video confirm-speakers --analysis-id <分析任务ID> --resolutions <用户确认.json>
```

该接口只接受待确认词和有合格参考的现有人物，保留所有原词、时间和词 ID；人工确认标记与声纹通过标记分开。之后重新执行原 `bmx_video_task.py --analyze-only` 命令，刷新本地 `analysis.json`，不会创建新的已绑定分析任务。再运行 `assemble_final.py` 和 `build_dub_manifest.py prepare/apply`，按最新词归属生成 ASS Name 和 manifest；已有精翻可复用。如果原来的一行确认后涉及多人，只拆分该行及其中文内容。不能修改 manifest 来绕过服务端人物校验。缺少合格同人参考时停止配音并报告，不借用其他人物声线。
