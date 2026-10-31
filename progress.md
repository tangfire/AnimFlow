# progress.md

## 状态
进行中 — 阶段 4/5（破坏性实验复核与交付文档）。

## 完成内容

### 阶段 0（环境与契约）✅
- venv + 依赖锁定：fastapi==0.162.0、uvicorn==0.54.0、pillow==13.0.0、pydantic==2.14.2、pytest==9.2.0、httpx==0.28.1。
- config/domain/pipeline/db：WAL+外键+busy_timeout、显式 BEGIN IMMEDIATE、幂等初始化（含槽位）。
- 退出门槛：空库初始化两次安全；新任务含全部 6 步；非法 DAG 被拒。

### 阶段 1（持久化与状态机）✅
- repository：单事务建任务、claim（依赖/产物预检/退避/资源同事务）、heartbeat 限频进度、
  complete（事务外校验文件→租约保护发布）、fail/timeout/recover、fail-fast 聚合、人工 retry、事件分页。
- `tests/test_state_machine.py` 20 项通过：恰 3 次预算、旧 attempt 三种写入全拒（含过期未回收）、
  holder 条件更新不误释放、事务注入异常无半套状态、等待资源不消耗 attempt、指纹稳定性。

### 阶段 2（Worker 与真实产物）✅
- 独立 Worker（spawn 子进程、非阻塞监督循环：回收结果→心跳→超时→过期回收→领取→启动）。
- 六步真实产物：PNG 帧 → 风格变换 → 真实 GIF → 真实 ZIP，均有 manifest+SHA-256。
- 故障注入预设：fail_first_n / always_fail / nonretryable / exit_early / timeout /
  crash_after_files（失效窗口 barrier）/ slow / fail_launch；只影响指定任务指定步骤。
- `tests/test_recovery.py` 12 项通过（真实硬杀）：A01 端到端、A04 硬杀恢复（旧 WORKER_LOST、
  新 attempt、上游不重跑、恢复耗时实测 <15s）、A06 超时（心跳持续、3 次 TIMED_OUT、GPU 无泄漏）、
  A07 双 Worker 无重复领取、A08 GPU holder 采样 ≤1、A09 写完未提交硬杀（孤儿文件不发布、新 a2 目录）、
  成功后重启不重跑、A14 启动失败/异常退出/claim 后死亡、A11 损坏阻断人工 retry、事件分页不重不漏。

### 阶段 3（API 与看板）✅
- `app/main.py`：plan.md §10 全部接口 + 统一错误信封 + 下载按 artifact_id 查表全量摘要校验
  （损坏→409+ARTIFACT_INVALID 事件）+ healthz + 静态页挂载。
- `app/static/`：任务列表/预设创建/步骤卡（等待原因、进度、租约、错误）/attempt 表/事件增量时间线/
  资源槽位/GIF 预览/ZIP 下载/人工重试；URL ?job_id= 恢复；断网降级轮询。
- `tests/test_api.py` 6 项通过：A01 API 层（202→Worker 消费→GIF 多帧解码→ZIP）、A03/A11 HTTP 语义
  （409/422/404）、A10 uvicorn 子进程重启后 Worker 继续 + 事件分页不重不漏、A12 校验与路径约束。

## 实际运行命令及结果
- `.venv/Scripts/python.exe -m pytest tests/ -q` → **38 passed in 97.54s**（2026-10-31）。
- 阶段 2 提交：05f6580（阶段 0-1）。

## 待解决问题
- 无。

## 下一步
- scripts/demo.py、scripts/crash_demo.py；docs 六件套 + README + SUBMISSION；
  README 冷启动；最终汇报。
