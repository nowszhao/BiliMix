# 精翻配音协议

精翻由 Code Agent 执行；BiliMix 服务端完成源人物分析、固定参考提取、完整话语 TTS、混音和视频适配。

## 输入

外部精翻配音必须提供：同一源媒体、工作 ASS、`dub_manifest`、已完成的 `analysis_id`。人物分析返回 `result.analysis`，协议标识 `dubbing_protocol: 1`。

```text
bmx audio upload source.mp4 --field local_path
bmx video analyze --server-path <媒体路径> --wait
bmx task submit --type video --server-path <媒体路径> --subtitle-path <工作ASS路径> --dub-manifest dub_manifest.json --analysis-id <分析ID>
```

工作 ASS 每行文本格式为 `EN || ZH`，Name 保存源人物身份；它用于导入与审校。最终显示 ASS 使用配音后的时间轴，不能作为新的工作 ASS 导入。

## Manifest

顶层字段：`schema_version: 1`、`analysis_id`、`source_media_sha256`、非空 `units`。

| Unit 字段 | 约束 |
|---|---|
| `unit_id` | 非空且唯一 |
| `speaker_id` | 已解析的源人物，与所有源词归属相同 |
| `source_word_ids` | 按原顺序完整覆盖源词，各一次，不跨人物 |
| `text_en` | 校正后的英文话语 |
| `text_zh` | 完整自然中文，最多 1500 字符；超长时按自然从句拆分 |
| `boundary_after` | `continuation` / `sentence_end` / `turn_end` |
| `captions` | 可选显示分段；中文连接必须精确等于 `text_zh` |

同人相邻源行可重组，不能把 A→B→A 改排为 A→A→B。显示分段不会增加 TTS 请求或句内停顿。

缺文件、无效 ASS、媒体哈希不符、未完成分析、未知人物、源词漏失/重复/重排均返回错误。

## 人物与参考

WhisperX 提供词级定位；pyannote 提供人物分段和重叠发言区间。原声声纹进一步复核词与人物归属，只有相似度和候选差值达到门槛才修正标签。未通过复核的词保留原生标签并标记 `speaker_verified:false`，不进入参考池。

每人全片共用合格连续原声参考。参考须包含至少 2 秒同人语音，最长 10 秒；排除另一人重叠、未验证和长停顿区间。不足参考时停止合成，不借其他人的声音。Demucs 负责分离人声与背景；它不负责区分人物。

缓存绑定参考内容、文本、语种、模型与参数。分析与合成均在恢复任务时检查缓存内容。声纹诊断不等于人工听审，交付时应说明低置信人物片段。

## 音频与画面

完整中文自然播放后建立主时间轴。保留一份连续音频母带，按实际音频累计端点计算视频帧数。源间隙不足时补停帧；分块只编码视频，主音频在最终封装时统一加入。每块与最终导出检查实际帧数，缺音频或缺帧会失败。

显示字幕按实际画面宽高与字号显式折行、分页。默认中文最多两行、英文最多两行、合计最多四行，估算文字高度不超过画面 25%。页间连续、整体起止保持不变，显示时间按文本长度分配，不承诺逐词口型同步。

结果包含 `video_result.video_url`、`video_result.subtitle_url`、对应路径及 `video_result.subtitle_layout` 汇总。ASS 附带 `.layout.json`。

## 任务恢复

`analysis_state.json` 绑定服务地址和媒体哈希；分析 ID 在等待前保存。`dubbing_state.json` 记录配音提交结果，相同请求复用 task_id。`--new-task` 显式创建新任务；`--fresh-upload` 重新上传和分析。

`submission_pending.json` 表示提交响应尚未确认。先检查服务器任务列表，再决定是否重发；服务端提交不提供原子幂等保证。

失败分析可 retry；完成分析需显式 `POST /api/task/<id>/retry`，JSON 为 `{"refresh_analysis":true}`。已提交配音保存分析快照，新的精翻任务应使用当前分析结果。

## 分析完成与可配音状态

`analysis.dubbing_ready` 为 false 时保留完整词流和 `unresolved_segments`，不执行人物克隆。原生时间段没有覆盖某个词不会再阻止后续声纹复核；只对同人物、距词边缘不超过 50 ms 的小偏差桥接。跨人空隙和接近等量的多人覆盖必须由片段本身的声学证据解析，证据不足保留待确认。完整话语的声纹结果不能覆盖未知或其他人物的短插话。

进度分别显示分离、转录、原生人物分析、词绑定、声纹复核窗口计数和汇总。有效的原生人物缓存保持可复用，未完成声纹复核的分析快照不能充当完成缓存。

用户听原声并明确确认后，可用 `bmx video confirm-speakers --analysis-id ID --resolutions decisions.json` 提交 JSON 列表，例如 `[{"word_ids":[42],"speaker_id":"SPEAKER_01"}]`。对应 API 为 `POST /api/dubbing/ID/confirm-speakers`，请求 `{"resolutions":[...]}`。仅允许确认未知词，目标人物必须有合格参考；不能修改既有人物、原文或时间。响应包含最新 `analysis`。人工确认持久化且标记为 `human_confirmed`，不会伪装成声纹通过或进入参考池。

刷新本地 analysis 后，精翻脚本按原词 ID 重新读取人物；ASS 和 manifest 可复用已有翻译重建。人物未确认或缺少同人参考时，客户端和服务端均拒绝配音。
