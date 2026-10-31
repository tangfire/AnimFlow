# 验收矩阵：把恢复能力变成证据

P0 是提交最低门槛。测试不要求逐字照搬文件名，但行为不可省略。需要同时有状态机测试和真实跨进程集成测试；单靠 UI 截图或 mock 掉数据库的单测不够。

## 测试设施

- 每个测试用独立临时 db/artifacts，不污染演示任务。
- 仓储层可注入时间，精确控制过期和退避；集成层使用真实时钟与短租约，不能假装真的运行过一小时。
- 等待采用有 deadline 的轮询与状态屏障，不靠固定长 sleep 猜结果。
- 记录自己启动的 Worker PID/进程句柄。Windows 和 Linux 使用合适的硬终止方法，不能 `killall python`。
- 用可控 barrier/fault point 卡在具体失效窗口；生产演示不暴露任意进程终止能力。
- 所有真实进程测试在 finally 中结束自己创建的进程，清理连接，避免测试挂死或泄漏。
- 证据至少含 task_id、step、旧/新 attempt_id、状态、产物摘要、相关事件。测试输出可保存为 JSON 或 Markdown 摘要。

## P0

| 编号 | 场景与操作 | 必须成立的断言 |
| --- | --- | --- |
| A01 | API 启动，Worker 未开，提交任务；再启动 Worker | POST 持久返回 202；未开 Worker 时任务可查询；随后六步骤成功、GIF 能解码且多帧、ZIP 有正确文件；同一任务每步成功 attempt 一次 |
| A02 | stylize_frames 前一次执行瞬时失败 | 第一次 attempt FAILED；步骤曾 RETRY_WAIT，next_run_at 持久化；第二次成功；上游成功步骤 attempt_count 不变；错误与两次历史可查 |
| A03 | stylize_frames 持续失败直到预算耗尽，然后人工 retry 并清注入 | 默认恰好 3 次总尝试，不是 4 次；下游 BLOCKED；任务在在途结束后 FAILED；retry_round 增加、旧历史保留、成功上游不重算；最后成功；RUNNING/SUCCEEDED 上 retry 返回 409 |
| A04 | 步骤运行中，硬杀自己启动的 Worker；另起 Worker | 租约过期后旧 attempt=WORKER_LOST；新 attempt_id 不同；成功上游 attempt/摘要不变；任务最终成功。记录恢复耗时，允许 lease+轮询/处理延迟，不宣称瞬间恢复 |
| A05 | A 持有 attempt 后人为推进测试时间使租约过期，B 领取新 attempt，A 尝试 heartbeat/complete/release | A 的三种写入均被拒；B 的状态、资源 holder、artifact 不变；旧目录不能被当作成功结果。额外验证“过期但尚未被回收”的 A 也不能提交 |
| A06 | 子进程卡住，监督进程仍在续心跳 | deadline 到期触发 TIMED_OUT，子进程被结束；GPU 槽释放且进入有界重试；其他任务仍可查询与推进。区分业务无进展与 Worker 失联 |
| A07 | 两 Worker 竞争同一任务/就绪步骤 | 同一步骤不能同时有两个有效 claim；attempt_no 唯一；依赖未成功不得执行。两个不同步骤可以按资源配置并行，不出现数据库死锁或无界 busy 循环 |
| A08 | 两任务争抢一个 mock GPU 槽，同时有可执行 CPU 步骤 | 有效 GPU holder 数始终 ≤1；等待 GPU 不消费 attempt；CPU 工作能推进；GPU 完成、超时与失联均可回收。此断言只覆盖逻辑预约，不覆盖真实 CUDA 进程 |
| A09 | 产物已写完但成功事务前硬杀 Worker | 旧文件未发布，不出现 SUCCEEDED/可下载记录；恢复后新 attempt 成功；旧文件不得覆写新结果。另验证数据库成功提交后重启不重跑该步骤 |
| A10 | API 重启、浏览器刷新、Worker 继续 | Worker 不受 API 重启影响；历史及进度从数据库读取；页面重新打开能定位同一任务；events after_id 不重复不漏，超出一页可继续翻页 |
| A11 | 人工 retry/恢复前删除或篡改一个成功上游产物 | 明确 ARTIFACT_INVALID，不能继续把该产物当有效依赖；失败/事件可查；下载损坏产物返回清晰错误。按 P0 保守策略新建任务，不假装自动重算依赖闭包 |
| A12 | 非法尺寸/帧数、未知任务/产物、恶意路径；数据库持久与冷启动 | 422/404/409 有规范语义；不越界读文件；启动不依赖残留内存；按 README 的新环境可跑通；重启不清空历史 |
| A13 | claim/complete 事务中注入异常 | 事务回滚后没有半套状态：无占槽却没 attempt、无步骤成功却没 artifact/event；attempt/step/job/resource 状态自洽 |
| A14 | 模拟不可重试错误、claim 后子进程启动失败、执行子进程异常退出 | 不可重试直接终止该步；启动失败/异常退出有明确错误及预算；claim 后监督进程死于启动前可由过期恢复；无永久资源泄漏 |

注意 A11 的损坏检测要发生在新依赖执行前。不能只验证下载接口，也不能在检查损坏前就把下游跑完。

## P1，可选且必须单独标识

| 编号 | 扩展 | 验收 |
| --- | --- | --- |
| B01 | stylize_frames 分块 checkpoint | 第 k 块后杀进程；已校验块复用，未完成块重跑；参数/版本变化不复用旧 checkpoint |
| B02 | 跨任务内容缓存 | 同业务输入命中；style/seed/步骤版本/上游摘要变化 miss；损坏产物 miss；命中不占 GPU；不能命中失败或未发布的 attempt |
| B03 | Idempotency-Key | 相同键相同输入返回原任务；相同键不同输入 409；并发提交不创建两份 |
| B04 | Docker/CI/公网 | 不依赖临时磁盘持久化；重启仍可查任务；若无认证则限制预设和速率、不公开危险故障注入；展示真实可访问 URL |

## 测试报告写法

每项填：实际测试名、运行命令、运行环境、结果 PASS/FAIL/NOT RUN、证据路径、限制。明确区分自动化测试与手工演示。记录测试数量必须来自真实输出；token/工时不从测试数量推算。

至少保留一段恢复时间线，例如：已完成上游 → stylize attempt A RUNNING → 硬杀 → lease 过期 → A WORKER_LOST → attempt B RUNNING → B SUCCEEDED → 最终成功。时间与 ID 必须来自实际执行，不复制示例伪造数据。

不要求写“从未失败”。记录真实遇到的错误、最初误判、修复及回归，区分“已修复缺陷”和“仍存在的限制”。
