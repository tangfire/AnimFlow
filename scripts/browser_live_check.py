"""Check the real, already-published plan in Chromium; makes NO model calls.

Run live_demo.py first. Uses its saved task and starts only a temporary API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"scripts"))
from browser_check import free_port, spawn, http_json
from demo import kill_tree, wait_until


def main():
    from playwright.sync_api import sync_playwright
    data=ROOT/"data"/"deepseek-demo"
    evidence=json.loads((data/"live-evidence.json").read_text(encoding="utf-8"))
    env=os.environ.copy()
    env.update({"ANIMFLOW_DATA_DIR":str(data),"ANIMFLOW_DB_PATH":str(data/"animflow.db"),
                "ANIMFLOW_ARTIFACTS_DIR":str(data/"artifacts"),"PYTHONPATH":str(ROOT)})
    port=free_port()
    base=f"http://127.0.0.1:{port}"
    proc=spawn([sys.executable,"-m","uvicorn","app.main:app","--port",str(port),
                "--host","127.0.0.1","--log-level","warning"],env,"live-api.out")
    try:
        wait_until(lambda:http_json("GET",base+"/healthz")["status"]=="ok",30,"API not ready")
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={"width":1440,"height":1050},device_scale_factor=1)
            errors=[]
            page.on("pageerror",lambda err:errors.append(str(err)))
            page.goto(base+"/?job_id="+evidence["job_id"],wait_until="networkidle")
            page.wait_for_selector("#d-status.SUCCEEDED",timeout=20000)
            page.wait_for_function("document.querySelector('#d-plan').textContent.includes('真实 DeepSeek')")
            text=page.inner_text("#d-plan")
            assert evidence["plan"]["model"] in text
            assert str(evidence["plan"]["usage"]["total_tokens"]) in text
            assert evidence["plan"]["motion"] in text
            print("PASS: real provider/model, tokens and motion displayed")
            page.wait_for_function("document.querySelector('#gif-img').naturalWidth === 256")
            print("PASS: actual GIF decoded in browser")
            assert page.input_value("#p-planner")=="deepseek"
            print("PASS: local configuration selects real mode")
            assert not errors,errors
            print("PASS: no JavaScript page errors")
            screen=ROOT/"docs"/"assets"/"deepseek-dashboard.png"
            page.screenshot(path=str(screen),full_page=True)
            print("Screenshot:",screen)
            browser.close()
    finally:
        kill_tree(proc)


if __name__=="__main__":
    main()
