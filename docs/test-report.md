# 测试报告

区分标注：**自动化测试**（pytest，可重复）与**手工演示**（脚本/人工操作）。
所有统计来自真实输出，未做推算；token 与个人工时不从测试数量折算。

## 1. 实际运行环境

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11 Home China（10.0.26100） |
| Python | 3.12.4 |
| SQLite | 3.45.3（标准库 `sqlite3`） |
| 关键依赖 | fastapi 0.162.0 / uvicorn 0.54.0 / pillow 13.0.0 / pydantic 2.14.2 / pytest 9.2.0 / httpx 0.28.1 |
| 运行日期 | 2026-10-31 |

## 2. 测试命令与结果

```bash
# 创建虚拟环境并安装（README 冷启动步骤）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Windows
# .venv/bin/python -m pip install -r requirements-dev.txt         # macOS/Linux

# 运行全部测试
.venv/Scripts/python.exe -m pytest tests/ -q
```

**结果：`45 passed, 1 warning in 97.93s`**（2026-10-31，正确性修复轮后的最终数字）
证据：`docs/evidence/pytest-full-2026-10-31.txt`（`-v` 完整输出）
唯一的 warning 是 `fastapi/testclient` 的 `StarletteDeprecationWarning`（建议安装 `httpx2`），与本项目逻辑无关。

按文件分组（同一次运行）：

