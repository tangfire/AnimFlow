# 演示视频

`scripts/record_video.py` 录制真实 Chromium 页面操作，并导出 H.264 MP4。
中文字幕为单独的说明层，不改业务状态；不配音、不剪掉等待或失败过程。
视频只覆盖本机 MVP 的演示，不应描述为生产系统或本人独立手写全部代码。

## 重录

需要本机 `.env` 配置和可用 DeepSeek 额度。脚本显式调用两次真实模型；遇到可重试服务错误还可能增加请求。
其他恢复/资源场景明确切 MOCK。普通项目启动/pytest 不需要这些可选依赖。

```powershell
cd D:\AnimFlow
.venv\Scripts\python.exe -m pip install -r requirements-recording.txt
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -X utf8 scripts\record_video.py
```

脚本在 `data/video-recording/<时间>/` 下创建独立任务数据库与原始 WebM，并且只终止自己启动的进程。
每次重录会更新 `deliverables/AnimFlow-demo.mp4`、封面与执行证据；旧原始记录仍在各自 data 子目录。
`deliverables/` 被 Git 忽略，避免视频和本地交付附件混入源码提交。

## 视频覆盖

1. 真实 DeepSeek 规划 → 可见模型、耗时、usage、request_id → GIF 播放与 ZIP 下载。
2. 下游风格处理持续失败，三次尝试耗尽 → 人工重试 → 原规划 artifact/request_id 保留。
3. 明确切 MOCK：瞬时故障自动退避重试。
4. 对自己启动的 Worker 执行真实 `taskkill /F /T` → 新 Worker 恢复，旧 attempt 为 WORKER_LOST，上游次数不变。
5. 两任务竞争一个模拟 GPU 槽位；采样 holder 峰值不超过 1，两个任务均成功。
6. 刷新页面后任务与历史从数据库恢复。

视频可作附件；源码和 Markdown 说明仍通过公开仓库交付。发送前本人核对个人信息、真实工时/工具用量，并另附简历。

## 2026-11-01 成片验证

本机成片：`deliverables/AnimFlow-demo.mp4`，**3 分 26 秒（206.08 秒），1600×900，H.264，无音轨，7,244,956 字节（约 7.24 MB）**。
已用 FFmpeg 对整段视频完整解码，返回 0；抽查 11 个时间点的画面，字幕和关键表格可读。
成片为连续实录，无剪辑跳过步骤。片中真实模型调用两次，各 676 tokens，总计 1352 应用 tokens（不等于开发工具用量）。
全部场景均有实际断言：模型响应、ZIP CRC、规划 artifact_id/request_id 复用、自动重试、真实强杀/重启、
上游不重跑、GPU 逻辑预约峰值 1、刷新后任务保留。
执行时间线与媒体验证见 `docs/evidence/video-2026-11-01.json`。

本机另外提供 `deliverables/提交前核对.md` 和 `deliverables/视频字幕提纲.md`。
成片未上传外部视频平台、未发送邮件；可由本人作为附件提交。
