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

期望：`38 passed`。`-v` 可看每个用例名；完整输出可存档到 `docs/evidence/`。

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

要点：耗时约等于租约剩余时间 + 轮询延迟，**不是瞬时恢复**；成功上游 `extract_frames` 的
`attempt_count` 保持 1（不重跑）；旧 attempt 目录保留为孤儿文件但不发布。

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

两个终端：

```bash
# 终端 1：API
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 终端 2：Worker
.venv/Scripts/python.exe -m app.worker
```

浏览器打开 `http://127.0.0.1:8000/`：

1. 点「瞬时失败→自动重试」预设 → 左侧出现任务，右侧步骤卡逐个变 `RUNNING` →
   `stylize_frames` 出现一次 `RETRY_WAIT`（显示 `RETRY_BACKOFF` 与下次尝试时间）→ 最终 `SUCCEEDED`。
2. 点「永久失败→人工重试」→ 任务 `FAILED`，下游 `BLOCKED` → 点「人工重试」（勾选清故障）→ 任务回到队列并成功。
   保留 `?job_id=...` 刷新页面，历史与选择都能恢复。
3. 点「GPU 争抢 ×2」→ 资源面板里 `gpu-0` 只有一个 holder，另一个任务显示 `WAIT GPU`；CPU 步骤照常推进。
4. 下载 GIF 能播放、ZIP 能解压；「attempt 历史」能看到每次尝试与错误码。
5. **硬杀实验（手工）**：`taskkill /F /T /PID <worker的pid>`（Worker 终端里打印过）→ 页面显示步骤仍 `RUNNING` 直到租约过期
   → 另起 Worker → 时间线出现 `ATTEMPT_WORKER_LOST` → 新 attempt 接管 → 任务最终成功。

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

```bash
# 终端 1（API）
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 终端 2（Worker，稍后硬杀用它）
.venv\Scripts\python.exe -m app.worker
# 浏览器打开 http://127.0.0.1:8000/
```

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

- **操作**：点预设「慢任务」→ 等 stylize_frames 进入 RUNNING → 切到终端 2，
  `Ctrl+C` 无效则直接关掉窗口也可以，但推荐用命令硬杀：
  在**第三个终端**执行 `taskkill /F /T /PID <终端2的python PID>`（或任务管理器结束该进程树）
  → 回到页面观察 → 再在终端 2 重新启动 `.venv\Scripts\python.exe -m app.worker` → 等任务成功。
- **预期画面**：硬杀后页面上该步骤短暂仍显示 RUNNING（租约未过期，这正是设计）；
  新 Worker 启动后几秒内，事件时间线出现 ATTEMPT_WORKER_LOST 与 STEP_RETRY_SCHEDULED，
  attempt 历史出现 #2 RUNNING → SUCCEEDED，任务最终 SUCCEEDED；旧 #1 标记 WORKER_LOST。
  实测恢复耗时约 2 秒（= 租约剩余 + 轮询延迟）。
- **讲解要点**：「监督进程死了租约才过期，由下一个 Worker 回收——at-least-once，不是 exactly-once；
  计算可能重做，但只有当前有效 attempt 能发布结果，旧 attempt 的心跳/提交/释放全被拒绝。
  恢复是步骤边界级：这一步整步重跑，没有分帧断点续算。」

### 第 7 段 · 收尾（约 20 秒）

- **操作**：镜头回到看板总览；可顺手 `Ctrl+R` 刷新页面展示状态从数据库恢复。
- **预期画面**：刷新后任务列表、选中任务、事件与 attempt 历史原样恢复。
- **讲解要点**：「45 项自动化测试全绿，含真实跨进程硬杀与失效窗口；
  边界如实标注：单机 SQLite、模拟 GPU 槽位、步骤边界恢复。测试与证据见仓库 docs/test-report.md。」

总时长约 5 分 45 秒。若时间紧，可砍掉第 5 段（GPU 争抢）压到 5 分钟内。
