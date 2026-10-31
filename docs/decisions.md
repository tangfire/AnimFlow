# 设计决策记录

必要调整都在这里说明原因、行为变化与替代验证。未实现的能力明确标注为「未实现」。

## D1. 每次操作新建数据库连接，而非长连接
**原因**：FastAPI 同步端点在线程池中运行，SQLite 连接跨线程共享需要额外锁定，容易出错。
**行为变化**：`Repository._conn()` 每次打开新连接（`isolation_level=None`，由 `write_tx` 显式 `BEGIN IMMEDIATE`）。
**权衡**：连接开销换取线程安全与简单性；单机低并发下可接受。
**替代验证**：并发领取测试 `test_two_repos_cannot_claim_same_gpu_step`、`test_a07_two_workers_one_job_no_double_claim`。

## D2. claim 事务内先插 attempt 再占槽（外键顺序）
**原因**：`resource_slots.holder_attempt_id` 外键引用 `attempts(id)`；若先占槽会触发 `FOREIGN KEY constraint failed`。
**行为变化**：顺序改为 插 attempt → 条件占槽 → 更新步骤 → 事件 → 提交。占槽失败则整事务回滚。
**发现方式**：阶段 1 首次运行 `test_claim_creates_attempt_and_occupies_slot` 时外键报错。
**替代验证**：`test_a13_claim_tx_rollback_no_partial_state`（中途注入异常 → 无半套状态）。

## D3. 产物完整性校验放事务外，发布放事务内
**原因**：SHA-256 校验是磁盘 IO，放进 `BEGIN IMMEDIATE` 会拉长写锁持有时间。
**行为变化**：`complete_attempt` 先在事务外核对清单/尺寸/摘要，再进入租约保护的发布事务。
**权衡**：文件与数据库之间存在窗口（见 `architecture.md` 第 4 节），由 attempt 身份与租约条件更新兜底，
旧 attempt 无法发布、孤儿文件不被当作成功。
**替代验证**：`test_a09_crash_after_files_before_publish`（真实硬杀）。

## D4. 人工 retry 的 fatal 先提交、异常在事务外抛出
**原因**：最初在事务内 `raise ArtifactInvalid`，导致记录 `ARTIFACT_INVALID` 的写入被回滚，用户看不到失败原因。
**行为变化**：预检失败时先提交 fatal + 事件，事务结束后再抛 409。
**发现方式**：`test_a11_corrupt_upstream_blocks_manual_retry` 断言 `fatal_error_code` 时发现为 `None`。
**替代验证**：该测试修复后通过；`test_a11_corrupt_artifact_download_and_retry_409`（HTTP 层）。

## D5. demo_fault 内嵌于 params 时自动拆分
**原因**：测试与脚本把故障配置和业务参数放在同一个 JSON 里传给 `create_job`。
**行为变化**：`create_job` 若发现 `params.demo_fault`，拆出为故障配置，不计入 `input_hash`（业务指纹）。
**原因补充**：故障注入不应影响业务指纹，否则同一业务输入会因故障不同而 fingerprint 不同。

## D6. 原子写在 Windows 上对 `os.replace` 做有界重试
**原因**：Windows 上目标文件被并发读者打开时，`os.replace` 报 `WinError 5`（拒绝访问）。
监督进程读 `progress.json` 与子进程写它会撞车。
**行为变化**：`atomic_write_bytes` 对 `os.replace` 做 10 次退避重试（约 0.5s）。
**发现方式**：冒烟测试中 render_preview 首次尝试因 `PermissionError: [WinError 5]` 失败。
**替代验证**：重跑冒烟测试该步骤一次成功；`test_a01_worker_end_to_end`。

## D7. 事件用全局自增 id 做增量消费
**原因**：`plan.md` 要求 `after_id` 增量且不重不漏。
**行为变化**：`events.id` 为 `INTEGER PRIMARY KEY AUTOINCREMENT`（跨任务全局递增），接口按 `job_id + id > after_id` 过滤。
**消费规则**：客户端始终以返回的 `next_after_id` 继续；空页也返回该值；id 升序、不重复不遗漏。
**替代验证**：`test_events_pagination_no_gap_no_dup`、`test_a10_api_restart_worker_continues`（HTTP 分页）。

## D8. 任务详情做全量产物摘要校验
**原因**：最初只查文件存在性，导致目录内单个 PNG 被删仍显示 `artifact_health: valid`。
**行为变化**：`artifact_health_for_job` 对每个已发布产物做完整 SHA-256 校验。
**权衡**：演示产物为 KB 级，全量校验可接受；生产应改为抽样或缓存。
**发现方式**：`test_a11` 断言 health 为 invalid 时最初得到 valid。

## D9. 未实现 / 明确标注的能力
- **分块 checkpoint（B01）**：未实现。P0 仅步骤边界恢复，单步中断整步重跑。
- **跨任务内容缓存（B02）**：未实现。P0 复用范围仅限同一任务恢复/人工重试时保留有效成功步骤。
- **Idempotency-Key（B03）**：未实现。
- **Docker / CI / 公网部署（B04）**：未实现（本地项目优先；发布步骤见 README）。
- **孤儿产物自动清理**：未实现。崩溃留下的旧 attempt 目录保留供排查。
- **真实 GPU 隔离与指标采集**：未实现，见 `architecture.md` 第 6 节。

## D10. 平铺与嵌套两种任务输入格式都支持，混用明确拒绝
**原因**：外部复核发现 `POST /api/jobs {"width":999}` 返回 202 且静默保存默认值——
Pydantic v2 的 `model_dump()` 会把 extra 键拍平到顶层，原代码读 `data["__pydantic_extra__"]`
恒为 None，平铺参数被整体丢弃。
**行为变化**：改为直接读 `body.__pydantic_extra__`；平铺格式的业务参数与 `demo_fault`
都被正确解析并走 `normalize_input`/`normalize_fault` 校验（非法值 422、未知键 422）；
`params` 嵌套与平铺混用时明确 422，不猜测语义。
**替代验证**：`test_api.py::test_flat_input_parsed_and_validated`。

## D11. 持有权检查在 BEGIN IMMEDIATE 之后取时
**原因**：外部复核发现 complete 在文件校验前采样 `now`，发布事务沿用该时间，
校验期间租约过期仍会发布；heartbeat/fail/release/claim 同类。
**行为变化**：所有持有权路径（heartbeat/complete/fail/timeout/recover/release/manual_retry）
与决策路径（claim 的 next_run_at 判断、聚合）统一在拿到写锁后取当前时间；
方法入口到写锁之间的时间流逝（文件校验、等待锁、调度延迟）全部计入租约。
过期操作被拒绝：不发布结果、不续活租约、不释放资源，交由回收路径记 WORKER_LOST。
**替代验证**：3 项回归测试（verify 期间过期 + 两个「等写锁期间过期」）。
