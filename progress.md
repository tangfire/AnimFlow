# progress.md

## 状态
进行中 — 阶段 2（Worker 与真实产物）。

## 完成内容

### 阶段 0（环境与契约）✅
- venv（.venv）+ 依赖锁定：fastapi==0.162.0、uvicorn==0.54.0、pillow==13.0.0、pydantic==2.14.2、pytest==9.2.0、httpx==0.28.1。
- `app/config.py`（ANIMFLOW_* 环境变量、lease>=3*heartbeat 校验）、`app/domain.py`（状态/错误码）、`app/pipeline.py`（六步 DAG、唯一性/依赖/无环检查、输入边界、指纹、退避公式）、`app/db.py`（WAL+外键+busy_timeout、显式 BEGIN IMMEDIATE、幂等初始化含槽位）。
- 退出门槛实测通过：空库初始化两次安全；新任务含全部 6 步；非法 DAG（环/缺依赖/重名）被拒。

### 阶段 1（持久化与状态机）✅
- `app/repository.py`：单事务建任务、公平扫描 claim（依赖/产物预检/重试时间/资源同事务校验，先插 attempt 再条件占槽）、heartbeat 限频进度、complete（事务外校验文件→租约保护发布）、fail/retry、超时、过期租约回收、fail-fast 聚合（在途排空→FAILED+BLOCKED）、人工 retry（预检+OUTPUT_REUSED+保留历史）、事件与分页查询。
- `tests/test_state_machine.py` 20 个测试全部通过，覆盖 A02/A03/A05/A07/A08(单元)/A13/A14 核心事务：恰 3 次预算、旧 attempt 的 heartbeat/complete/release 全拒绝（含“过期未回收”与“holder 条件更新不能误释放新 holder”）、事务注入异常无半套状态、等待资源不消耗 attempt、指纹稳定性。

## 实际运行命令及结果
- `.venv/Scripts/python.exe -m pytest tests/test_state_machine.py -q` → 20 passed（2026-10-31）。

## 待解决问题
- 无。

## 下一步
- 阶段 2：真实子进程 Worker 集成测试（硬杀恢复 A04/A09、超时 A06、双 Worker A07、GPU 争抢 A08）。
