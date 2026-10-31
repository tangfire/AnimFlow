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
