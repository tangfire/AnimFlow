# 题 D：可观测、可恢复的动画素材处理管线

源码：https://github.com/tangfire/AnimFlow
演示视频：[MP4，3 分 26 秒，中文合成旁白与说明字幕](https://github.com/tangfire/AnimFlow/blob/main/docs/assets/AnimFlow-demo-narrated.mp4)（文件页可下载视频；画面为实际浏览器录制，非本人现场口述）。
运行与验收：[README.md](README.md)、[测试报告](docs/test-report.md)

## 实现方式与时间规划

技术栈：Python 3.12 + FastAPI + 标准库 sqlite3（WAL）+ 独立 Worker（multiprocessing spawn 子进程）+
Pillow + 原生 HTML/JS。运行组件为 API、Worker 监督循环、步骤子进程、同一 SQLite 数据库与产物目录。

**动作规划可真实调用 DeepSeek，也保留离线 MOCK**：`plan_motion` 产生受 Schema 校验的 JSON，
下游 Pillow 渲染实际应用运动与配色。GPU 为 `mock-gpu-0` 逻辑槽位，无真实显卡隔离；不是模型直接生成视频。
真实调用的模型/请求 ID/耗时/token usage 随规划产物保存并在看板展示。
**状态机、跨进程执行、进程硬杀与重启恢复、GIF/ZIP 产物
与状态查询是真实实现**，全部有自动化测试与实测证据（[docs/test-report.md](docs/test-report.md)）。

按本人回顾，项目实际历时约 **2 小时 30 分钟**，为包含智能体执行与等待的总历时，
不是个人独立编码工时，也不是逐阶段精确计时记录。时间安排按交付优先级推进：
先持久化内核与恢复实验，再完成 Worker、API/看板，随后补故障复核、真实模型接入和演示材料。
没有可核对的逐阶段实际工时；`exec.md` 中的时间是前期实施建议，不作为实际耗时。

## 长任务、状态管理与失败恢复

API 只做入队与查询，不在请求里计算；Worker 独立进程监督执行。所有业务状态在 SQLite：
任务/步骤/attempt 状态机，claim 在 `BEGIN IMMEDIATE` 单事务内校验依赖、产物完整性、重试时间与
资源槽并建立 attempt；每次执行有唯一 `attempt_id`，心跳/进度/结束/释放资源必须匹配
`active_attempt_id`、owner 且租约有效，否则拒绝并记录原因。执行期限（按步骤成本）与租约
（秒级续期）分离，两者不能混为一谈。重试有界（默认每轮 3 次，退避 `min(2^(n-1),30)` 秒），
出现最终失败后 fail-fast：不再领取新步骤，在途排空后任务置 `FAILED`、其余步骤 `BLOCKED`。
**一句话边界：这是 at-least-once（至少一次）语义 + 步骤边界恢复——崩溃后计算可能重做，
靠身份校验、产物隔离与事务提交保证只有当前有效 attempt 能发布结果；单步中断整步重跑。**

硬杀恢复实测证据：[docs/evidence/crash-demo-2026-10-31.txt](docs/evidence/crash-demo-2026-10-31.txt)。
时间线：上游成功 → stylize attempt#1 RUNNING（已预约 GPU）→ `taskkill /F /T` 硬杀 Worker →
租约过期 → attempt#1 `WORKER_LOST` → attempt#2 领取并 `SUCCEEDED` → 任务 `SUCCEEDED`；
识别失联耗时 2.07s（crash_demo 短租约 2s 配置下的实测值，仅覆盖「识别失联」一段；
「开始重试」另加退避、「任务最终完成」另加剩余步骤时间；默认租约 10s 时识别更晚，非瞬时恢复）；
**成功上游 `extract_frames` 的 attempt 数保持 1，未重跑**；
旧 attempt 的心跳/提交/释放一律被拒（`tests/test_state_machine.py::test_a05_*`）。

文件与数据库双写间隙：产物先原子落盘并校验，发布在受租约保护的事务中；「文件写完但事务未提交」
时硬杀，旧文件成为孤儿目录、不发布，新 attempt 写新目录（`test_a09_crash_after_files_before_publish`）；
数据库已提交后重启不重跑（`test_a09_db_committed_then_restart_no_rerun`）。详见
[docs/architecture.md](docs/architecture.md) 第 4 节。

## 步骤与依赖

六步固定 DAG：素材检查 →（生成帧 ∥ 动作规划）→ 风格化（模拟 GPU）→ GIF 预览 → 打包 ZIP。
划分依据：产物稳定可验证（PNG/GIF/ZIP + SHA-256 清单）、资源不同（CPU/GPU 分池）、
失败域不同（帧生成失败不影响规划）、可单独校验与复用。这是**固定 DAG 实现，不是通用工作流设计器**；
DAG 与版本随任务入库形成快照，运行中任务不随代码变化而静默变更，Worker 遇到不支持版本明确报任务级错误。

## 哪些状态持久化，为什么

任务输入/业务哈希/pipeline 版本与 DAG 快照；步骤依赖/状态/资源/重试预算/下次执行时间/进度/输出指纹；
attempt 的身份/owner/租约/心跳/执行期限/终态/工作目录；产物的相对路径/SHA-256/尺寸/清单；
资源槽 holder；业务事件（自增 id，增量分页消费）。
日志、心跳与事件是三回事：心跳不逐条写事件，事件是审计历史，UI 完全依赖 API 查询而非浏览器内存，
刷新或 API 重启后从数据库恢复展示。

## GPU 与小时级任务

模拟槽位实做了：持久化 CPU×2 + `mock-gpu-0`，**实际限制同时有效领取的 GPU attempt 数 ≤1**
（双 Worker 双任务采样峰值 = 1）；等待 GPU 不消耗 attempt；完成/超时/失联回收均释放。
未采集：真实 utilization、显存、温度（界面标注「未采集 / MOCK」）。
**租约失效不等于 CUDA 进程消失**——真实 GPU 调度需要节点执行代理、可靠的进程/容器终止与退出确认，
确认释放后再分配，设备失联应隔离标记。小时级任务需要分块 checkpoint（P0 未实现，仅步骤边界恢复）、
外部任务 ID 与幂等键防重复计费、持久产物与监控，而不是延长 HTTP 请求。

## 使用 AI coding 工具与 token

使用 **Codex 与 Claude Code（接入 MiMo）**。Codex 协助规格设计、验收拆解、代码复核、
真实 DeepSeek 适配和演示材料制作；Claude Code / MiMo 依据规格完成初版编码、调试和交付文档。
具体模型版本未做完整记录，不把工具名称等同于底层模型名称。

**开发 token 未获得跨工具的完整用量统计，无法可靠给出总数或数值估算。**
没有用代码行数、测试数或应用运行消耗反推开发 token。从工作内容判断，状态机/租约正确性、
故障恢复测试与多轮排错是较主要的交互内容；这是定性判断，没有用量占比支撑。
应用内 DeepSeek 用量另有真实记录（如首次请求 672 tokens），不计作编程工具用量。
详见 [AI 使用记录](docs/ai-usage.md)。

## 本人投入最多的部分

本人参与主要集中在需求沟通、功能范围与交付形式的确认，以及使用和协调上述 AI 工具推进项目。
例如选择公开源码交付、追加真实模型接入，并要求可提交的演示视频与中文旁白。
本人没有独立完成主要编码与故障排查，这些工作以及测试执行、录制主要由智能体完成。

排除 LLM 直接执行的部分后，个人投入较少，未单独计时，因此无法可靠比较哪一项花费最多，
也不将智能体运行时间计为本人手工实践时间。上面的自动化验证与演示是实际执行证据，
不代表本人已经独立复现全部实验。个人与 AI 的分工见 [工作日志](docs/worklog.md)。

## 失败案例与限制

开发过程中记录的案例一：**故障注入从未生效**。触发条件：测试把 `demo_fault` 与业务参数放在同一 JSON 传给
`create_job`。当时现象：`always_fail` 等场景的任务竟然直接成功，相关测试超时。根因：`create_job`
没有拆分内嵌的 `demo_fault`，它被当作未知业务参数丢弃，Worker 收到的故障配置恒为空。
修复：`create_job` 自动拆出内嵌 `demo_fault`，且不计入业务指纹（`input_hash`）。
验证：此前 5 个失败的集成测试（A06/A09/A11/A14 相关）修复后全部通过，
全量 38 项通过（**当时结果**；其后外部复核又发现 3 个问题并补 7 项回归，恢复套件增至 45 项；模型接入后共 75 项，
见 [docs/test-report.md](docs/test-report.md) §2/§3.1）。

开发过程中记录的案例二：**人工重试的失败原因丢失**。触发条件：损坏上游产物后调用 retry。
当时现象：接口返回 409，但 `fatal_error_code` 为 `None`，事件里没有 `ARTIFACT_INVALID`。
根因：在事务内 `raise ArtifactInvalid`，异常把刚写入的 fatal 与事件一起回滚。
修复：先提交 fatal 与事件，事务外再抛 409（`docs/decisions.md` D4）。
验证：`test_a11_corrupt_upstream_blocks_manual_retry` 断言 fatal 与事件均存在。
（完整缺陷清单见 [docs/test-report.md](docs/test-report.md) 第 6 节。）

未解决限制：单机 SQLite（不适合跨主机网络文件系统）；GPU 为逻辑槽位、非真实隔离；
P0 仅步骤边界恢复，无分帧 checkpoint；无跨任务缓存；无登录/限流/公网部署；
无暂停取消与 WebSocket 推送；崩溃孤儿产物目录保留未自动清理；外部副作用（真实模型计费）不在保证范围。
哪些已实现/已测试、哪些只讨论见 [docs/decisions.md](docs/decisions.md) D9。


个人身份、入职日期与简历通过提交邮件提供，不放入公开源码仓库。
