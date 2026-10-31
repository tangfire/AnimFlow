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