| 文件 | 数量 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_state_machine.py` | 26 | 状态机与事务（A02/A03/A05/A07/A08/A13/A14 的事务断言 + 正确性修复轮 6 项回归） |
| `tests/test_recovery.py` | 12 | 真实跨进程硬杀与恢复（A01/A04/A06/A07/A08/A09/A11/A14） |
| `tests/test_api.py` | 7 | HTTP 接口与看板数据（A01/A03/A10/A11/A12 + 平铺输入回归） |
| `tests/test_resources.py` | 0 | 见下方说明 |

> `tests/test_resources.py` 未单独建文件：A08 的 GPU 互斥断言由 `test_recovery.py::test_a07_two_workers_two_jobs_gpu_mutex`
> （真实双 Worker + 双任务采样）与 `test_state_machine.py::test_two_repos_cannot_claim_same_gpu_step`（注入时钟精确断言）共同覆盖，
> 不为凑目录结构造空文件。

## 3. P0 验收矩阵

| 编号 | 场景 | 测试名（自动化 / 手工） | 结果 |
| --- | --- | --- | --- |
| A01 | API 起、Worker 未起时提交，再起 Worker | `test_api.py::test_a01_post_before_worker_then_success` | PASS |
| A02 | stylize 瞬时失败后自动重试 | `test_state_machine.py::test_a02_transient_retry_then_success` | PASS |
| A03 | 持续失败耗尽预算→人工 retry | `test_state_machine.py::test_a03_budget_exhaust_then_manual_retry`；`test_api.py::test_a03_retry_http_semantics` | PASS |
| A04 | 步骤运行中硬杀 Worker，另起恢复 | `test_recovery.py::test_a04_hard_kill_worker_then_recover` | PASS |
| A05 | 旧 attempt 的 heartbeat/complete/release 被拒 | `test_state_machine.py::test_a05_stale_attempt_rejected_after_new_attempt`、`::test_a05_owner_mismatch_rejected` | PASS |
| A06 | 子进程卡住、心跳仍在，超时回收 | `test_recovery.py::test_a06_step_timeout_kills_child_and_retries` | PASS |
| A07 | 双 Worker 竞争，无重复领取 | `test_recovery.py::test_a07_two_workers_one_job_no_double_claim` | PASS |
| A08 | 双任务争一个 mock GPU，CPU 不受阻 | `test_recovery.py::test_a07_two_workers_two_jobs_gpu_mutex`（采样峰值 ≤1） | PASS |
| A09 | 产物写完但成功事务前硬杀 | `test_recovery.py::test_a09_crash_after_files_before_publish`；成功提交后重启不重跑 `::test_a09_db_committed_then_restart_no_rerun` | PASS |
| A10 | API 重启、Worker 继续、事件分页 | `test_api.py::test_a10_api_restart_worker_continues` | PASS |
| A11 | 上游产物损坏→阻断续跑与下载 | `test_recovery.py::test_a11_corrupt_upstream_blocks_manual_retry`；`test_api.py::test_a11_corrupt_artifact_download_and_retry_409` | PASS |
| A12 | 非法输入/未知任务/恶意路径/冷启动持久 | `test_api.py::test_a12_validation_and_404`、`::test_download_path_is_db_driven_not_user_path` | PASS |
| A13 | claim/complete 注入异常→回滚无半套状态 | `test_state_machine.py::test_a13_claim_tx_rollback_no_partial_state`、`::test_a13_complete_tx_rollback_no_partial_state` | PASS |
| A14 | 不可重试/启动失败/异常退出 | `test_state_machine.py::test_a14_nonretryable_fails_immediately`、`::test_a14_abnormal_exit_consumes_budget`；`test_recovery.py::test_a14_fail_launch_consumes_budget`、`::test_a14_exit_early_then_retry_succeeds`、`::test_a14_claim_then_death_before_spawn_recovered` | PASS |

P0 全部有实测证据，无跳过、无 `xfail`。

### 3.1 正确性修复轮（外部复核问题）的回归用例

外部复核在原 38 项全绿的 main 上复现了三个问题；先补失败测试再修复，新增 7 项：

| 复核问题 | 回归测试 | 修复前 | 修复后 |
| --- | --- | --- | --- |
| 1 租约检查用过早采样的时间（complete 文件校验/等写锁期间过期仍接受） | `test_complete_rejected_when_lease_expires_during_file_verify`、`test_heartbeat_rejected_when_lease_expires_before_tx_check`、`test_fail_rejected_when_lease_expires_before_tx_check` | FAIL（接受过期提交/续租） | PASS |
| 2 领取路径任务级错误未聚合终态（任务停在 RUNNING/QUEUED） | `test_claim_precheck_invalid_artifact_fails_job`、`test_claim_unsupported_pipeline_version_fails_queued_job`、`test_job_fatal_with_inflight_step_drains_then_fails` | FAIL（停在 RUNNING/QUEUED） | PASS（无在途→FAILED+BLOCKED；有在途→排空后 FAILED） |
| 3 平铺 HTTP 输入被静默忽略（`{"width":999}` 存成默认值） | `test_flat_input_parsed_and_validated` | FAIL（202 且存默认） | PASS（合法值入库；非法/未知/混用 422） |

## 4. 恢复时间线（真实证据，来自 `scripts/crash_demo.py`）

存档：`docs/evidence/crash-demo-2026-10-31.txt`（手工演示，可重复执行）

```
18:31:2x STEP_CLAIMED   {"step": "stylize_frames", "attempt_no": 1, "resource": "gpu", ...}
         ↑ 硬杀 Worker-1（taskkill /F /T，只结束本脚本创建的 pid）
