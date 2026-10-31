# 演示脚本：跟着做能复现的四类实验

全部在**本机**执行，只结束脚本自己创建的进程，不碰机器上其他进程。
时间与 ID 来自实际运行，不复制伪造数据。

## 0. 准备（一次）

```bash
cd /d/AnimFlow          # 项目根目录
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

## 1. 自动化测试（约 100 秒）

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

期望：`75 passed`（45 项恢复/接口测试 + 30 项模型接入测试，均离线）。`-v` 可看每个用例名；
完整输出可存档到 `docs/evidence/`。

## 2. 硬杀恢复（约 30 秒，A04/A09 的手工版）

```bash
.venv/Scripts/python.exe scripts/crash_demo.py
```

脚本会：启动 Worker → 提交慢 stylize 任务 → 等到 `stylize RUNNING` 且 GPU 槽已预约 →
`taskkill /F /T` 硬杀自己创建的 Worker → 打印「硬杀后瞬间 attempt 仍 RUNNING（租约未过期）」
→ 另起 Worker → 打印数据库里的真实事件时间线与逐条断言。

期望输出（关键行）：

```
[Worker-1] pid=...
stylize RUNNING，active_attempt=...（GPU 槽已预约）
硬杀 Worker-1（taskkill /F /T，只杀本脚本创建的 pid）…
硬杀后瞬间: attempt 状态=RUNNING（租约 1s 后过期）
[Worker-2] pid=...（另一进程，接手恢复）
  ... ATTEMPT_WORKER_LOST {"attempt_no": 1, ...}
  ... STEP_RETRY_SCHEDULED {"attempt_no": 1, "error_code": "WORKER_LOST", ...}
  ... STEP_CLAIMED {"step": "stylize_frames", "attempt_no": 2, ...}
  ... JOB_SUCCEEDED {}
  [PASS] 旧 attempt = WORKER_LOST
  [PASS] 新 attempt_id 不同
  [PASS] 新 attempt 成功
  [PASS] 最终任务 SUCCEEDED
  [PASS] GPU 槽已释放
恢复耗时: 硬杀→WORKER_LOST = 2.07s
```

要点：

- **上表的 2.07s 只对 `crash_demo.py` 的内置短租约配置成立**（脚本内部设
  `ANIMFLOW_LEASE_S=2`、`ANIMFLOW_POLL_S=0.2`、`ANIMFLOW_HEARTBEAT_S=0.5`）。
  默认配置的租约是 10 秒，用默认配置演示时识别失联会更晚。
- 恢复耗时受四件事影响：**硬杀时租约还剩多久**、新 Worker 何时启动、轮询间隔、
  退避（`STEP_RETRY_SCHEDULED` 里的 `next_run_at`）。应分三段看，不是「2 秒全部恢复」：
  1) **识别失联**：`ATTEMPT_WORKER_LOST` 出现（≈ 租约剩余 + 轮询延迟）；
  2) **开始重试**：退避窗口后 `STEP_CLAIMED` 出现新 attempt（默认退避 1s 起）；
  3) **任务最终完成**：新 attempt 把剩余步骤跑完。
- 成功上游 `extract_frames` 的 `attempt_count` 保持 1（不重跑）；
  旧 attempt 目录保留为孤儿文件但不发布。

## 3. 四个业务场景（约 1～2 分钟）

```bash
.venv/Scripts/python.exe scripts/demo.py
```

自足启动 API（随机端口）与 Worker，依次跑：

1. **成功管线**：六步 `SUCCEEDED`，打印真实 GIF 路径。
2. **瞬时失败→自动重试**：stylize `#1 FAILED (MOCK_TRANSIENT_FAULT)` → `#2 SUCCEEDED`，
   并打印 `STEP_RETRY_SCHEDULED` 事件。
3. **持续失败→人工 retry**：任务 `FAILED`（stylize 尝试 3 次，下游 `BLOCKED`）→
   `retry(clear_fault=true)` → 成功；`extract` 的 attempts 仍为 1。
4. **双任务争 GPU**：采样期间 GPU 有效 holder 峰值 = 1（≤1 即通过）。

脚本结束会打印已清理自己创建的进程。

## 4. 看板手工操作（约 3 分钟）

两个终端（Windows PowerShell 命令，可直接复制；每条都在标注的终端里执行）：

```powershell
# 终端 1（API）—— 默认配置即可；若做短租约硬杀演示，与终端 2 保持同一套环境变量
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 终端 2（Worker，短租约演示配置：租约 2s / 心跳 0.5s / 轮询 0.2s / 演示加速）
$env:ANIMFLOW_LEASE_S="2"; $env:ANIMFLOW_HEARTBEAT_S="0.5"; $env:ANIMFLOW_POLL_S="0.2"; $env:ANIMFLOW_STEP_SPEED="8"
.venv\Scripts\python.exe -m app.worker
```

