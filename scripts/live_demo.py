"""Explicit opt-in PAID DeepSeek smoke test; pytest never runs this script.

One real planning call (unless a retry is needed), followed by a downstream
failure/manual retry that must retain the published plan and its attempt.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/deepseek-demo")
    parser.add_argument("--brief", default="青蓝色光球绕圆形轨道运动，深蓝背景，紫色光环，科技感")
    args = parser.parse_args()
    root = (ROOT / args.data_dir).resolve()
    os.environ.update({"ANIMFLOW_DATA_DIR":str(root),
                       "ANIMFLOW_DB_PATH":str(root/"animflow.db"),
                       "ANIMFLOW_ARTIFACTS_DIR":str(root/"artifacts"),
                       "ANIMFLOW_LOGS_DIR":str(root/"logs"),
                       "ANIMFLOW_POLL_S":"0.2", "ANIMFLOW_HEARTBEAT_S":"0.5",
                       "ANIMFLOW_LEASE_S":"3", "ANIMFLOW_STEP_SPEED":"4"})
    from app.config import load_config
    from app.main import create_app
    from fastapi.testclient import TestClient
    from PIL import Image
    cfg = load_config()
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        raise SystemExit("Configure DEEPSEEK_API_KEY in local .env first (never commit it).")
    root.mkdir(parents=True,exist_ok=True)
    with (root/"worker-output.log").open("w",encoding="utf-8") as out:
        proc = subprocess.Popen([sys.executable,"-m","app.worker"],cwd=ROOT,
                                env=os.environ.copy(),stdout=out,stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        try:
            with TestClient(create_app(cfg)) as client:
                response = client.post("/api/jobs",json={"params":{
                    "planner_mode":"deepseek","planner_model":"deepseek-flash",
                    "brief":args.brief,"frame_count":36,"fps":12},
                    "demo_fault":{"always_fail":{"stylize_frames":True}}})
                assert response.status_code==202, response.text
                job_id=response.json()["id"]
                print("job_id:",job_id,flush=True)
                def wait_for(target):
                    deadline=time.monotonic()+160
                    previous=None
                    while time.monotonic()<deadline:
                        job=client.get(f"/api/jobs/{job_id}").json()
                        steps={s["name"]:s["status"] for s in job["steps"]}
                        if steps != previous:
                            print(json.dumps({"status":job["status"],"steps":steps}),flush=True)
                            previous=steps
                        if job["status"]==target:
                            return job
                        if target=="SUCCEEDED" and job["status"]=="FAILED":
                            raise AssertionError("Task failed: "+json.dumps(job.get("fatal_error_code")))
                        time.sleep(.3)
                    raise AssertionError("Timeout waiting for "+target)
                failed=wait_for("FAILED")
                plan_step=next(s for s in failed["steps"] if s["name"]=="plan_motion")
                assert plan_step["status"]=="SUCCEEDED",json.dumps(plan_step,ensure_ascii=True)
                assert plan_step["attempt_count"]==1, "Expected one successful real model attempt"
                artifact_id=plan_step["artifacts"][0]["id"]
                plan=client.get(f"/api/jobs/{job_id}/motion-plan").json()["plan"]
                assert plan["provider"]=="deepseek" and plan["request_id"]
                print("REAL PLAN:",json.dumps(plan,ensure_ascii=True),flush=True)
                r=client.post(f"/api/jobs/{job_id}/retry",json={"clear_fault":True})
                assert r.status_code==200,r.text
                success=wait_for("SUCCEEDED")
                new_plan=next(s for s in success["steps"] if s["name"]=="plan_motion")
                assert new_plan["attempt_count"]==1
                assert new_plan["artifacts"][0]["id"]==artifact_id
                print("PASS: published real plan reused; no second model attempt",flush=True)
                arts=[a for s in success["steps"] for a in s["artifacts"]]
                gif=next(a for a in arts if a["kind"]=="preview_gif")
                gif_path=cfg.artifacts_dir/gif["relative_path"]
                with Image.open(gif_path) as img:
                    assert img.n_frames==36
                    print("PASS: GIF",img.size,"frames=",img.n_frames,flush=True)
                archive=next(a for a in arts if a["kind"]=="final_zip")
                with zipfile.ZipFile(cfg.artifacts_dir/archive["relative_path"]) as zf:
                    assert zf.testzip() is None
                print("PASS: ZIP integrity",flush=True)
                evidence={"job_id":job_id,"plan":plan,"plan_attempt_count":1,
                          "reused_artifact_id":artifact_id,"gif_path":str(gif_path),
                          "frames":36,"status":"SUCCEEDED","test":"real API + Worker + downstream retry"}
                (root/"live-evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
                print("Evidence:",root/"live-evidence.json",flush=True)
        finally:
            if proc.poll() is None:
                if os.name=="nt":
                    subprocess.run(["taskkill","/F","/T","/PID",str(proc.pid)],capture_output=True)
                else:
                    proc.terminate()
                proc.wait(timeout=10)


if __name__=="__main__":
    main()
