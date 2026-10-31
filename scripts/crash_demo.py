"""硬杀恢复演示：只结束本脚本创建的进程（绝不碰其他进程）。

流程：启动 Worker → 提交慢任务 → stylize RUNNING 时硬杀 Worker 整棵进程树
→ 观察租约仍有效（attempt 仍 RUNNING）→ 另起 Worker → 租约过期回收
（WORKER_LOST）→ 新 attempt → 任务最终成功。打印真实恢复时间线。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/crash_demo.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "crash-run"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from demo import kill_tree, wait_until  # noqa: E402,F401


def spawn(cmd, env, log_name):
    """本脚本自己的进程与日志目录（只管理自己创建的进程）。"""
    DATA.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=open(DATA / log_name, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    from app.config import load_config, ensure_dirs
    from app.repository import bootstrap
    from app import pipeline

    env = os.environ.copy()
    env.update({
        "ANIMFLOW_DATA_DIR": str(DATA),
        "ANIMFLOW_POLL_S": "0.2",
        "ANIMFLOW_HEARTBEAT_S": "0.5",
        "ANIMFLOW_LEASE_S": "2",
        "ANIMFLOW_STEP_SPEED": "8",
        "PYTHONPATH": str(ROOT),
    })
    os.environ.update({k: v for k, v in env.items() if k.startswith("ANIMFLOW_")})
    cfg = load_config()
    ensure_dirs(cfg)
    repo = bootstrap(cfg.db_path, cfg.artifacts_dir, lease_seconds=cfg.lease_seconds)

    # 慢 stylize：给硬杀留出窗口
    job = repo.create_job({**pipeline.DEFAULT_INPUT, "seed": 99,
                           "demo_fault": {"slow": {"stylize_frames": 8}}})
    jid = job["id"]
    print(f"任务 {jid[:8]} 已入库（此时无 Worker）")

    proc = spawn([sys.executable, "-m", "app.worker"], env, "worker-1.out")
    print(f"[Worker-1] pid={proc.pid}")
    killed_at = None
    old_attempt = None
    try:
        def stylize_running():
            s = repo.step_row(jid, "stylize_frames")
            return s and s["status"] == "RUNNING" and s["active_attempt_id"]

        wait_until(stylize_running, 60, "stylize 未进入 RUNNING")
        s = repo.step_row(jid, "stylize_frames")
        old_attempt = s["active_attempt_id"]
        print(f"stylize RUNNING，active_attempt={old_attempt[:8]}（GPU 槽已预约）")
        print("硬杀 Worker-1（taskkill /F /T，只杀本脚本创建的 pid）…")
        killed_at = time.time()
        kill_tree(proc)
        # 立刻查看：租约未过期前 attempt 仍 RUNNING（新 Worker 也还没回收）
        a = repo.attempt_rows(s["id"])[0]
        print(f"硬杀后瞬间: attempt 状态={a['status']}（租约 {int(a['lease_expires_at'] - time.time())}s 后过期）")
    finally:
        kill_tree(proc)

    proc2 = spawn([sys.executable, "-m", "app.worker"], env, "worker-2.out")
    print(f"[Worker-2] pid={proc2.pid}（另一进程，接手恢复）")
    try:
        wait_until(lambda: repo.get_job_detail(jid)["status"] == "SUCCEEDED",
                   120, "恢复后任务未成功")
    finally:
        kill_tree(proc2)

    detail = repo.get_job_detail(jid)
    attempts = repo.attempt_rows(repo.step_row(jid, "stylize_frames")["id"])
    evs = repo.get_events(jid, limit=500)["events"]
    lost = next(e for e in evs if e["type"] == "ATTEMPT_WORKER_LOST")
    recovered_at = lost["created_at"]

    print("\n===== 恢复时间线（全部来自数据库实测）=====")
    for e in evs:
        mark = " ◀ 硬杀发生在这之前" if e["id"] == lost["id"] else ""
        print(f"  {time.strftime('%H:%M:%S', time.localtime(e['created_at']))} "
              f"{e['type']:<22} {json.dumps(e['payload'], ensure_ascii=False)[:90]}{mark}")
    print("===== 断言 =====")
    ok = True
    checks = [
        ("旧 attempt = WORKER_LOST", attempts[0]["status"] == "WORKER_LOST"),
        ("新 attempt_id 不同", attempts[-1]["id"] != old_attempt),
        ("新 attempt 成功", attempts[-1]["status"] == "SUCCEEDED"),
        ("最终任务 SUCCEEDED", detail["status"] == "SUCCEEDED"),
        ("GPU 槽已释放", all(v is None for v in repo.slot_holders().values())),
    ]
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    up = repo.step_row(jid, "extract_frames")
    print(f"  上游 extract_frames: attempts={up['attempt_count']}（成功上游未重跑）")
    print(f"\n恢复耗时: 硬杀@{time.strftime('%H:%M:%S', time.localtime(killed_at))} → "
          f"WORKER_LOST@{time.strftime('%H:%M:%S', time.localtime(recovered_at))} = "
          f"{recovered_at - killed_at:.2f}s（= 租约剩余 + 轮询/处理延迟，非瞬时）")
    print("说明：这是 at-least-once 语义 + 步骤边界恢复；旧目录文件保留为孤儿，不发布。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
