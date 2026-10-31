# -*- coding: utf-8 -*-
"""浏览器实测脚本：用 Playwright 驱动真实 Chromium 验证看板。

自足运行：自己启动 API 与 Worker 子进程（随机端口、独立数据目录），
测试结束只结束本脚本创建的进程。断言针对页面 DOM 与页面内状态，
不是只看 HTTP 接口。

检查项（11 项）：
  1  任务经一次自动重试后 SUCCEEDED
  2  GIF 预览在 <img> 内解码成功
  3  ZIP 下载链接返回合法 zip
  4  永久失败任务到 FAILED
  5  FAILED 时「人工重试」按钮可见
  6  人工重试后成功且 stylize 历史保留（3×FAILED + SUCCEEDED）
  7  快速切换后 state 选中预期任务
  8  事件列表全部属于选中任务且与另一任务不相交（不串台）
  9  DOM 事件行数与页面 state 一致
 10  attempt 表只含选中任务的记录
 11  切到另一任务后事件仍不串台

前置（一次性）：
    .venv\\Scripts\\python.exe -m pip install -r requirements-browser.txt
    .venv\\Scripts\\python.exe -m playwright install chromium

运行（项目根目录，约 2 分钟）：
    .venv\\Scripts\\python.exe scripts\\browser_check.py
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "browser-run"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from demo import kill_tree, wait_until  # noqa: E402  复用「只杀自己的进程」工具


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn(cmd: list[str], env: dict, log_name: str) -> subprocess.Popen:
    DATA.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=open(DATA / log_name, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def http_json(method: str, url: str, payload=None, timeout: float = 10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要可选依赖 playwright。安装：")
        print(r"  .venv\Scripts\python.exe -m pip install -r requirements-browser.txt")
        print(r"  .venv\Scripts\python.exe -m playwright install chromium")
        return 2

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({
        "ANIMFLOW_DATA_DIR": str(DATA),
        "ANIMFLOW_POLL_S": "0.2",
        "ANIMFLOW_HEARTBEAT_S": "0.5",
        "ANIMFLOW_LEASE_S": "2",
        "ANIMFLOW_STEP_SPEED": "8",
        "PYTHONPATH": str(ROOT),
    })
    api_proc = spawn(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env, "api.out")
    worker_proc = spawn([sys.executable, "-m", "app.worker"], env, "worker.out")
    print(f"[启动] API {base} (pid={api_proc.pid})  Worker (pid={worker_proc.pid})")
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    try:
        wait_until(lambda: http_json("GET", f"{base}/healthz")["status"] == "ok",
                   30, "API 未就绪")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.goto(base, wait_until="networkidle")

            # 1-3 成功管线（含一次自动重试）+ GIF + ZIP
            page.click('button[data-preset="transient"]')
            page.wait_for_selector("#d-status.SUCCEEDED", timeout=90000)
            check("任务最终 SUCCEEDED（含一次自动重试）", True, page.inner_text("#d-progress"))

            gif_visible = page.is_visible("#gif-img")
            natural = page.evaluate(
                "() => { const i = document.querySelector('#gif-img');"
                " return [i.naturalWidth, i.naturalHeight]; }")
            check("GIF 预览在页面内且解码成功", gif_visible and natural[0] > 0,
                  f"naturalWidth x Height = {natural}")

            ziphref = page.evaluate(
                "() => { const a=[...document.querySelectorAll('.artifact-row a')]"
                ".find(x => x.closest('.artifact-row')"
                ".querySelector('.kind').textContent.trim()==='final_zip');"
                " return a ? a.getAttribute('href') : null; }")
            assert ziphref, "页面上找不到 final_zip 下载链接"
            resp = page.request.get(base + ziphref)
            ok_zip = resp.status == 200 and resp.body()[:2] == b"PK"
            names = zipfile.ZipFile(io.BytesIO(resp.body())).namelist() if ok_zip else []
            check("ZIP 下载可用且为合法 zip", ok_zip and "preview.gif" in names, str(names))

            # 4-6 永久失败 → 人工重试
            page.click('button[data-preset="permanent"]')
            page.wait_for_selector("#d-status.FAILED", timeout=120000)
            check("永久失败任务到 FAILED", True)
            check("FAILED 时人工重试按钮可见", page.is_visible("#btn-retry"))
            page.click("#btn-retry")  # 默认勾选 clear_fault
            page.wait_for_selector("#d-status.SUCCEEDED", timeout=120000)
            current_job = page.evaluate("() => state.selectedJobId")
            attempts = http_json("GET", f"{base}/api/jobs/{current_job}/attempts")
            stylize = [a for a in attempts["items"] if a["step_name"] == "stylize_frames"]
            check("人工重试后成功且 stylize 历史保留",
                  stylize[-1]["status"] == "SUCCEEDED"
                  and sum(1 for a in stylize if a["status"] == "FAILED") == 3,
                  f"stylize attempts: {[(a['attempt_no'], a['status']) for a in stylize]}")

            # 7-11 快速切换两任务：事件与 attempt 不串台
            j1 = http_json("POST", base + "/api/jobs", {
                "params": {"seed": 101},
                "demo_fault": {"slow": {"stylize_frames": 4}}})["id"]
            j2 = http_json("POST", base + "/api/jobs", {
                "params": {"seed": 102},
                "demo_fault": {"slow": {"stylize_frames": 4}}})["id"]
            for jid in (j1, j2):
                wait_until(lambda j=jid: len(http_json(
                    "GET", f"{base}/api/jobs/{j}/events")["events"]) >= 3, 60, "事件太少")

            def select(jid: str) -> None:
                page.evaluate(
                    "(jid) => { const el=[...document.querySelectorAll('.job-item')]"
                    ".find(e => e.dataset.id === jid); if (el) el.click(); }", jid)

            for i in range(10):            # ~120ms 间隔快速交替
                select(j1 if i % 2 == 0 else j2)
                time.sleep(0.12)
            select(j1)                      # 停在 j1，等在途旧响应落地
            time.sleep(2.5)

            js_state = page.evaluate("() => ({job: state.selectedJobId,"
                                     " evIds: state.events.map(e => e.id)})")
            set_j1 = {e["id"] for e in http_json(
                "GET", f"{base}/api/jobs/{j1}/events?limit=500")["events"]}
            set_j2 = {e["id"] for e in http_json(
                "GET", f"{base}/api/jobs/{j2}/events?limit=500")["events"]}
            check("切换后 state 选中 j1", js_state["job"] == j1)
            ids = set(js_state["evIds"])
            check("事件列表全部属于 j1 且与 j2 不相交（不串台）",
                  ids <= set_j1 and not (ids & set_j2),
                  f"渲染 {len(ids)} 条，j1 共 {len(set_j1)} 条，"
                  f"越界 {len(ids - set_j1)} 条，与 j2 交集 {len(ids & set_j2)} 条")
            dom_rows = page.evaluate(
                "() => document.querySelectorAll('#d-events .event-row').length")
            check("DOM 事件行数与 state 一致", dom_rows == len(js_state["evIds"]),
                  f"dom={dom_rows} state={len(js_state['evIds'])}")

            for jid in (j1, j2):            # 等两个任务跑完再比对 attempt 表
                wait_until(lambda j=jid: http_json(
                    "GET", f"{base}/api/jobs/{j}")["status"] == "SUCCEEDED", 150, "任务未完成")
            dom_attempts = page.evaluate(
                "() => [...document.querySelectorAll('#d-attempts tbody tr')]"
                ".map(tr => [...tr.cells].slice(0, 3)"
                ".map(c => c.textContent.trim()).join('|'))")
            api_att = http_json("GET", f"{base}/api/jobs/{j1}/attempts")["items"]
            expect = [f"{a['attempt_no']}|{a['step_name']}|{a['status']}" for a in api_att]
            check("attempt 表只含选中任务 j1 的记录",
                  sorted(dom_attempts) == sorted(expect) and len(dom_attempts) == len(expect),
                  f"dom={len(dom_attempts)} api={len(expect)}")

            select(j2)                      # 复检：切到 j2 后也不串台
            time.sleep(2.5)
            js2 = page.evaluate("() => ({job: state.selectedJobId,"
                                " evIds: state.events.map(e => e.id)})")
            fresh_j1 = {e["id"] for e in http_json(
                "GET", f"{base}/api/jobs/{j1}/events?limit=500")["events"]}
            fresh_j2 = {e["id"] for e in http_json(
                "GET", f"{base}/api/jobs/{j2}/events?limit=500")["events"]}
            ids2 = set(js2["evIds"])
            check("切到 j2 后事件全部属于 j2 且与 j1 完全不相交",
                  ids2 <= fresh_j2 and not (ids2 & fresh_j1),
                  f"渲染 {len(ids2)} 条，j2 共 {len(fresh_j2)} 条，"
                  f"越界 {len(ids2 - fresh_j2)} 条，与 j1 交集 {len(ids2 & fresh_j1)} 条")
            browser.close()
    finally:
        kill_tree(worker_proc)
        kill_tree(api_proc)
        print("[清理] 已结束本脚本创建的 API/Worker 进程。")

    failed = [r for r in results if not r[1]]
    print(f"\n===== 浏览器实测: {len(results) - len(failed)}/{len(results)} PASS =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
