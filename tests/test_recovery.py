"""真实跨进程集成测试：独立 Worker 子进程 + 硬终止 + 恢复。

覆盖 A04（硬杀恢复）、A06（超时）、A07（双 Worker）、A09（失效窗口）、
A14（启动失败/异常退出）的进程级断言。所有硬杀只结束本测试创建的进程。
"""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app import pipeline
from app.repository import Repository

from conftest import (
    spawn_worker,
    terminate_process_tree,
    wait_until,
)

SHORT = {"ANIMFLOW_POLL_S": "0.2", "ANIMFLOW_HEARTBEAT_S": "0.5",
         "ANIMFLOW_LEASE_S": "2.0", "ANIMFLOW_STEP_SPEED": "10"}


def make_repo(tmp_env) -> Repository:
    return Repository(tmp_env["db"], tmp_env["artifacts"], lease_seconds=2.0)


def job_terminal(repo: Repository, job_id: str) -> str:
    return repo.get_job_detail(job_id)["status"]


# ---- A01 核心：Worker 端到端（不经 API）----------------------------------


def test_a01_worker_end_to_end(tmp_env):
    repo = make_repo(tmp_env)
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) in ("SUCCEEDED", "FAILED"),
                   timeout=90, message="任务未结束")
    finally:
        terminate_process_tree(proc)
    detail = repo.get_job_detail(job["id"])
    assert detail["status"] == "SUCCEEDED", json.dumps(detail, ensure_ascii=False)[:800]
    # 每步恰好一个成功 attempt
    for s in detail["steps"]:
        assert s["status"] == "SUCCEEDED"
        attempts = repo.attempt_rows(s["id"])
        succeeded = [a for a in attempts if a["status"] == "SUCCEEDED"]
        assert len(succeeded) == 1, f"{s['name']} 成功 attempt 不唯一"
        assert s["attempt_count"] == len(attempts)
    # GIF 真实可解码且多帧
    arts = {a["kind"]: a for s in detail["steps"] for a in s["artifacts"]}
    gif_path = tmp_env["artifacts"] / arts["preview_gif"]["relative_path"]
    im = Image.open(gif_path)
    im.load()
    assert getattr(im, "n_frames", 1) == pipeline.DEFAULT_INPUT["frame_count"]
    assert im.size == (pipeline.DEFAULT_INPUT["width"], pipeline.DEFAULT_INPUT["height"])
    # ZIP 真实可解压且含正确文件
    zip_path = tmp_env["artifacts"] / arts["final_zip"]["relative_path"]
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "preview.gif" in names and "params.json" in names
        assert zf.testzip() is None
    assert detail["artifact_health"]["status"] == "valid"


# ---- A04：硬杀 Worker 后换 Worker 恢复 -----------------------------------


def test_a04_hard_kill_worker_then_recover(tmp_env):
    """硬杀自己启动的 Worker（连同子进程树）；另起 Worker 后：
    旧 attempt=WORKER_LOST、新 attempt_id 不同、成功上游不重跑、任务最终成功。
    """
    repo = make_repo(tmp_env)
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT,
         "demo_fault": {"slow": {"stylize_frames": 6}}}
    )
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    killed_at = None
    old_attempt = None
    try:
        # 等 stylize 真正 RUNNING（有活跃租约）
        def stylize_running():
            s = repo.step_row(job["id"], "stylize_frames")
            return s is not None and s["status"] == "RUNNING" and s["active_attempt_id"]

        wait_until(stylize_running, timeout=60, message="stylize 未进入 RUNNING")
        old_attempt = repo.step_row(job["id"], "stylize_frames")["active_attempt_id"]
        upstream_snapshot = {
            name: (repo.step_row(job["id"], name)["attempt_count"],
                   repo.step_row(job["id"], name)["output_fingerprint"])
            for name in ("inspect_asset", "extract_frames", "plan_motion")
        }
        # 硬杀整棵进程树（只杀本测试创建的进程）
        killed_at = time.time()
        terminate_process_tree(proc)
        proc = None
    finally:
        if proc is not None:
            terminate_process_tree(proc)

    # 旧 attempt 此时还是 RUNNING（租约未过期）→ 新 Worker 回收后变 WORKER_LOST
    rec_proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) == "SUCCEEDED",
                   timeout=90, message="恢复后任务未成功")
    finally:
        terminate_process_tree(rec_proc)

    detail = repo.get_job_detail(job["id"])
    assert detail["status"] == "SUCCEEDED"
    stylize_attempts = repo.attempt_rows(detail["steps"][3]["id"])
    assert len(stylize_attempts) == 2
    a1, a2 = stylize_attempts
    assert a1["id"] == old_attempt
    assert a1["status"] == "WORKER_LOST"
    assert a2["status"] == "SUCCEEDED"
    assert a2["id"] != a1["id"]
    assert a2["attempt_no"] == 2 and a1["attempt_no"] == 1
    # 成功上游未重跑
    for name, (cnt, fp) in upstream_snapshot.items():
        row = repo.step_row(job["id"], name)
        assert row["attempt_count"] == cnt, f"{name} 被重跑"
        assert row["output_fingerprint"] == fp
    # 事件时间线：恢复耗时 = WORKER_LOST 事件时间 - 硬杀时间（允许 lease+轮询延迟）
    evs = repo.get_events(job["id"], limit=500)["events"]
    lost = [e for e in evs if e["type"] == "ATTEMPT_WORKER_LOST"]
    assert len(lost) == 1 and lost[0]["attempt_id"] == old_attempt
    recovered_at = lost[0]["created_at"]
    recovery_seconds = recovered_at - killed_at
    print(f"\nA04 恢复证据: kill@{killed_at:.3f} → WORKER_LOST@{recovered_at:.3f}"
          f"（{recovery_seconds:.2f}s，lease=2.0s+轮询）"
          f" old={old_attempt[:8]} new={a2['id'][:8]}")
    assert 0 <= recovery_seconds < 15, f"恢复耗时异常: {recovery_seconds}s"
    # 产物指向新 attempt
    art_attempts = {a["attempt_id"] for a in detail["steps"][3]["artifacts"]}
    assert art_attempts == {a2["id"]}


