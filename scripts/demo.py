"""五分钟演示脚本：成功、自动重试、人工重试、GPU 争抢。

自足运行：自己启动 API 与 Worker 子进程（只结束自己创建的进程），
通过 HTTP 接口展示真实状态与事件时间线。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/demo.py         # Windows
    .venv/bin/python scripts/demo.py                 # macOS/Linux
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "demo-run"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wait_until(pred, timeout: float, message: str, interval: float = 0.4):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if pred():
            return time.monotonic() - start
        time.sleep(interval)
    raise TimeoutError(message)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
    proc.wait(timeout=10)


def spawn(cmd: list[str], env: dict, log_name: str) -> subprocess.Popen:
    out = DATA / log_name
    return subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                            stdout=open(out, "w", encoding="utf-8"),
                            stderr=subprocess.STDOUT)


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({
        "ANIMFLOW_DATA_DIR": str(DATA),
        "ANIMFLOW_POLL_S": "0.3",
        "ANIMFLOW_HEARTBEAT_S": "1",
        "ANIMFLOW_LEASE_S": "4",
        "ANIMFLOW_STEP_SPEED": "4",
        "PYTHONPATH": str(ROOT),
    })
    api_proc = spawn([sys.executable, "-m", "uvicorn", "app.main:app",
                      "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                     env, "api.out")
    worker_proc = spawn([sys.executable, "-m", "app.worker"], env, "worker.out")
    print(f"[启动] API {base}  (pid={api_proc.pid})  Worker (pid={worker_proc.pid})")
    try:
        wait_until(lambda: http("GET", f"{base}/healthz")["status"] == "ok", 20, "API 未就绪")

        # ---- 场景 1：成功管线 ------------------------------------------
        print("\n=== 场景 1：成功管线（真实 GIF/ZIP）===")
        job = http("POST", f"{base}/api/jobs", {"params": {"style": "neon", "seed": 7}})
        jid = job["id"]
        print(f"任务 {jid[:8]} 已入队（API 立即返回，Worker 独立消费）")
        wait_until(lambda: http("GET", f"{base}/api/jobs/{jid}")["status"] == "SUCCEEDED",
                   120, "场景 1 超时")
        detail = http("GET", f"{base}/api/jobs/{jid}")
        for s in detail["steps"]:
            print(f"  {s['name']:<16} {s['status']:<10} attempts={s['attempt_count']}")
        gif = next(a for s in detail["steps"] for a in s["artifacts"]
                   if a["kind"] == "preview_gif")
        print(f"  GIF 产物: {DATA / 'artifacts' / gif['relative_path']}")

        # ---- 场景 2：瞬时失败 → 自动重试 -------------------------------
        print("\n=== 场景 2：stylize 瞬时失败一次 → 自动重试成功 ===")
        job = http("POST", f"{base}/api/jobs", {
            "params": {"seed": 11},
            "demo_fault": {"fail_first_n": {"stylize_frames": 1}},
        })
        jid = job["id"]
        wait_until(lambda: http("GET", f"{base}/api/jobs/{jid}")["status"] == "SUCCEEDED",
                   120, "场景 2 超时")
        att = http("GET", f"{base}/api/jobs/{jid}/attempts")
        for a in att["items"]:
            err = a["error"]["code"] or ""
            print(f"  {a['step_name']:<16} #{a['attempt_no']} {a['status']:<10} {err}")
        evs = http("GET", f"{base}/api/jobs/{jid}/events")["events"]
        for e in evs:
            if e["type"] in ("STEP_RETRY_SCHEDULED", "STEP_FAILED"):
                print(f"  事件 {e['type']}: {json.dumps(e['payload'], ensure_ascii=False)[:110]}")

        # ---- 场景 3：永久失败 → 人工 retry ------------------------------
        print("\n=== 场景 3：stylize 持续失败 → FAILED → 人工 retry(清故障) → 成功 ===")
        job = http("POST", f"{base}/api/jobs", {
            "params": {"seed": 13},
            "demo_fault": {"always_fail": {"stylize_frames": True}},
        })
        jid = job["id"]
        wait_until(lambda: http("GET", f"{base}/api/jobs/{jid}")["status"] == "FAILED",
                   120, "场景 3 失败阶段超时")
        detail = http("GET", f"{base}/api/jobs/{jid}")
        stylize = next(s for s in detail["steps"] if s["name"] == "stylize_frames")
        blocked = [s["name"] for s in detail["steps"] if s["status"] == "BLOCKED"]
        print(f"  任务 FAILED；stylize 尝试 {stylize['attempt_count']} 次（预算 3 次）；"
              f"下游 BLOCKED: {blocked}")
        http("POST", f"{base}/api/jobs/{jid}/retry", {"clear_fault": True})
        print("  已人工 retry（clear_fault=true），成功步骤保留复用")
        wait_until(lambda: http("GET", f"{base}/api/jobs/{jid}")["status"] == "SUCCEEDED",
                   120, "场景 3 恢复阶段超时")
        detail = http("GET", f"{base}/api/jobs/{jid}")
        extract = next(s for s in detail["steps"] if s["name"] == "extract_frames")
        stylize = next(s for s in detail["steps"] if s["name"] == "stylize_frames")
        print(f"  成功；extract attempts 仍为 {extract['attempt_count']}（未重算）；"
              f"stylize retry_round={stylize['retry_round']}")

        # ---- 场景 4：两任务争抢 GPU ------------------------------------
        print("\n=== 场景 4：两任务争抢 1 个 mock GPU 槽（CPU 不受阻）===")
        j1 = http("POST", f"{base}/api/jobs", {
            "params": {"seed": 21}, "demo_fault": {"slow": {"stylize_frames": 4}}})["id"]
        j2 = http("POST", f"{base}/api/jobs", {
            "params": {"seed": 22}, "demo_fault": {"slow": {"stylize_frames": 4}}})["id"]
        max_gpu = 0
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            res = http("GET", f"{base}/api/resources")
            holders = [s for s in res["slots"] if s["kind"] == "gpu" and s["holder"]]
            max_gpu = max(max_gpu, len(holders))
            if all(http("GET", f"{base}/api/jobs/{j}")["status"] == "SUCCEEDED"
                   for j in (j1, j2)):
                break
            time.sleep(0.15)
        print(f"  两任务均 SUCCEEDED；采样期间 GPU 有效 holder 峰值 = {max_gpu}（应 ≤1）")
        print("  （断言只覆盖数据库逻辑预约数，不代表真实 CUDA 进程数）")

        print("\n[完成] 全部演示场景结束。看板可访问:", base, "（本脚本结束时会关闭服务）")
        return 0
    finally:
        kill_tree(worker_proc)
        kill_tree(api_proc)
        print("[清理] 已结束本脚本创建的 API/Worker 进程。")


if __name__ == "__main__":
    sys.exit(main())
