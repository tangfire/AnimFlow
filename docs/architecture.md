# 架构：状态机、事务与恢复边界

本文解释 AnimFlow 的数据模型、领取/完成事务、崩溃窗口、小时级任务与真实 GPU 的边界。
所有结论与代码一致；执行语义是 **at-least-once（至少一次）**，不是 exactly-once。

## 1. 运行组件

```
浏览器 ── HTTP ──> FastAPI (app/main.py)          独立进程
                     │  只做查询与入队，不在请求里计算
                     ▼
              SQLite (WAL, 外键)  <──────────  Worker 监督循环 (app/worker.py)  独立进程
              data/animflow.db                                 │ spawn
              data/artifacts/                                  ▼
                                                        步骤子进程 (app/executors.py)
                                                        只写本次 attempt 独享目录
```

- API 与 Worker 互不依赖：API 挂了 Worker 继续；Worker 未启动时 API 仍能持久化入队。
- 每个进程使用**自己的数据库连接**；涉及竞争的写路径用 `BEGIN IMMEDIATE` 短事务。
- 事务内不等待计算、不持有锁做重活。

## 2. 数据模型

| 表 | 作用 |
| --- | --- |
| `jobs` | 任务输入、`input_hash`、pipeline 版本、DAG 快照、聚合状态、任务级 `fatal_error_code` |
| `steps` | 每步骤的依赖、状态、资源、`active_attempt_id`、重试预算（`retry_round`/`attempts_in_round`）、`next_run_at`、进度、`output_fingerprint` |
| `attempts` | 每次真实执行的身份：`attempt_no`、owner、租约（`lease_expires_at`/`heartbeat_at`）、`deadline_at`、终态、`work_dir` |
| `artifacts` | 已发布产物：相对路径、SHA-256、尺寸、清单（多文件产物逐文件摘要） |
| `events` | 结构化业务历史（自增 id），与对应状态**同事务**落库 |
| `resource_slots` | CPU/GPU 槽位；`holder_attempt_id` 指向 attempt，租约权威时间来自该 attempt |

状态机见 `plan.md` 第 6 节；代码集中在 `app/domain.py` 与 `app/repository.py`。

## 3. 领取与完成事务

### 领取 `claim_next_step`
按 `job.created_at → step.order_index` 公平扫描。对每个候选步骤，在**同一 `BEGIN IMMEDIATE` 事务**内依次校验：
任务未 fail-fast / pipeline 与步骤版本受支持 / 依赖全部 `SUCCEEDED` / 依赖产物磁盘完整 /
重试时间已到 / 存在空闲资源槽。全部通过才：先插入 `attempts` 行，再条件更新占槽
（`WHERE holder_attempt_id IS NULL`），更新步骤为 `RUNNING`、写 `STEP_CLAIMED` 事件、聚合任务状态，然后提交。
**提交之后**才 `spawn` 子进程。资源忙则跳过继续找别的工作，不消耗 attempt。

### 完成 `complete_attempt`
子进程退出后，监督进程读取并校验 `result.json`，**在事务外**核对产物清单/尺寸/摘要，再进入发布事务：
匹配 `active_attempt_id` + owner + 租约有效 → 插入 `artifacts`、步骤置 `SUCCEEDED` 并写指纹、
释放槽位（`WHERE holder_attempt_id = 我的 attempt`）、写 `STEP_SUCCEEDED`、聚合任务，单事务提交。

### 身份校验
所有 heartbeat / complete / fail / release 都经过 `_load_active_attempt`：
owner 不匹配、attempt 非 `RUNNING`、租约已过期、或该 attempt 已不是步骤的 `active_attempt_id`，一律拒绝并给出原因码
（`REJECTED_OWNER_MISMATCH` / `REJECTED_ATTEMPT_NOT_RUNNING` / `REJECTED_LEASE_EXPIRED` / `REJECTED_NOT_ACTIVE_ATTEMPT`）。
因此旧 attempt 即使晚到也无法覆盖新 attempt 的进度、产物或资源 holder。测试见 `tests/test_state_machine.py::test_a05_*`。

## 4. 崩溃窗口：文件与数据库不是同一个事务

产物先落盘（原子重命名 + manifest），数据库发布是另一事务，两者之间存在窗口。语义如下：

| 崩溃点 | 结果 |
| --- | --- |
| 写了一半就崩溃 | 无已发布记录 → 不复用，重试写新 attempt 目录 |
| **文件写完、成功事务提交前被硬杀** | 孤儿产物留在旧 `work_dir`，**不发布**；租约过期后该 attempt 记 `WORKER_LOST`，新 attempt 写新目录并发布 |
| 数据库已提交后 Worker 崩溃 | 成功保留，重启不重跑（`test_a09_db_committed_then_restart_no_rerun`） |
| 旧 attempt 晚到 | 身份校验拒绝，忽略其结果，不覆写新目录 |