# ---- A09：产物写完但成功事务前硬杀 ---------------------------------------


def test_a09_crash_after_files_before_publish(tmp_env):
    """crash_after_files：子进程把产物与 result.json 写完后卡住；
    在发布事务前硬杀 Worker；恢复后新 attempt 成功，旧文件不发布、不覆写。
    """
    repo = make_repo(tmp_env)
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT,
         "demo_fault": {"crash_after_files": {"stylize_frames": 1}}}
    )
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    killed_at = None
    try:
        def files_written():
            s = repo.step_row(job["id"], "stylize_frames")
            if s is None or s["status"] != "RUNNING":
                return False
            a1 = tmp_env["artifacts"] / job["id"] / "stylize_frames" / "a1"
            return (a1 / "result.json").is_file() and (a1 / "barrier.flag").is_file()

        wait_until(files_written, timeout=60, message="产物未写完（barrier 未出现）")
        # 此时文件齐全但数据库尚未发布
        assert repo.step_row(job["id"], "stylize_frames")["status"] == "RUNNING"
        assert repo.step_row(job["id"], "stylize_frames")["output_fingerprint"] is None
        killed_at = time.time()
        terminate_process_tree(proc)
        proc = None
    finally:
        if proc is not None:
            terminate_process_tree(proc)

    rec_proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) == "SUCCEEDED",
                   timeout=90, message="恢复后任务未成功")
    finally:
        terminate_process_tree(rec_proc)

    detail = repo.get_job_detail(job["id"])
    stylize = detail["steps"][3]
    attempts = repo.attempt_rows(stylize["id"])
    assert attempts[0]["status"] == "WORKER_LOST"      # 未发布即被回收
    assert attempts[-1]["status"] == "SUCCEEDED"
    # 旧文件仍是孤儿：a1 目录存在但没有已发布记录
    a1_dir = tmp_env["artifacts"] / job["id"] / "stylize_frames" / "a1"
    assert a1_dir.is_dir() and (a1_dir / "result.json").is_file()
    art_attempts = {a["attempt_id"] for a in stylize["artifacts"]}
    assert attempts[0]["id"] not in art_attempts       # 旧文件未被当作成功结果
    assert attempts[-1]["id"] in art_attempts
    assert art_attempts == {attempts[-1]["id"]}
    # 新 attempt 写了新目录 a2，未覆写 a1
    a2_dir = tmp_env["artifacts"] / job["id"] / "stylize_frames" / "a2"
    assert a2_dir.is_dir()
    print(f"\nA09 失效窗口证据: kill@{killed_at:.3f} 旧attempt={attempts[0]['id'][:8]}"
          f"(WORKER_LOST, 孤儿文件保留) 新attempt={attempts[-1]['id'][:8]}(SUCCEEDED, a2)")


def test_a09_db_committed_then_restart_no_rerun(tmp_env):
    """数据库成功提交后重启 Worker：成功步骤不重跑。"""
    repo = make_repo(tmp_env)
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) == "SUCCEEDED",
                   timeout=90, message="任务未成功")
    finally:
        terminate_process_tree(proc)
    detail = repo.get_job_detail(job["id"])
    snapshot = {s["name"]: (s["attempt_count"], s["output_fingerprint"])
                for s in detail["steps"]}
    # 重启 Worker
    proc2 = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        time.sleep(2.0)  # 给它几个轮询周期去“想”领取工作
        assert proc2.poll() is None
    finally:
        terminate_process_tree(proc2)
    detail2 = repo.get_job_detail(job["id"])
    assert detail2["status"] == "SUCCEEDED"
    for s in detail2["steps"]:
        assert (s["attempt_count"], s["output_fingerprint"]) == snapshot[s["name"]]
        assert len(repo.attempt_rows(s["id"])) == s["attempt_count"]


