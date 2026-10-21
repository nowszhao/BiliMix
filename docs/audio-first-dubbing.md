# 精翻配音：音频优先协议 v1

精翻和同人语义重组必须保留。人物身份来自原声分析，完整中文话语是 TTS 单位，显示字幕不决定声音断点。中文自然播放后生成主时间轴，原画面按实际音频时长变速；没有源间隙但音频需要停顿时，用相邻画面停帧覆盖该间隙。

精翻和中文话语重组由本地 skill 执行。服务端接收 manifest 后跳过翻译，直接按已绑定人物合成中文音频；外部精翻文件缺失或无法解析时明确失败，不回退到服务端翻译。

## 工作流

1. 上传 MP4：`bmx audio upload source.mp4 --field local_path`。
2. 人物预分析：`bmx video analyze --server-path <返回路径> --wait`。异步任务使用已有 task status/wait/result 协议，结果位于 `result.analysis`。也可 POST `/api/dubbing/analyze`，JSON 含 `local_path` 或 `video_url`。
3. 将分析导入 `nows-ytb-bilingualex/scripts/import_analysis.py`，运行带 speaker/turn 约束的 FULL 和精翻。YouTube 原字幕及简介继续用作内容/术语核对。
4. 运行 `build_dub_manifest.py prepare`，由翻译模型重组完整中文话语，写 `dub_units_plan.json`；再 `apply` 生成 manifest。
5. 提交：`bmx task submit --type video --server-path <同一媒体路径> --subtitle-path <精翻工作ASS> --dub-manifest dub_manifest.json --analysis-id <预分析task_id> --ref-select-mode speaker_global`。
6. 用 `bmx task result`、`bmx video download` 和 `bmx video download-srt` 获取实际配音结果。

包装脚本支持 `--analyze-only`，自动保存 `analysis.json/analysis_state.json`，复用上传文件。`bmx api --field dubbing_protocol` 必须为 `1`。旧版服务/CLI不接受新参数时应升级，不能静默退回无人物约束的配音。

## 数据契约

manifest 包含 `schema_version:1`、`analysis_id`、`source_media_sha256` 和 `units`。每个 unit 必须有唯一 `unit_id`、`speaker_id`、完整 `text_zh` 和有序 `source_word_ids`。所有词 ID 必须与服务器分析完全同序覆盖各一次；不得把 A→B→A 改成 A→A→B，也不得跨人合成一个单声线请求。

`captions` 可把一个 unit 拆成多条显示文字，`text_zh` 连接必须等于完整话语。显示时间目前按文本长度在实际音频区间内分配，是显示估算，不承诺逐词同步；不会增加 TTS 请求或间隔。工作 ASS 保留源定位，最终 ASS 使用输出音频时间轴。

原生人物分段后，再以声纹嵌入复核完整话语和内部换人片段。只有达到相似度与候选差值门槛时才修正标签；有独立声学证据的插话保持分开，未通过复核的词保留原生标签并标明 `speaker_verified:false`。人物参考从全片同人连续语音区间选择，排除未通过声纹复核的词及另一人重叠区间。短答共用同人参考。模型身份仍需对实际素材抽听验收；人物不明、缺少至少 2 秒合格同人参考、词级定位无法覆盖时，任务明确报错，禁止借其他人声音补齐。

缓存绑定参考音频内容、文本、语种、模型/配置/权重指纹和生成参数。首次、恢复、重试都重新校验内容地址，不能仅因旧整数索引存在便复用。任务持久化保存 manifest、人物分析、参考清单和源/输出时间映射。

## 画面帧数与长片导出

帧数按音频累计端点取整，分块边界共用全局帧数。每段帧率转换保留末帧，并按目标帧数补足停帧；小于一帧的源间隙至少选取一个源帧。拼接后按输出帧序号重建时间戳，避免 29.97/23.976 等分数帧率的 CFR 编码重复舍弃边界帧。每块及最终编码均检查实际 `nb_frames`，少帧直接失败。主音频保持连续，最后一次性封装，不随视频块切开编码。

回归包含真实 FFmpeg 的整片、分块、25/29.97/23.976 fps、不规则极短间隙和新增长停顿。开发机或测试环境需要 FFmpeg 与 FFprobe；没有时相应编码回归会明确跳过。

## 模型与运行环境

- WhisperX ASR/词级定位与原生 pyannote 人物识别各跑一遍，保留原生重叠发言区间。`workers/analyze_speakers.py` 在模型环境中执行。
- `workers/verify_speakers.py` 用原声实际声纹复核，策略在 `pipeline/speaker_verification.py`。`speaker_verification_summary` 提供复核词数与修正词数，不等同于人工确认率。声学证据、原生分段、最终分析分别缓存，代码/模型/音频或文件内容改变后失效。词级边界仅容许 20 毫秒内唯一人物的时钟误差。
- 失败的分析可调用通用 retry；已完成的分析需要显式 POST `/api/task/<id>/retry`，JSON 为 `{"refresh_analysis":true}` 才能刷新。刷新复用内容校验有效的模型证据，不进入翻译或 TTS。已提交配音保存其分析快照，重新精翻前应以刷新后的词级人物为准。
- 默认人物模型 `pyannote/speaker-diarization-community-1`。完整本地缓存存在时离线加载；否则需要模型访问权限和 HF_TOKEN。日志不得输出 token。
- 服务、WhisperX 和 TTS 必须使用与依赖兼容的 Python 环境；部署时显式指定解释器和端口，避免 base Python 3.13 与旧 pydub 的 audioop 不兼容。
- 先跑 `python -m pytest -q`。测试使用隔离数据库/文件目录，不触发真实模型。上线验证另跑真实视频的分析、合成、混音及视频导出。

## 兼容和错误

外部旧 ASS 不再绕过人物分析。若一行覆盖不同人物，不能只按最长重叠给整行贴身份；需要通过新精翻 manifest 重组后提交。ASS Name 与分析冲突时拒绝任务，不按字幕文件中的名字强行覆盖声纹。

缺 TTS 音频会失败而不是漏句交付。主音频不为原时间窗口压速或截尾。真实重叠对话和身份不确定素材应先校正分析/分句，不能声称自动识别百分之百准确。