18:31:30 ATTEMPT_WORKER_LOST  {"attempt_no": 1, "owner": "...-22940-...", ...}
18:31:30 STEP_RETRY_SCHEDULED {"attempt_no": 1, "error_code": "WORKER_LOST", ...}
18:31:31 STEP_CLAIMED   {"step": "stylize_frames", "attempt_no": 2, ...}
18:31:39 STEP_SUCCEEDED {"step": "stylize_frames", "attempt_no": 2, ...}
18:31:40 JOB_SUCCEEDED  {}
```

断言（脚本内逐条打印）：

- `[PASS]` 旧 attempt = `WORKER_LOST`
- `[PASS]` 新 attempt_id 与旧的不同
- `[PASS]` 新 attempt 成功
- `[PASS]` 最终任务 `SUCCEEDED`
- `[PASS]` GPU 槽已释放（`holder_attempt_id` 全为 NULL）
- 上游 `extract_frames` 的 `attempt_count` 仍为 1（成功上游未重跑）
- **恢复耗时：硬杀 → `WORKER_LOST` = 2.07s**（= 租约剩余 + 轮询/处理延迟；租约 2s、轮询 0.2s，非瞬时恢复）

## 5. 手工演示（`scripts/demo.py`）

自足启动 API 与 Worker，跑四个场景（存档见 `docs/demo-script.md` 的复现步骤）：

| 场景 | 观察到的结果 |
| --- | --- |
| 1 成功管线 | 六步 `SUCCEEDED`；GIF 12 帧 256×256；ZIP 含 `preview.gif`/`params.json`/`source_summary.json`/`README.txt` |
| 2 瞬时失败→自动重试 | stylize `#1 FAILED (MOCK_TRANSIENT_FAULT)` → `#2 SUCCEEDED`；事件含 `STEP_RETRY_SCHEDULED` |
| 3 持续失败→人工 retry | 任务 `FAILED`，stylize 尝试 3 次（预算 3），下游 `BLOCKED`；`retry(clear_fault=true)` 后成功，extract `attempts` 仍为 1，stylize `retry_round=1` |
| 4 双任务争 GPU | 两任务均 `SUCCEEDED`；采样期间 GPU 有效 holder 峰值 = 1 |

## 5A. 浏览器实测（真实 headless Chromium，11/11 PASS）

证据：`docs/evidence/browser-check-2026-10-31.txt`。用 Playwright 驱动真实 Chromium 打开看板页面，
实际点击预设按钮与重试按钮，断言 DOM 与页面内状态（非只看 API）：

| 检查项 | 结果 |
| --- | --- |
| GIF 预览在页面 `<img>` 内解码成功 | PASS（naturalWidth×Height = 256×256） |
| ZIP 下载（页面链接）为合法 zip | PASS（含 preview.gif 等 4 个文件） |
| 永久失败 → FAILED → 点击「人工重试」→ 成功，stylize 历史保留 3×FAILED + SUCCEEDED | PASS |
| 快速切换两个任务（~120ms×10 次）后事件不串台 | PASS（渲染事件集与另一任务 id 集交集=0） |
| 快速切换后 attempt 表只含选中任务记录 | PASS（dom 6 行 = API 6 条） |

> 该轮同时修复了看板的一个真实缺陷：快速切换任务时，`pollEvents`/`renderAttempts` 的在途响应
> 没有作废机制，旧任务的事件可能渲染进新任务视图。修复：`selectJob` 递增 `jobToken`，
> 所有异步渲染前校验 token，过期响应直接丢弃（`app/static/app.js`）。此项为浏览器实测前的
> 代码审查发现，实测用于确认修复有效。

## 6. 真实遇到并修复的缺陷