# ---- A06：超时（监督进程存活、业务无进展）--------------------------------


def test_a06_step_timeout_kills_child_and_retries(tmp_env):
    """子进程卡住、监督进程仍在续心跳；deadline 到 → TIMED_OUT、
    子进程被结束、GPU 槽释放、进入有界重试；其他任务仍可推进。
    """
    env = {**SHORT, "ANIMFLOW_STEP_TIMEOUT_S": "3"}
    repo = Repository(tmp_env["db"], tmp_env["artifacts"], lease_seconds=2.0)
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT, "demo_fault": {"timeout": {"stylize_frames": True}}}
    )
    other = repo.create_job(dict(pipeline.DEFAULT_INPUT))  # 其他任务仍可推进
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], env)
    try:
        # 其他任务先被推进（CPU 步骤不受 stylize 卡死影响）
        wait_until(lambda: repo.step_row(other["id"], "inspect_asset")["status"]
                   in ("SUCCEEDED", "RUNNING"), timeout=30,
                   message="其他任务未被推进")
        # 单 Worker 跑完全部预算：3 次超时 → FAILED
        wait_until(lambda: repo.step_row(job["id"], "stylize_frames")["status"] == "FAILED",
                   timeout=120, message="超时未按预算耗尽")
    finally:
        terminate_process_tree(proc)
    attempts = repo.attempt_rows(repo.step_row(job["id"], "stylize_frames")["id"])
    assert len(attempts) == 3
    assert all(a["status"] == "TIMED_OUT" for a in attempts)
    assert all(a["error_code"] == "TIMED_OUT" for a in attempts)
    # 心跳在超时前持续过（监督进程活着但业务无进展）
    assert all(a["heartbeat_at"] > a["started_at"] for a in attempts)
    # GPU 槽全程无泄漏
    assert all(v is None for v in repo.slot_holders().values())
    print(f"\nA06 超时证据: attempts={[(a['attempt_no'], a['status']) for a in attempts]}，"
          f"GPU 槽无泄漏")


# ---- A07：两个 Worker 并行 ------------------------------------------------


def test_a07_two_workers_one_job_no_double_claim(tmp_env):
    repo = make_repo(tmp_env)
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    p1 = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    p2 = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) == "SUCCEEDED",
                   timeout=90, message="双 Worker 任务未成功")
    finally:
        terminate_process_tree(p1)
        terminate_process_tree(p2)
    detail = repo.get_job_detail(job["id"])
    assert detail["status"] == "SUCCEEDED"
    for s in detail["steps"]:
        attempts = repo.attempt_rows(s["id"])
        # 同一步骤不能有两个成功 attempt；attempt_no 唯一递增
        assert sum(1 for a in attempts if a["status"] == "SUCCEEDED") == 1
        assert [a["attempt_no"] for a in attempts] == list(range(1, len(attempts) + 1))
    # 资源无泄漏
    assert all(v is None for v in repo.slot_holders().values())


def test_a07_two_workers_two_jobs_gpu_mutex(tmp_env):
    """两任务争 GPU：有效 GPU holder 数始终 ≤1（采样验证）。"""
    repo = make_repo(tmp_env)
    j1 = repo.create_job({**pipeline.DEFAULT_INPUT,
                          "demo_fault": {"slow": {"stylize_frames": 3}}})
    j2 = repo.create_job({**pipeline.DEFAULT_INPUT,
                          "demo_fault": {"slow": {"stylize_frames": 3}}})
    p1 = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    p2 = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    max_gpu_holders = 0
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            res = repo.list_resources()
            gpu_holders = [s["holder"] for s in res["slots"] if s["kind"] == "gpu" and s["holder"]]
            max_gpu_holders = max(max_gpu_holders, len(gpu_holders))
            if job_terminal(repo, j1["id"]) == "SUCCEEDED" and \
               job_terminal(repo, j2["id"]) == "SUCCEEDED":
                break
            time.sleep(0.1)
    finally:
        terminate_process_tree(p1)
        terminate_process_tree(p2)
    assert job_terminal(repo, j1["id"]) == "SUCCEEDED"
    assert job_terminal(repo, j2["id"]) == "SUCCEEDED"
    assert max_gpu_holders <= 1, f"GPU 有效预约数曾达到 {max_gpu_holders}"
    print(f"\nA08/A07 GPU 互斥证据: 采样期间最大 GPU holder 数 = {max_gpu_holders}")