> 租约相关参数只有 Worker 使用；为了让「重启 Worker 后配置一致」一眼可核对，
> 终端 1 也可以设置同一组变量（无害）。**不设置则使用默认租约 10s**，
> 届时识别失联的时间会明显更长——这是设计行为，不是恢复失败。

浏览器打开 `http://127.0.0.1:8000/`：

1. 点「瞬时失败→自动重试」预设 → 左侧出现任务，右侧步骤卡逐个变 `RUNNING` →
   `stylize_frames` 出现一次 `RETRY_WAIT`（显示 `RETRY_BACKOFF` 与下次尝试时间）→ 最终 `SUCCEEDED`。
2. 点「永久失败→人工重试」→ 任务 `FAILED`，下游 `BLOCKED` → 点「人工重试」（勾选清故障）→ 任务回到队列并成功。
   保留 `?job_id=...` 刷新页面，历史与选择都能恢复。
3. 点「GPU 争抢 ×2」→ 资源面板里 `gpu-0` 只有一个 holder，另一个任务显示 `WAIT GPU`；CPU 步骤照常推进。
4. 下载 GIF 能播放、ZIP 能解压；「attempt 历史」能看到每次尝试与错误码。
5. **硬杀实验（手工）**：点「慢任务」预设，等 `stylize_frames` 进入 `RUNNING`，在**第三个 PowerShell**
   里用 `taskkill` 结束终端 2 的 Worker 进程树（PID 用任务管理器或
   `Get-Process python | Select-Object Id, StartTime` 核对，确保是你启动的那个）：

   ```powershell
   taskkill /F /T /PID <终端2的python PID>
   ```

   `Ctrl+C` 是优雅停止，**不属于强制终止实验**，不要用它替代 taskkill。
   硬杀后页面上该步骤会短暂仍显示 `RUNNING`——这是租约未过期的正常表现。
   然后在终端 2 **用与启动时相同的环境变量**重启 Worker：

   ```powershell
   $env:ANIMFLOW_LEASE_S="2"; $env:ANIMFLOW_HEARTBEAT_S="0.5"; $env:ANIMFLOW_POLL_S="0.2"; $env:ANIMFLOW_STEP_SPEED="8"
   .venv\Scripts\python.exe -m app.worker
   ```

   观察时间线分三段推进：`ATTEMPT_WORKER_LOST`（**识别失联**，≈ 租约剩余 + 轮询延迟）
   → 退避后 `STEP_CLAIMED` 出现新 attempt（**开始重试**，默认退避 1s 起）
   → 新 attempt 完成剩余步骤（**任务最终完成**）。三段总和才是恢复总耗时，
   不承诺固定秒数；短租约（2s）配置下通常几秒内完成，默认 10s 租约则识别失联更晚。

## 5. 边界演示

```bash
# 关闭故障注入（演示部署时用）
ANIMFLOW_FAULTS_ENABLED=0 .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
# 此时 POST 带 demo_fault 返回 422 FAULTS_DISABLED

# 非法输入
curl -X POST http://127.0.0.1:8000/api/jobs -H "Content-Type: application/json" \
  -d '{"params":{"width":9999}}'      # 422 INPUT_INVALID，不入队

# 未知任务 / 未知产物
curl http://127.0.0.1:8000/api/jobs/nope          # 404
curl http://127.0.0.1:8000/api/artifacts/nope/download   # 404
```

## 6. 录制用演示脚本（4～6 分钟）

面向录像的单遍流程。**录制前先跑一遍全流程**；下方「预期画面」按本机实测写成，
若你的机器较慢，把启动命令加上 `ANIMFLOW_STEP_SPEED=8` 加速演示计算。

### 开场前准备（不录或快进，约 1 分钟）

两终端都用 PowerShell；**短租约演示配置必须两边一致**，且第 6 段重启 Worker 时
在终端 2 重复设置同一组变量（环境变量不跨新窗口持久）：

```powershell
# 终端 1（API）
$env:ANIMFLOW_LEASE_S="2"; $env:ANIMFLOW_HEARTBEAT_S="0.5"; $env:ANIMFLOW_POLL_S="0.2"; $env:ANIMFLOW_STEP_SPEED="8"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 终端 2（Worker，稍后用 taskkill 硬杀它）
$env:ANIMFLOW_LEASE_S="2"; $env:ANIMFLOW_HEARTBEAT_S="0.5"; $env:ANIMFLOW_POLL_S="0.2"; $env:ANIMFLOW_STEP_SPEED="8"
.venv\Scripts\python.exe -m app.worker

# 浏览器打开 http://127.0.0.1:8000/
```

