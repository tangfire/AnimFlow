# docs/assets 生成方式

`demo-neon-seed7.gif` 来自 `scripts/demo.py` 场景 1 的真实产物
（任务 2f1dc043，参数 width=256 height=256 frame_count=12 fps=25 style=neon seed=7），
由 `render_preview` 步骤用 Pillow 合成。复制自 `data/demo-run/artifacts/.../preview.gif`，
未做任何修改；素材由程序生成，AI/GPU 计算为模拟。

`deepseek-orbit-demo.gif` 来自 2026-11-01 `scripts/live_demo.py` 的真实 DeepSeek 规划任务，
任务 `61628b692b70416c862bee242d522015`，36 帧 256×256、12 fps。模型决定 motion=orbit 及青蓝/紫配色；
Pillow 按规划渲染。`deepseek-dashboard.png` 是 `scripts/browser_live_check.py` 对该已发布任务的真实 Chromium 截图。
调用用量与复用证据见 `docs/evidence/deepseek-live-2026-11-01.json`；这两张新素材不属于原 MOCK 演示。

`deepseek-bounce-demo.gif` 是第二个真实请求的对照产物（任务 `80378daa621348b098fae804eaf1f219`），
橙色光球/金色光环/深紫背景、motion=bounce，36 帧 12fps；来自 `scripts/live_demo.py --data-dir data/deepseek-contrast --brief ...`。
用量与来源见 `docs/evidence/deepseek-contrast-2026-11-01.json`。

`AnimFlow-demo.mp4` 是 2026-11-01 使用 `scripts/record_video.py` 录制并完整解码校验的提交演示视频，
206.08 秒、1600×900、H.264、中文字幕、无音轨，7,244,956 字节。与本机 deliverables 成片 SHA-256 一致。
录制边界见 `docs/video.md`，执行与校验证据见 `docs/evidence/video-2026-11-01.json`。

`AnimFlow-demo-narrated.mp4` 是同一实录的中文合成旁白版（当前推荐提交），206.08 秒、1600×900、H.264+AAC，
10,243,299 字节。字幕说明层的开头/片尾标签更新为“中文合成旁白”，实际业务画面保持一致。
声音由 `scripts/narrate_video.py` 按 `docs/narration.json` 20 段时间轴生成，完整解码与音量检查通过。