# ---- A14：启动失败与异常退出（真实 Worker）-------------------------------


def test_a14_fail_launch_consumes_budget(tmp_env):
    repo = make_repo(tmp_env)
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT, "demo_fault": {"fail_launch": {"stylize_frames": True}}}
    )
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: repo.step_row(job["id"], "stylize_frames")["status"] == "FAILED",
                   timeout=60, message="启动失败未按预算终止")
    finally:
        terminate_process_tree(proc)
    attempts = repo.attempt_rows(repo.step_row(job["id"], "stylize_frames")["id"])
    assert len(attempts) == 3
    assert all(a["error_code"] == "SPAWN_FAILED" for a in attempts)
    assert all(v is None for v in repo.slot_holders().values())  # 无资源泄漏


def test_a14_exit_early_then_retry_succeeds(tmp_env):
    repo = make_repo(tmp_env)
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT, "demo_fault": {"exit_early": {"stylize_frames": 1}}}
    )
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) == "SUCCEEDED",
                   timeout=90, message="异常退出后未恢复成功")
    finally:
        terminate_process_tree(proc)
    attempts = repo.attempt_rows(repo.step_row(job["id"], "stylize_frames")["id"])
    assert attempts[0]["status"] == "FAILED"
    assert attempts[0]["error_code"] == "ABNORMAL_EXIT"
    assert attempts[1]["status"] == "SUCCEEDED"


def test_a14_claim_then_death_before_spawn_recovered(tmp_env):
    """claim 后监督进程死于子进程启动前：由过期租约恢复，无资源泄漏。"""
    repo = make_repo(tmp_env)
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    claim = repo.claim_next_step("dead-worker")
    assert claim is not None
    # 模拟：claim 提交后进程立即死亡（不 spawn、不 heartbeat）
    time.sleep(2.5)  # 超过 lease=2s
    recovered = repo.recover_expired()
    assert [r.attempt_id for r in recovered] == [claim.attempt_id]
    assert repo.attempt_rows(claim.step_id)[0]["status"] == "WORKER_LOST"
    assert all(v is None for v in repo.slot_holders().values())
    # 新 Worker 可继续
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) == "SUCCEEDED",
                   timeout=90, message="claim 后死亡未恢复")
    finally:
        terminate_process_tree(proc)


# ---- A11（进程级）：产物损坏阻断任务 -------------------------------------


def test_a11_corrupt_upstream_blocks_manual_retry(tmp_env):
    """删除一个成功上游产物后人工 retry：ARTIFACT_INVALID，拒绝继续。"""
    from app.domain import ArtifactInvalid

    repo = make_repo(tmp_env)
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT, "demo_fault": {"always_fail": {"stylize_frames": True}}}
    )
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) == "FAILED",
                   timeout=90, message="任务未失败")
    finally:
        terminate_process_tree(proc)
    # 砍掉 extract_frames 的一个帧文件
    detail = repo.get_job_detail(job["id"])
    frames_art = [a for s in detail["steps"] if s["name"] == "extract_frames"
                  for a in s["artifacts"]][0]
    frames_dir = tmp_env["artifacts"] / frames_art["relative_path"]
    victims = sorted(frames_dir.glob("*.png"))
    assert victims, "没有帧文件可破坏"
    victims[0].unlink()
    # 人工 retry 被预检拒绝
    with pytest.raises(ArtifactInvalid) as ei:
        repo.manual_retry(job["id"], clear_fault=True)
    assert ei.value.code == "ARTIFACT_INVALID"
    detail = repo.get_job_detail(job["id"])
    assert detail["status"] == "FAILED"  # 保守策略：保留失败，用户重建新任务
    assert detail["fatal_error_code"] == "ARTIFACT_INVALID"
    assert detail["artifact_health"]["status"] == "invalid"
    evs = [e["type"] for e in repo.get_events(job["id"], limit=500)["events"]]
    assert "ARTIFACT_INVALID" in evs


# ---- 事件分页一致性（A10 的仓储部分）-------------------------------------


def test_events_pagination_no_gap_no_dup(tmp_env):
    repo = make_repo(tmp_env)
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], SHORT)
    try:
        wait_until(lambda: job_terminal(repo, job["id"]) == "SUCCEEDED",
                   timeout=90, message="任务未成功")
    finally:
        terminate_process_tree(proc)
    full = repo.get_events(job["id"], limit=500)["events"]
    assert len(full) >= 10
    paged: list = []
    after = 0
    while True:
        page = repo.get_events(job["id"], after_id=after, limit=2)
        paged.extend(page["events"])
        if not page["events"]:
            break
        after = page["next_after_id"]
    assert [e["id"] for e in paged] == [e["id"] for e in full]  # 不重不漏