> 这套 2 秒租约只为录制时长服务；默认租约是 10 秒，不设变量即为默认。
> 录制中引用的「几秒内识别失联」只对该短租约配置成立，讲解时说明配置即可。

### 第 1 段 · 架构开场（约 30 秒）

- **操作**：镜头给两个终端 + 浏览器看板三栏布局。
- **预期画面**：终端 1 显示 uvicorn 已启动；终端 2 显示「Worker 启动 id=…」；页面顶部「已连接」。
- **讲解要点**：「API 和 Worker 是两个独立进程，共享同一个 SQLite；API 挂了 Worker 照跑，
  Worker 没开 API 也能入队。页面所有状态来自 API 查询，刷新不丢。」

### 第 2 段 · 成功管线与真实产物（约 60 秒）

- **操作**：点预设「成功管线」→ 等六步跑完 → 页面下方 GIF 自动播放 → 点 final_zip 的「下载」并打开。
- **预期画面**：六个步骤卡依次 RUNNING→SUCCEEDED；「已完成 6/6 步」；GIF 真的动；
  ZIP 解压含 preview.gif / params.json / source_summary.json / README.txt。
- **讲解要点**：「六步固定 DAG；产物是 Pillow 生成的真实 PNG→GIF→ZIP，带 SHA-256 清单；
  AI 与 GPU 计算是确定性模拟，素材由程序生成。」

### 第 3 段 · 瞬时失败与自动重试（约 60 秒）

- **操作**：点预设「瞬时失败→自动重试」→ 观察 stylize_frames 卡片 → 跑完后看「Attempt 历史」与「事件时间线」。
- **预期画面**：stylize 先 FAILED（错误码 MOCK_TRANSIENT_FAULT）→ 出现 RETRY_WAIT 芯片
  （RETRY_BACKOFF + 下次尝试时间）→ 第 2 次尝试 SUCCEEDED；attempt 历史里 stylize 两行（#1 FAILED、#2 SUCCEEDED）；
  事件里有 STEP_RETRY_SCHEDULED。
- **讲解要点**：「每次执行有唯一 attempt_id；失败按退避自动重试，预算有界（每轮 3 次）；
  上游 inspect/extract 的 attempt 数始终是 1——成功步骤不会重跑。」

### 第 4 段 · 永久失败、fail-fast 与人工重试（约 60 秒）

- **操作**：点预设「永久失败→人工重试」→ 等任务 FAILED → 指针指下游 BLOCKED 与 stylize 的 3 次失败
  → 勾选「清除故障注入」点「人工重试」→ 等任务再次成功 → 展开 attempt 历史看 stylize 第 4 行。
- **预期画面**：任务 FAILED，stylize 恰好 3 次尝试（不是 4 次），render/package 显示 BLOCKED；
  重试后任务回队列并最终 SUCCEEDED；attempt 历史保留 3 条 FAILED + 新增 SUCCEEDED；
  上游 extract 的 attempts 仍为 1；事件里有 JOB_RETRIED 与 OUTPUT_REUSED。
- **讲解要点**：「出现最终失败后 fail-fast：不再领新步骤，在途排空后任务 FAILED、下游 BLOCKED。
  人工重试只接受 FAILED 任务，保留全部历史、复用有效成功产物；清故障只清演示注入，不改业务参数。」

### 第 5 段 · GPU 逻辑预约（约 45 秒）

- **操作**：点预设「GPU 争抢 ×2」→ 盯住右下「资源槽位」面板 → 等两个任务都完成。
- **预期画面**：两个任务排队进入 stylize；gpu-0 同一时刻只有一个 holder（另一个任务步骤显示
  等待 GPU 的芯片）；最终两个都 SUCCEEDED。
- **讲解要点**：「这是数据库里的逻辑槽位预约，有效 GPU holder 恒 ≤1；等 GPU 不消耗尝试次数，
  CPU 步骤不受阻。要如实说明：不是真实显卡隔离，不采集显存/温度等真实指标。」

### 第 6 段 · 硬杀恢复（约 60 秒，核心卖点）