| 现象 | 根因 | 修复 | 回归验证 |
| --- | --- | --- | --- |
| claim 触发 `FOREIGN KEY constraint failed` | 先占槽再插 attempt，违反外键引用顺序 | 改为先插 attempt 再条件占槽 | `test_claim_creates_attempt_and_occupies_slot` |
| render_preview 首次尝试 `PermissionError [WinError 5]` | Windows 上监督进程读 `progress.json` 与子进程 `os.replace` 竞态 | `atomic_write_bytes` 对 `os.replace` 有界重试 | 冒烟重跑一次成功；`test_a01_worker_end_to_end` |
| `create_job` 收到的 `demo_fault` 未生效（故障从未触发） | 参数里内嵌的 `demo_fault` 未被拆出，被当成业务参数丢弃 | `create_job` 自动拆分内嵌 `demo_fault`，且不计入 `input_hash` | A02/A03/A06/A09/A14 相关测试由失败转通过 |
| 人工 retry 后 `fatal_error_code` 为 `None` | 在事务内 `raise ArtifactInvalid`，把 `ARTIFACT_INVALID` 写入一起回滚 | 先提交 fatal + 事件，事务外再抛 409 | `test_a11_corrupt_upstream_blocks_manual_retry` |
| `artifact_health` 显示 `valid` 但目录内 PNG 已被删 | 健康检查只验目录存在性 | 改为逐文件完整 SHA-256 校验 | `test_a11_*` |
| FastAPI 把 `body` 当成 query 参数，POST 返回 422 | Pydantic 模型定义在 `create_app` 函数内，注解无法解析 | 模型提升到模块级 | `test_api.py` 6 项由失败转通过 |
| 子进程 `NameError: Image` | `_stylize_image` 未导入 `PIL.Image` | 补导入 | 冒烟测试六步全绿 |
| README 冷启动：`pip install -r requirements-dev.txt` 报 `UnicodeDecodeError: 'gbk'` | 中文 Windows 默认 locale 为 cp936，pip 解析 requirements 中的中文注释失败 | requirements 文件改为纯 ASCII 注释 | 新临时目录 + 全新 venv 冷启动通过 |
| **（外部复核 1）** complete/heartbeat/fail 用方法入口采样的时间做租约检查：文件校验或等写锁期间租约过期仍被接受/续租 | `now` 在事务前采样，`_load_active_attempt` 沿用过期值 | 所有持有权路径（heartbeat/complete/fail/timeout/recover/claim/release/manual_retry）改为 `BEGIN IMMEDIATE` 之后取时，并加注入点 | 3 项回归由失败转通过（`test_*_before_tx_check`、`test_complete_*_during_file_verify`） |
| **（外部复核 2）** claim 路径发现任务级错误（产物损坏/版本不支持）后任务永远停在 RUNNING 或 QUEUED | `_set_job_fatal` 后直接 return，未调用聚合 | 每个 fatal 点同事务调用 `_aggregate_job`；上游产物收集移到占槽之前（缺产物时不留半套 claim） | 3 项回归由失败转通过 |
| **（外部复核 3）** `POST /api/jobs {"width":999}` 返回 202 并保存默认值 | Pydantic v2 `model_dump()` 把 extra 拍平，`data["__pydantic_extra__"]` 恒为 None，平铺键被丢弃 | 直接读 `body.__pydantic_extra__`；平铺格式正确解析校验，嵌套与平铺混用明确 422 | `test_flat_input_parsed_and_validated` 由失败转通过 |
| 快速切换任务时事件/attempt 可能串台 | `pollEvents`/`renderAttempts` 无选择代次防护，旧任务在途响应落进新任务视图 | `jobToken`：切换时递增，异步渲染前校验，过期丢弃 | 浏览器实测 11/11 PASS（含 120ms×10 快速切换） |

## 7. 已知限制（非缺陷，如实标注）

- 执行语义是 **at-least-once**：崩溃后计算可能重做，靠 attempt 身份 + 产物隔离 + 事务提交保证只有当前有效 attempt 能发布结果。不宣称 exactly-once。
- **恢复是步骤边界级**：单个步骤中断整步重跑，没有分帧断点续算（B01 未实现）。
- **GPU 是模拟槽位**：限制的是数据库里的有效预约数，不是真实 CUDA 进程数；不采集 utilization/显存/温度。
- **租约过期 ≠ 旧进程消失**：硬杀监督进程后子进程可能短暂残留，它只能写旧 attempt 目录，不能发布数据库状态。
- **单机 SQLite**：只适合同机本地持久化；不应把 WAL 放在跨主机网络文件系统上。
- 崩溃留下的孤儿产物目录保留未清理，便于排查，但会占用磁盘。
- `tests/test_resources.py` 未单独建文件（见第 2 节说明）。