「文件写完但事务未提交」的失效窗口由 `demo_fault.crash_after_files` 触发，
测试 `tests/test_recovery.py::test_a09_crash_after_files_before_publish` 真实硬杀验证。

## 5. 小时级任务怎么演进

租约按秒续期、执行期限按步骤成本独立设置（`timeout_seconds`），二者不能混为一谈：
**租约失效只说明监督进程失联，不等于计算停止**。把 lease 改成一小时不是长任务支持。
真正的小时级任务需要：

- **分块 checkpoint**（P1 的 B01）：stylize_frames 以帧块为单位持久化输入指纹与已完成块摘要，续跑只复用已校验块，控制重复 GPU 成本。当前 P0 只做**步骤边界恢复**，单步中断整步重跑。
- 心跳（监督进程存活）与进展（业务推进）分离：前者续租，后者来自子进程 `progress.json`。
- 避免长事务与常驻 HTTP 连接；产物持久化到本地目录而非容器临时盘。
- 事件分页（`after_id`）消费、日志与磁盘容量管理、孤儿产物清理（当前 P0 保留孤儿供排查，未自动清理）。
- 数据库在线备份要考虑 WAL，不能只复制活跃的 `.db` 文件。

## 6. GPU：模拟槽位与物理边界

P0 的 `resource_slots` 持久化 CPU 槽位与一个 `mock-gpu-0`，**实际限制同时有效领取的 GPU attempt 数 ≤1**
（测试 `test_a07_two_workers_two_jobs_gpu_mutex` 采样验证 holder 峰值）。CPU 与 GPU 分池，等 GPU 不阻塞 CPU 步骤。

必须如实说明的边界：

- 这是**数据库逻辑预约**，不是真实显卡隔离；不采集 utilization / 显存 / 温度，界面标注「未采集 / MOCK」。
- **租约过期不保证旧 CUDA 进程消失**。真实 GPU 调度需要节点侧执行代理、可靠的进程/容器终止与退出确认，
  确认释放后再分配；设备失联应隔离并标记 UNKNOWN 而非立即重新分配。
- 生产还需要 GPU 型号/显存约束、OOM 分类与降级、模型加载时间、DCGM/NVML 指标、资源配额与等待公平性。

## 7. 边界（P0 不做）

通用 DAG 编辑器、登录权限、任意文件上传与外部 URL 抓取、跨机器 Worker、真实 GPU 调度、
跨任务全局缓存、WebSocket/SSE、暂停/取消、真实视频模型推理。
`demo_fault` 仅在本地/演示开启；`ANIMFLOW_FAULTS_ENABLED=0` 可关闭，接口只接受受限预设，不开放任意 kill 或代码执行。

## 8. 真实模型适配（2026-11-01）

`plan_motion` 按持久化输入选择 MOCK / DeepSeek，HTTP 调用只在步骤子进程里进行，不阻塞监督进程续租。
每 attempt 一个 HTTP 请求，无内部重试；网络读超时 35 秒，步骤墙钟期限 60 秒由监督进程强制结束兜底。
缺配置/鉴权/额度错误不可重试；429、5xx、网络故障、空/截断/Schema 非法输出可按原预算重试。
真实模式失败从不伪造成功或降级 MOCK。只接受有限运动原语/颜色/数值，不执行模型生成代码。

`motion_plan.json` 包含校验后的方案、provider/model/request_id/prompt_version/latency_ms/usage；不存 API Key、HTTP headers 或推理过程。
Key 从本地被忽略的 `.env` 加载到环境变量，仅留在进程内。新输入包含 mode/model/brief，提示词版本进入指纹。
Worker 下游指纹额外包含上游实际 artifact SHA-256，避免非确定模型仅有相同输入血缘却没有内容身份。
管线版本升为 1.1.0，旧版未完成任务明确失败，不对旧任务静默应用新渲染逻辑。

已提交的成功规划在下游失败/重启后复用，因此不会再次消耗 token。API 响应丢失、调用后数据库发布前崩溃仍可能重投并计费，
这是外部服务副作用，attempt fencing 不能撤销已发生的消费。不声称模型推理 exactly-once。
DeepSeek request_id 仅作观测关联，不视为可用来恢复查询的外部任务 ID。
运行时 token 是 provider 返回的实际应用用量，与本项目 AI coding 工具消耗的 token 分开统计。
