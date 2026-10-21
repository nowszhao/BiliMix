# 只生成精翻字幕

在新项目首次运行时指定字幕模式：

```text
python <skill>/scripts/workflow.py run --input <YouTube网址或本地视频> --mode subtitles --workdir <目录>
```

YouTube 视频会读取英文字幕；本地视频需同时提供 `--source-subtitles <English.srt>`。缺下载/字幕转换工具时先执行 setup。默认采用与视频相符的实际分辨率，可在首次运行用 --width/--height 明确指定。

之后按主 Skill 的 FULL → 精翻两步处理，重复同一 run 入口即可。该模式不连接 BiliMix，不请求声纹或 TTS；字幕源时间按字幕行内插值，只用于显示，不作为声纹识别或声音克隆依据。

滚动字幕仅在时间重叠且文字前后重复时去除重复前缀；无法可靠解释的重叠明确报错，不机械延时覆盖。模型保留真实台词，不能按重复词猜人物。

交付 `output/bilingual.ass`（观看用分页字幕），附 `output/refined.ass`（源时间的工作字幕）及布局汇总。分页每种语言最多两行、合计四行、高度预算 25%。字幕模式不生成配音 manifest，不声称人物声线已验证。
