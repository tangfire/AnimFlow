"""HTTP 接口与看板数据测试：TestClient + 真实 Worker 子进程 + uvicorn 重启。

覆盖 A01（API 层）、A03/A11（HTTP 语义）、A10（API 重启与事件分页）、A12（校验/404/路径约束）。
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import pipeline
from app.config import Config
from app.main import create_app
from app.repository import Repository

from conftest import ROOT, terminate_process_tree, wait_until

SHORT = {"ANIMFLOW_POLL_S": "0.2", "ANIMFLOW_HEARTBEAT_S": "0.5",
         "ANIMFLOW_LEASE_S": "2.0", "ANIMFLOW_STEP_SPEED": "10"}


@pytest.fixture
def client(tmp_env):
    cfg = Config(
        data_dir=tmp_env["root"], db_path=tmp_env["db"], artifacts_dir=tmp_env["artifacts"],
        logs_dir=tmp_env["root"] / "logs",
        poll_interval_s=0.2, heartbeat_interval_s=0.5, lease_seconds=2.0,
    )
    with TestClient(create_app(cfg)) as c:
        c.app_state_cfg = cfg  # type: ignore[attr-defined]
        yield c


def repo_of(tmp_env) -> Repository:
    return Repository(tmp_env["db"], tmp_env["artifacts"], lease_seconds=2.0)


# ---- A01：提交 → Worker 未开可查询 → Worker 消费 → 真实产物 ----------------


def test_a01_post_before_worker_then_success(client, tmp_env, worker_spawner):
    # Worker 未开也能提交（202 持久化）
    r = client.post("/api/jobs", json={"params": dict(pipeline.DEFAULT_INPUT)})
    assert r.status_code == 202
    body = r.json()
    job_id = body["id"]
    assert body["status"] == "QUEUED"
    assert body["status_url"] == f"/api/jobs/{job_id}"
    # 未开 Worker 时可查询
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "QUEUED"
    assert len(r.json()["steps"]) == 6

    # 启动 Worker → 六步成功
    worker_spawner()
    repo = repo_of(tmp_env)
    wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "SUCCEEDED",
               timeout=90, message="任务未成功")
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert all(s["status"] == "SUCCEEDED" for s in detail["steps"])
    for s in detail["steps"]:
        succeeded = [a for a in repo.attempt_rows(s["id"]) if a["status"] == "SUCCEEDED"]
        assert len(succeeded) == 1

    # GIF 能解码且多帧；ZIP 有正确文件（经下载接口）
    arts = {a["kind"]: a for s in detail["steps"] for a in s["artifacts"]}
    gif = client.get(f"/api/artifacts/{arts['preview_gif']['id']}/download")
    assert gif.status_code == 200
    im = Image.open(io.BytesIO(gif.content))
    im.load()
    assert im.n_frames == pipeline.DEFAULT_INPUT["frame_count"]
    z = client.get(f"/api/artifacts/{arts['final_zip']['id']}/download")
    assert z.status_code == 200
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert "preview.gif" in zf.namelist()
        assert zf.testzip() is None
    # 多文件产物（帧集合）下载自动打包为 ZIP
    frames = client.get(f"/api/artifacts/{arts['frames']['id']}/download")
    assert frames.status_code == 200
    with zipfile.ZipFile(io.BytesIO(frames.content)) as zf:
        pngs = [n for n in zf.namelist() if n.endswith(".png")]
        assert len(pngs) == pipeline.DEFAULT_INPUT["frame_count"]


# ---- 参数与路径校验（A12）------------------------------------------------


def test_a12_validation_and_404(client):
    # 非法尺寸/帧数 → 422，不入队
    for bad in ({"width": 9999}, {"frame_count": 0}, {"frame_count": 100},
                {"style": "unknown-style"}, {"seed": -1}, {"height": "abc"},
                {"unknown_key": 1}):
        r = client.post("/api/jobs", json={"params": bad})
        assert r.status_code == 422, bad
        assert r.json()["error"]["code"] == "INPUT_INVALID"
    # 未知任务 → 404
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.get("/api/jobs/deadbeef/attempts").status_code == 404
    assert client.get("/api/jobs/deadbeef/events").status_code == 404
    assert client.post("/api/jobs/deadbeef/retry", json={}).status_code == 404
    # 未知产物 → 404
    assert client.get("/api/artifacts/nope/download").status_code == 404
    # 非法故障类型/步骤 → 422
    r = client.post("/api/jobs", json={"demo_fault": {"rm_rf": {"stylize_frames": True}}})
    assert r.status_code == 422
    r = client.post("/api/jobs", json={"demo_fault": {"always_fail": {"no_such_step": True}}})
    assert r.status_code == 422
    # healthz 与 resources 正常
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json()["database"] == "ok"
    res = client.get("/api/resources").json()
    assert {s["kind"] for s in res["slots"]} == {"cpu", "gpu"}
    # 看板静态页可取
    page = client.get("/")
    assert page.status_code == 200 and "AnimFlow" in page.text


def test_download_path_is_db_driven_not_user_path(client):
    """下载只按 artifact_id 查表；不存在的 id 不能读任意磁盘路径。"""
    r = client.get("/api/artifacts/..%2F..%2Fetc%2Fpasswd/download")
    assert r.status_code in (404, 422)
    r = client.get("/api/artifacts/../../../windows/win.ini/download")
    assert r.status_code in (404, 422)


# ---- A03 HTTP 语义：retry 409 -------------------------------------------


def test_a03_retry_http_semantics(client, tmp_env, worker_spawner):
    r = client.post("/api/jobs", json={
        "params": dict(pipeline.DEFAULT_INPUT),
        "demo_fault": {"always_fail": {"stylize_frames": True}},
    })
    job_id = r.json()["id"]
    # 非 FAILED（此处为 QUEUED，Worker 尚未启动）上 retry → 409
    r2 = client.post(f"/api/jobs/{job_id}/retry", json={})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "INVALID_STATE"
    worker_spawner()
    wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "FAILED",
               timeout=90, message="任务未失败")
    # FAILED 上 retry + clear_fault → 200，任务回 QUEUED/RUNNING
    r = client.post(f"/api/jobs/{job_id}/retry", json={"clear_fault": True})
    assert r.status_code == 200
    assert r.json()["input"]["demo_fault"] == {}
    # 成功后 SUCCEEDED 上 retry → 409
    wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "SUCCEEDED",
               timeout=120, message="重试后未成功")
    r = client.post(f"/api/jobs/{job_id}/retry", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INVALID_STATE"


# ---- A11 HTTP 语义：损坏产物 409 ----------------------------------------


def test_a11_corrupt_artifact_download_and_retry_409(client, tmp_env, worker_spawner):
    r = client.post("/api/jobs", json={
        "params": dict(pipeline.DEFAULT_INPUT),
        "demo_fault": {"always_fail": {"stylize_frames": True}},
    })
    job_id = r.json()["id"]
    worker_spawner()
    wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "FAILED",
               timeout=90, message="任务未失败")
    detail = client.get(f"/api/jobs/{job_id}").json()
    # stylize 失败 → render/package 无产物；破坏 extract 的帧目录验证损坏检测
    frames_art = [a for s in detail["steps"] if s["name"] == "extract_frames"
                  for a in s["artifacts"]][0]
    frames_dir = tmp_env["artifacts"] / frames_art["relative_path"]
    victim = sorted(frames_dir.glob("*.png"))[0]
    victim.unlink()  # 破坏产物：删除一个已发布帧文件
    # 下载该目录产物 → 409 明确错误 + artifact_health
    r = client.get(f"/api/artifacts/{frames_art['id']}/download")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "ARTIFACT_INVALID"
    assert body["artifact_health"] == "invalid"
    # 任务详情健康检查 → invalid
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["artifact_health"]["status"] == "invalid"
    # 人工 retry → 409 保留失败
    r = client.post(f"/api/jobs/{job_id}/retry", json={"clear_fault": True})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ARTIFACT_INVALID"
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "FAILED"
    # 事件里可查 ARTIFACT_INVALID
    evs = client.get(f"/api/jobs/{job_id}/events?limit=500").json()["events"]
    assert any(e["type"] == "ARTIFACT_INVALID" for e in evs)


# ---- A10：API 重启、Worker 不受影响、事件分页 ----------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_api(db: Path, artifacts: Path, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "ANIMFLOW_DATA_DIR": str(db.parent),
        "ANIMFLOW_DB_PATH": str(db),
        "ANIMFLOW_ARTIFACTS_DIR": str(artifacts),
        "PYTHONPATH": str(ROOT),
    })
    out = db.parent / f"api-{port}-{int(time.time())}.out"
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=open(out, "w", encoding="utf-8"), stderr=subprocess.STDOUT,
    )


def _http_json(method: str, url: str, payload=None, timeout=5):
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def test_a10_api_restart_worker_continues(client, tmp_env, worker_spawner):
    port = _free_port()
    api_proc = _spawn_api(tmp_env["db"], tmp_env["artifacts"], port)
    try:
        wait_until(lambda: _http_json("GET", f"http://127.0.0.1:{port}/healthz")[0] == 200,
                   timeout=20, message="API 未就绪")
        status, body = _http_json("POST", f"http://127.0.0.1:{port}/api/jobs",
                                  {"params": {**pipeline.DEFAULT_INPUT,
                                              "demo_fault": {"slow": {"stylize_frames": 4}}}})
        assert status == 202
        job_id = body["id"]
        worker_spawner()
        # 分页拉事件（每页 3 条），验证不重不漏
        def fetch_all_events():
            events, after = [], 0
            while True:
                _, page = _http_json(
                    "GET",
                    f"http://127.0.0.1:{port}/api/jobs/{job_id}/events"
                    f"?after_id={after}&limit=3")
                events.extend(page["events"])
                if not page["events"]:
                    break
                after = page["next_after_id"]
            return events
        wait_until(lambda: len(fetch_all_events()) >= 2, timeout=30,
                   message="事件太少")
    finally:
        terminate_process_tree(api_proc)
    # API 已重启前：先杀掉再起一个新的（同一端口）
    api_proc2 = _spawn_api(tmp_env["db"], tmp_env["artifacts"], port)
    try:
        wait_until(lambda: _http_json("GET", f"http://127.0.0.1:{port}/healthz")[0] == 200,
                   timeout=20, message="API 重启后未就绪")
        # 任务历史从数据库恢复
        _, detail = _http_json("GET", f"http://127.0.0.1:{port}/api/jobs/{job_id}")
        assert detail["id"] == job_id
        # Worker 不受 API 重启影响，最终成功
        wait_until(lambda: _http_json("GET", f"http://127.0.0.1:{port}/api/jobs/{job_id}")[1]["status"] == "SUCCEEDED",
                   timeout=90, message="API 重启后任务未成功")
        # 事件分页：不重不漏
        full = _http_json("GET", f"http://127.0.0.1:{port}/api/jobs/{job_id}/events?limit=500")[1]["events"]
        paged = []
        after = 0
        while True:
            page = _http_json(
                "GET",
                f"http://127.0.0.1:{port}/api/jobs/{job_id}/events?after_id={after}&limit=4")[1]
            paged.extend(page["events"])
            if not page["events"]:
                break
            after = page["next_after_id"]
        assert [e["id"] for e in paged] == [e["id"] for e in full]
        # after 超过最大 id → 空页且 next_after_id 保持
        _, empty = _http_json(
            "GET", f"http://127.0.0.1:{port}/api/jobs/{job_id}/events?after_id={full[-1]['id'] + 100}")
        assert empty["events"] == []
        assert empty["next_after_id"] == full[-1]["id"] + 100
    finally:
        terminate_process_tree(api_proc2)


# ---- 回归：平铺输入必须解析或明确拒绝（外部复核问题 3）------------------


def test_flat_input_parsed_and_validated(client):
    """平铺格式：合法值实际入库；非法值 422；未知参数 422；不得静默忽略。"""
    # 合法平铺 → 实际入库（修复前被静默丢弃、存成默认值）
    r = client.post("/api/jobs", json={"style": "comic", "seed": 7})
    assert r.status_code == 202
    detail = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert detail["input"]["params"]["style"] == "comic"
    assert detail["input"]["params"]["seed"] == 7
    assert detail["input"]["params"]["width"] == 256  # 未提供的键保留默认
    # 非法平铺值 → 422（修复前 202 且存默认）
    assert client.post("/api/jobs", json={"width": 9999}).status_code == 422
    assert client.post("/api/jobs", json={"seed": -1}).status_code == 422
    assert client.post("/api/jobs", json={"style": "nope"}).status_code == 422
    # 未知参数 → 422（不得静默丢弃）
    r = client.post("/api/jobs", json={"style": "neon", "bogus_key": 1})
    assert r.status_code == 422
    assert "bogus_key" in r.json()["error"]["message"]
    # 平铺与 params 嵌套混用 → 422（明确拒绝，不猜测语义）
    r = client.post("/api/jobs", json={"params": {"seed": 3}, "width": 999})
    assert r.status_code == 422
    # 嵌套格式仍然可用
    r = client.post("/api/jobs", json={"params": {"style": "pixel", "seed": 9}})
    assert r.status_code == 202
    detail = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert detail["input"]["params"]["style"] == "pixel"
    # 平铺 demo_fault 也必须解析（同样不得静默丢弃）
    r = client.post("/api/jobs", json={
        "seed": 5, "demo_fault": {"always_fail": {"plan_motion": True}}})
    assert r.status_code == 202
    detail = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert detail["input"]["demo_fault"] == {"always_fail": {"plan_motion": True}}
    assert detail["input"]["params"]["seed"] == 5
