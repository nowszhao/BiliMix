# BiliMix 维护约定

- 架构见 `docs/architecture.md`。HTTP 适配在 services，任务编排在 application，处理算法在 pipeline，基础状态与执行队列在 core。
- core/pipeline 不导入 services/application；application 不导入 Flask/services。不要用延迟导入绕过方向限制。
- 所有重型处理通过共享 task_queue；不在路由新增私有锁、直接推理或每请求后台执行器。取消 active 后必须等回调退出，才释放执行权。
- 视频任务身份与内部音频步骤分开；完整中文话语与显示字幕分页分开；segment 模式必须保留实际参考及回退报告。
- 任务接收、恢复和重做必须保留媒体哈希、来源词 ID、ASS/manifest 绑定及参考模式。持久化使用原子写入，不吞掉损坏检查点错误后继续配音。
- 唯一发布的 Skill 是 skills/nows-ytb-dubbing；安装副本通过 scripts/install_skills.py 更新。不要恢复平行的旧流程。
- 修改布局后运行 scripts/sync_skill_layout.py，发布前用 --check 校验。
- 使用临时数据目录验证，不访问生产任务或模型服务。回归命令：python -m pytest tests -q；发布检查：python scripts/release.py --output <临时目录>。
- 修改模块职责或执行协议时同步架构文档，并保持入口可从任意工作目录使用。
