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

## 阶段 4（破坏性实验复核）✅
- A01–A14 全部有实测证据，无跳过、无 xfail。证据：`docs/evidence/pytest-full-2026-10-31.txt`、
  `docs/evidence/crash-demo-2026-10-31.txt`（硬杀恢复 2.07s，5 条断言全 PASS）。
- 演示脚本实测通过：`scripts/crash_demo.py`（5 断言 PASS）、`scripts/demo.py`（四场景，GPU 峰值=1）。

## 阶段 5（说明与交付）✅
- README、SUBMISSION、docs/{architecture,decisions,test-report,demo-script,ai-usage,worklog}。
- 展示 GIF `docs/assets/demo-neon-seed7.gif` 及生成说明。
- 提交：`9161209`(阶段2-3)、`60c01d5`(交付文档)、`47ef20d`(cp936 修复)。

## 冷启动发现并修复的缺陷
- `pip install -r requirements-dev.txt` 在中文 Windows（cp936）下因 requirements 内中文注释报
  `UnicodeDecodeError: 'gbk'`。修复：requirements 改为纯 ASCII 注释（`47ef20d`）。

## 冷启动复验 ✅（2026-10-31）
- 新临时目录克隆 `47ef20d` → 全新 venv → `pip install -r requirements-dev.txt`（exit 0）→
  `pytest tests/ -q` → **38 passed in 98.92s**。
- 按 README 启动 API+Worker → POST 任务（watercolor/seed=2026）→ 六步各 1 次尝试 SUCCEEDED、
  health=valid → 下载 GIF 12 帧 256×256 可解码、ZIP 完整可解压。进程已清理。

## 正确性修复轮（外部复核问题）✅（2026-10-31）
- 问题1 租约过早采样时间：所有持有权路径改为 `BEGIN IMMEDIATE` 之后取时；3 项回归
  （verify 期间过期 / 等写锁期间过期的 heartbeat 与 fail）先失败后通过。
- 问题2 领取路径任务级错误未聚合：每个 fatal 点同事务聚合；上游产物收集移到占槽之前；
  3 项回归（损坏→FAILED+BLOCKED、版本不支持→QUEUED 转 FAILED、有在途排空后 FAILED）通过。
- 问题3 平铺输入被静默忽略：直接读 `body.__pydantic_extra__`；平铺正确解析校验、
  混用 422；`test_flat_input_parsed_and_validated` 通过。
- 看板串台防护：selectJob 递增 jobToken，pollEvents/renderAttempts 丢弃过期响应。
- 全量回归：**45 passed in 97.93s**（证据已覆盖 docs/evidence/pytest-full-2026-10-31.txt）。
- 浏览器实测（Playwright+真实 Chromium，11/11 PASS）：GIF 解码、ZIP 下载、
  失败→人工重试、~120ms×10 快速切换不串台；证据 docs/evidence/browser-check-2026-10-31.txt。
- 文档同步：test-report（45 项/新缺陷/浏览器节）、decisions D10/D11、README 双输入格式、
  demo-script §6 录制脚本。

## 待解决问题
- 无。

## 下一步
- 向用户汇报；个人信息/工时/token 待本人补充。