- **操作**：点预设「慢任务」→ 等 stylize_frames 进入 RUNNING → 打开**第三个 PowerShell**，
  先核对 PID（任务管理器，或 `Get-Process python | Select Id, StartTime`，确认是终端 2 那个），
  然后**强制终止**（`Ctrl+C` 是优雅停止，不属于强制终止实验，不要用它替代）：

  ```powershell
  taskkill /F /T /PID <终端2的python PID>
  ```

  → 回到页面观察 → 在终端 2 **用与开场相同的环境变量**重启 Worker：

  ```powershell
  $env:ANIMFLOW_LEASE_S="2"; $env:ANIMFLOW_HEARTBEAT_S="0.5"; $env:ANIMFLOW_POLL_S="0.2"; $env:ANIMFLOW_STEP_SPEED="8"
  .venv\Scripts\python.exe -m app.worker
  ```

  → 等任务成功。
- **预期画面**：硬杀后页面上该步骤短暂仍显示 RUNNING（租约未过期，这正是设计）。
  之后时间线按三段推进，讲解时逐段指认：
  1) **识别失联**：`ATTEMPT_WORKER_LOST` 出现（本配置下 ≈ 租约剩余 + 轮询延迟，秒级）；
  2) **开始重试**：`STEP_RETRY_SCHEDULED` 显示退避（本配置 1s 起），随后 `STEP_CLAIMED` 出现 #2；
  3) **任务最终完成**：#2 跑完 stylize 及剩余步骤，任务 SUCCEEDED；旧 #1 标记 WORKER_LOST。
  三段总和才是恢复总耗时，不承诺固定秒数；若用默认 10 秒租约演示，第 1 段会明显更长。
- **讲解要点**：「监督进程死了租约才过期，由下一个 Worker 回收——at-least-once，不是 exactly-once；
  计算可能重做，但只有当前有效 attempt 能发布结果，旧 attempt 的心跳/提交/释放全被拒绝。
  恢复是步骤边界级：这一步整步重跑，没有分帧断点续算。」

### 第 7 段 · 收尾（约 20 秒）

- **操作**：镜头回到看板总览；可顺手 `Ctrl+R` 刷新页面展示状态从数据库恢复。
- **预期画面**：刷新后任务列表、选中任务、事件与 attempt 历史原样恢复。
- **讲解要点**：「自动化测试覆盖真实跨进程硬杀、失效窗口及模型接入；最新结果见测试报告。
  边界如实标注：单机 SQLite、模拟 GPU 槽位、步骤边界恢复。测试与证据见仓库 docs/test-report.md。」

总时长约 5 分 45 秒。若时间紧，可砍掉第 5 段（GPU 争抢）压到 5 分钟内。

## 7. 真实 DeepSeek 录制版（2026-11-01）

前述步骤与截图是原 MOCK 演示的流程；真实模式按下列新流程录制，避免仍宣称“AI 全是模拟”。
本机 `.env` 已配置时无需再输入 Key，不要展示文件或密钥控制台。

两个 PowerShell 窗口：

```powershell
cd D:\AnimFlow
powershell -ExecutionPolicy Bypass -File .\scripts\recording.ps1 api
# 另一个窗口：
cd D:\AnimFlow
powershell -ExecutionPolicy Bypass -File .\scripts\recording.ps1 worker
```

浏览器访问 `http://127.0.0.1:8000/`，确认“DeepSeek（真实调用）”。推荐录像顺序：

1. 输入“青蓝色光球绕圆形轨道运动，深蓝背景，紫色光环”，点成功管线。
2. 完成后展示“真实 DeepSeek”、JSON 方案、request_id、耗时、输入/输出 token 与 GIF。说明模型负责规划，Pillow 负责画面，GPU 仍模拟。
3. 改成“橙色光球上下弹跳，深紫背景，金色光环”，点成功管线。比较方案的 motion/颜色与真实画面变化。
4. 点“永久失败→人工重试”，模型规划成功后只在风格处理失败。清故障并重试，指出 plan_motion attempt=1、request_id 不变，重试未重复调用已成功的规划。
5. 硬杀恢复可先切 MOCK 以免录制过程多耗模型额度，或保留真实模式、等 plan_motion 已成功且 stylize RUNNING 时再硬杀自己启动的 Worker。
   用同一 recording.ps1 worker 重启，查看 WORKER_LOST → 新 stylize attempt → 成功，规划仍复用。

录像脚本租约为 3 秒，识别失联与开始重试的时间取决于剩余租约/重启/轮询/退避，不承诺固定 2 秒完成整个任务。
真实调用可能限流/超时；出现错误如实展示，不能把 MOCK 结果说成真实模型。再次调用可能付费；已成功规划可复用不等于外部服务计费 exactly-once。
画面能力限定为光球/光环/配色/运动原语，不能将它描述成通用文生视频。

显式自动验证：`.venv\Scripts\python.exe scripts\live_demo.py`；只重看已完成真实任务（不新调用）：
`.venv\Scripts\python.exe scripts\browser_live_check.py`。
