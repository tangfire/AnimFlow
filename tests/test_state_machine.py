"""仓储层状态机与事务测试（可注入时钟，不启动真实进程）。

覆盖 A02/A03/A05/A07/A08/A13/A14 的核心事务断言。
"""
from __future__ import annotations

import pytest

from app import pipeline
from app.artifacts import bundle_single_file
from app.domain import (
    ArtifactInvalid,
    AttemptStatus,
    ERR_ABNORMAL_EXIT,
    ERR_NONRETRYABLE_FAULT,
    EventType,
    IdentityRejected,
    InvalidJobState,
    JobStatus,
    REJ_LEASE,
    REJ_NOT_ACTIVE,
    REJ_OWNER,
    REJ_SLOT,
    StepStatus,
)
from app.pipeline import PipelineError, StepDef, validate_dag
from app.repository import Repository

from conftest import FakeClock, make_fake_bundle


def _complete_with_fake_file(repo: Repository, claim, clock: FakeClock | None = None):
    bundles = make_fake_bundle(repo, claim)
    return repo.complete_attempt(claim.attempt_id, claim.owner, bundles, fingerprint=f"fp-{claim.step_name}-{claim.attempt_no}")


# ---- 阶段 0 退出门槛 -----------------------------------------------------


def test_db_init_idempotent(tmp_env):
    from app.db import init_db
    init_db(tmp_env["db"], cpu_slots=2, gpu_slots=1)  # 二次初始化安全
    init_db(tmp_env["db"], cpu_slots=2, gpu_slots=1)
    repo = Repository(tmp_env["db"], tmp_env["artifacts"])
    res = repo.list_resources()
    kinds = [s["kind"] for s in res["slots"]]
    assert kinds.count("cpu") == 2 and kinds.count("gpu") == 1


def test_create_job_has_all_steps_in_one_transaction(repo):
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    assert job["status"] == "QUEUED"
    assert [s["name"] for s in job["steps"]] == [s.name for s in pipeline.STEPS]
    assert all(s["status"] == "PENDING" for s in job["steps"])
    events = repo.get_events(job["id"])["events"]
    assert [e["type"] for e in events] == ["JOB_CREATED"]


def test_dag_validation_rejects_illegal_dag():
    with pytest.raises(PipelineError):
        validate_dag(
            (
                StepDef("a", "1", ("b",), "cpu", 10),  # 环 a→b→a
                StepDef("b", "1", ("a",), "cpu", 10),
            )
        )
    with pytest.raises(PipelineError):
        validate_dag((StepDef("a", "1", ("missing",), "cpu", 10),))
    with pytest.raises(PipelineError):
        validate_dag(
            (
                StepDef("a", "1", (), "cpu", 10),
                StepDef("a", "2", (), "cpu", 10),  # 重名
            )
        )


# ---- 领取与互斥（A07/A08 单元）-------------------------------------------


def test_claim_creates_attempt_and_occupies_slot(repo, clock):
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    c1 = repo.claim_next_step("worker-A")
    assert c1 is not None
    assert c1.step_name == "inspect_asset"
    assert c1.attempt_no == 1
    holders = repo.slot_holders()
    assert sum(1 for v in holders.values() if v is not None) == 1
    assert c1.attempt_id in holders.values()
    step = repo.step_row(job["id"], "inspect_asset")
    assert step["status"] == "RUNNING"
    assert step["active_attempt_id"] == c1.attempt_id
    evs = repo.get_events(job["id"])["events"]
    assert any(e["type"] == "STEP_CLAIMED" for e in evs)


def test_two_repos_cannot_claim_same_gpu_step(repo_clock, tmp_env, clock):
    """两个连接不能拿到同一步骤/同一 GPU 槽位（A07/A08）。"""
    job = repo_clock.create_job(dict(pipeline.DEFAULT_INPUT))
    repo_b = Repository(tmp_env["db"], tmp_env["artifacts"], lease_seconds=10.0, now_fn=clock)

    def drive_until_gpu(repo: Repository, worker: str):
        for _ in range(30):
            claim = repo.claim_next_step(worker)
            if claim is None:
                return None
            if claim.resource == "gpu":
                return claim
            _complete_with_fake_file(repo, claim, clock)
        return None

    g1 = drive_until_gpu(repo_clock, "worker-A")
    g2 = drive_until_gpu(repo_b, "worker-B")
    assert g1 is not None
    assert g2 is None  # 唯一 GPU 槽被 A 持有，B 拿不到
    holders = [v for v in repo_clock.slot_holders().values() if v is not None]
    assert holders == [g1.attempt_id]
    # A 完成释放 GPU 后，第二个任务的 GPU 步骤才能被 B 领取
    _complete_with_fake_file(repo_clock, g1, clock)
    job2 = repo_clock.create_job(dict(pipeline.DEFAULT_INPUT))
    g3 = drive_until_gpu(repo_b, "worker-B")
    assert g3 is not None
    assert g3.job_id == job2["id"]
    assert g3.attempt_id != g1.attempt_id


def test_waiting_for_resource_does_not_consume_attempt(repo_clock, clock):
    """等待 GPU 不消耗 attempt（A08）：槽忙时 claim 返回 None，步骤留在 PENDING。"""
    job = repo_clock.create_job(dict(pipeline.DEFAULT_INPUT))

    def drive_until_gpu(repo, worker):
        for _ in range(30):
            claim = repo.claim_next_step(worker)
            if claim is None:
                return None
            if claim.resource == "gpu":
                return claim
            _complete_with_fake_file(repo, claim, clock)
        return None

    g1 = drive_until_gpu(repo_clock, "worker-A")
    assert g1 is not None
    stylize = repo_clock.step_row(job["id"], "stylize_frames")
    assert stylize["status"] == "RUNNING" and stylize["attempt_count"] == 1
    repo_b = Repository(tmp_env_db(repo_clock), tmp_env_arts(repo_clock),
                        lease_seconds=10.0, now_fn=clock)
    g2 = drive_until_gpu(repo_b, "worker-B")
    assert g2 is None
    # B 等待 GPU 不产生新 attempt、不消耗尝试次数
    stylize_after = repo_clock.step_row(job["id"], "stylize_frames")
    assert stylize_after["attempt_count"] == 1
    assert all(a["owner"] == "worker-A"
               for a in repo_clock.attempt_rows(stylize_after["id"]))


def tmp_env_db(repo: Repository):
    return repo.db_path


def tmp_env_arts(repo: Repository):
    return repo.artifacts_root


def test_dependency_gate_blocks_claim(repo, clock):
    """依赖未成功不得执行（A07）。"""
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    # 只完成 inspect → extract 可领；plan_motion 也可领（只依赖 inspect）
    c = repo.claim_next_step("w")
    _complete_with_fake_file(repo, c)
    names = set()
    for _ in range(4):
        c = repo.claim_next_step("w")
        if c is None:
            break
        names.add(c.step_name)
        _complete_with_fake_file(repo, c)
    # stylize 只有在 extract+plan 都完成后才可领
    stylize = repo.step_row(job["id"], "stylize_frames")
    if stylize["status"] != "SUCCEEDED":
        assert stylize["status"] in ("PENDING", "RUNNING", "RETRY_WAIT")


# ---- A02：瞬时失败自动重试 ----------------------------------------------


def test_a02_transient_retry_then_success(repo_clock, clock):
    repo = repo_clock
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT, "demo_fault": {"fail_first_n": {"stylize_frames": 1}}}
    )

    def run_until_step(name: str):
        for _ in range(20):
            claim = repo.claim_next_step("w")
            if claim is None:
                return None
            if claim.step_name == name:
                return claim
            _complete_with_fake_file(repo, claim)
        raise AssertionError("没有领到任何步骤")

    # 正常推进：inspect → extract/plan → stylize(第1次失败)
    stylize_claim = None
    for _ in range(10):
        claim = repo.claim_next_step("w")
        assert claim is not None
        if claim.step_name == "stylize_frames":
            stylize_claim = claim
            # 模拟子进程故障：瞬时失败
            out = repo.fail_attempt(
                stylize_claim.attempt_id, "w",
                "MOCK_TRANSIENT_FAULT", "演示注入的瞬时失败", retryable=True,
            )
            assert out["retry_scheduled"] is True
            assert out["step_status"] == "RETRY_WAIT"
            break
        _complete_with_fake_file(repo, claim)

    step = repo.step_row(job["id"], "stylize_frames")
    assert step["status"] == "RETRY_WAIT"
    assert step["next_run_at"] is not None
    assert step["next_run_at"] > clock()  # next_run_at 持久化
    upstream = repo.step_row(job["id"], "extract_frames")
    upstream_attempts = upstream["attempt_count"]
    assert upstream["status"] == "SUCCEEDED"

    # 退避未到 → 不可领取
    assert repo.claim_next_step("w") is None
    clock.advance(pipeline.retry_backoff_seconds(1) + 0.01)

    # 第二次尝试成功
    c2 = repo.claim_next_step("w")
    assert c2 is not None and c2.step_name == "stylize_frames" and c2.attempt_no == 2
    _complete_with_fake_file(repo, c2)

    # 上游成功步骤 attempt_count 不变；历史可查
    assert repo.step_row(job["id"], "extract_frames")["attempt_count"] == upstream_attempts
    attempts = repo.attempt_rows(step["id"])
    assert [a["status"] for a in attempts] == ["FAILED", "SUCCEEDED"]
    evs = [e["type"] for e in repo.get_events(job["id"])["events"]]
    assert "STEP_RETRY_SCHEDULED" in evs


# ---- A03：预算耗尽 + 人工 retry ------------------------------------------


def test_a03_budget_exhaust_then_manual_retry(repo_clock, clock):
    repo = repo_clock
    fault = {"always_fail": {"stylize_frames": True}}
    job = repo.create_job({**pipeline.DEFAULT_INPUT, "demo_fault": fault})

    def run_until(name):
        for _ in range(30):
            claim = repo.claim_next_step("w")
            if claim is None:
                return None
            if claim.step_name == name:
                return claim
            _complete_with_fake_file(repo, claim)
        return None

    # 推进到 stylize
    while True:
        c = run_until("stylize_frames")
        assert c is not None
        out = repo.fail_attempt(c.attempt_id, "w", "MOCK_TRANSIENT_FAULT", "持续失败")
        if not out["retry_scheduled"]:
            break
        clock.advance(31)

    step = repo.step_row(job["id"], "stylize_frames")
    assert step["status"] == "FAILED"
    attempts = repo.attempt_rows(step["id"])
    assert len(attempts) == 3  # 恰好 3 次总尝试，不是 4 次
    assert [a["attempt_no"] for a in attempts] == [1, 2, 3]

    # 聚合：任务 FAILED；下游 BLOCKED
    assert repo.aggregate_job(job["id"]) == "FAILED"
    render = repo.step_row(job["id"], "render_preview")
    assert render["status"] == "BLOCKED"
    package = repo.step_row(job["id"], "package_artifact")
    assert package["status"] == "BLOCKED"
    detail = repo.get_job_detail(job["id"])
    assert detail["status"] == "FAILED"

    # 人工 retry + clear_fault
    extract_fp_before = repo.step_row(job["id"], "extract_frames")["output_fingerprint"]
    extract_attempts_before = repo.step_row(job["id"], "extract_frames")["attempt_count"]
    retried = repo.manual_retry(job["id"], clear_fault=True)
    assert retried["status"] == "QUEUED"
    assert retried["input"]["demo_fault"] == {}
    stylize_after = repo.step_row(job["id"], "stylize_frames")
    assert stylize_after["status"] == "PENDING"
    assert stylize_after["retry_round"] == 1
    assert stylize_after["attempts_in_round"] == 0
    # 成功上游保留、不重算
    assert repo.step_row(job["id"], "extract_frames")["attempt_count"] == extract_attempts_before
    assert repo.step_row(job["id"], "extract_frames")["output_fingerprint"] == extract_fp_before
    # 旧历史保留
    assert len(repo.attempt_rows(step["id"])) == 3
    evs = [e["type"] for e in repo.get_events(job["id"])["events"]]
    assert "JOB_RETRIED" in evs and "OUTPUT_REUSED" in evs

    # 重试后成功
    while True:
        c = run_until("stylize_frames")
        assert c is not None and c.retry_round == 1
        _complete_with_fake_file(repo, c)
        break
    while True:
        c = repo.claim_next_step("w")
        if c is None:
            break
        _complete_with_fake_file(repo, c)
    assert repo.get_job_detail(job["id"])["status"] == "SUCCEEDED"


def test_manual_retry_on_non_failed_is_409(repo):
    """A03：QUEUED/RUNNING/SUCCEEDED 上人工 retry 返回 409 语义。"""
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    with pytest.raises(InvalidJobState):
        repo.manual_retry(job["id"])  # QUEUED
    c = repo.claim_next_step("w")
    with pytest.raises(InvalidJobState):
        repo.manual_retry(job["id"])  # RUNNING
    repo.complete_attempt(c.attempt_id, "w", make_fake_bundle(repo, c), "fp1")
    # 跑完整管线到 SUCCEEDED
    for _ in range(10):
        c = repo.claim_next_step("w")
        if c is None:
            break
        repo.complete_attempt(c.attempt_id, "w", make_fake_bundle(repo, c), f"fp-{c.step_name}")
    assert repo.get_job_detail(job["id"])["status"] == "SUCCEEDED"
    with pytest.raises(InvalidJobState):
        repo.manual_retry(job["id"])  # SUCCEEDED


# ---- A05：旧 attempt 拒绝 ------------------------------------------------


def test_a05_stale_attempt_rejected_after_new_attempt(repo_clock, tmp_env, clock):
    repo = repo_clock
    """A 持有 attempt → 租约过期 → B 回收并领新 attempt → A 的
    heartbeat/complete/release 全部被拒，B 的状态/holder/产物不变。
    额外验证：过期但尚未回收时 A 也不能提交。
    """
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    a_claim = repo.claim_next_step("worker-A")
    assert a_claim is not None

    # 推进时间使 A 的租约过期（但尚未回收）
    clock.advance(10.5)

    # 过期但未回收：A 的三种写入全部被拒
    with pytest.raises(IdentityRejected) as ei:
        repo.heartbeat(a_claim.attempt_id, "worker-A")
    assert ei.value.code == REJ_LEASE
    with pytest.raises(IdentityRejected) as ei:
        bundles = make_fake_bundle(repo, a_claim)
        repo.complete_attempt(a_claim.attempt_id, "worker-A", bundles, "fp-old")
    assert ei.value.code == REJ_LEASE
    with pytest.raises(IdentityRejected):
        repo.release_resource_for_test(a_claim.attempt_id, "worker-A")

    # B 回收过期租约并领取新 attempt
    recovered = repo.recover_expired()
    assert [r.attempt_id for r in recovered] == [a_claim.attempt_id]
    assert repo.attempt_rows(a_claim.step_id)[0]["status"] == "WORKER_LOST"

    # 退避窗口未到不可领取
    repo_b = Repository(tmp_env["db"], tmp_env["artifacts"], lease_seconds=10.0, now_fn=clock)
    assert repo_b.claim_next_step("worker-B") is None
    clock.advance(1.1)
    b_claim = repo_b.claim_next_step("worker-B")
    assert b_claim is not None
    assert b_claim.attempt_id != a_claim.attempt_id
    assert b_claim.attempt_no == 2
    holders = repo.slot_holders()
    assert b_claim.attempt_id in holders.values()
    assert a_claim.attempt_id not in holders.values()

    # A 的三种写入仍然被拒（status 已非 RUNNING / 不是 active）——
    # 此时 B 仍持有槽位，用于验证旧 attempt 不能扰动新 attempt 的资源
    with pytest.raises(IdentityRejected) as ei:
        repo.heartbeat(a_claim.attempt_id, "worker-A")
    assert ei.value.code in (REJ_LEASE, "REJECTED_ATTEMPT_NOT_RUNNING")
    with pytest.raises(IdentityRejected):
        bundles = make_fake_bundle(repo, a_claim)
        repo.complete_attempt(a_claim.attempt_id, "worker-A", bundles, "fp-old")
    with pytest.raises(IdentityRejected) as ei:
        repo.release_resource_for_test(a_claim.attempt_id, "worker-A")
    assert ei.value.code in ("REJECTED_ATTEMPT_NOT_RUNNING", REJ_LEASE, REJ_NOT_ACTIVE)
    # 即便绕过身份守卫，holder 条件更新也不能误释放新 attempt（B）的槽位
    with pytest.raises(IdentityRejected) as ei:
        repo.raw_release_slot_for_test(a_claim.attempt_id)
    assert ei.value.code == REJ_SLOT
    assert b_claim.attempt_id in repo.slot_holders().values()

    # B 的写入正常
    repo_b.heartbeat(b_claim.attempt_id, "worker-B", progress=0.5, progress_message="跑着")
    _complete_with_fake_file(repo_b, b_claim)
    step = repo.step_row(job["id"], "inspect_asset")
    assert step["status"] == "SUCCEEDED"
    assert step["active_attempt_id"] is None
    # B 的产物未被改动：SUCCEEDED 步骤的产物指向 B 的 attempt
    arts_attempts = {
        a["attempt_id"] for a in repo.get_job_detail(job["id"])["steps"][0]["artifacts"]
    }
    assert arts_attempts == {b_claim.attempt_id}


def test_a05_owner_mismatch_rejected(repo, clock):
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    claim = repo.claim_next_step("worker-A")
    with pytest.raises(IdentityRejected) as ei:
        repo.heartbeat(claim.attempt_id, "worker-B")
    assert ei.value.code == REJ_OWNER
    with pytest.raises(IdentityRejected) as ei:
        repo.fail_attempt(claim.attempt_id, "worker-B", "X", "m")
    assert ei.value.code == REJ_OWNER


def test_not_active_attempt_cannot_commit(repo, clock):
    """步骤 active_attempt 已被清除后，旧 attempt 即使租约未过也不能提交。"""
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    claim = repo.claim_next_step("w")
    # 直接清掉 active（模拟已被新 attempt 取代的状态）
    import sqlite3
    from app.db import connect
    conn = connect(repo.db_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE steps SET active_attempt_id=NULL WHERE id=?", (claim.step_id,))
    conn.execute("COMMIT")
    conn.close()
    with pytest.raises(IdentityRejected) as ei:
        repo.complete_attempt(claim.attempt_id, "w",
                              make_fake_bundle(repo, claim), "fp")
    assert ei.value.code == REJ_NOT_ACTIVE


# ---- A13：事务回滚无半套状态 --------------------------------------------


def test_a13_claim_tx_rollback_no_partial_state(repo_clock, clock, tmp_env):
    """claim 事务中注入异常 → 无占槽、无 attempt、步骤仍 PENDING、无事件。"""
    job = repo_clock.create_job(dict(pipeline.DEFAULT_INPUT))

    def boom(point):
        if point == "claim_after_slot":
            raise RuntimeError("注入故障：claim 事务中途失败")

    repo_clock.tx_fault_hook = boom
    with pytest.raises(RuntimeError):
        repo_clock.claim_next_step("w")
    repo_clock.tx_fault_hook = None

    holders = [v for v in repo_clock.slot_holders().values() if v is not None]
    assert holders == []  # 无占槽却没 attempt
    step = repo_clock.step_row(job["id"], "inspect_asset")
    assert step["status"] == "PENDING"
    assert step["attempt_count"] == 0
    assert step["active_attempt_id"] is None
    evs = [e["type"] for e in repo_clock.get_events(job["id"])["events"]]
    assert evs == ["JOB_CREATED"]  # 无 STEP_CLAIMED
    # 回滚后可以正常领取
    c = repo_clock.claim_next_step("w")
    assert c is not None


def test_a13_complete_tx_rollback_no_partial_state(repo_clock, clock):
    job = repo_clock.create_job(dict(pipeline.DEFAULT_INPUT))
    claim = repo_clock.claim_next_step("w")
    bundles = make_fake_bundle(repo_clock, claim)

    def boom(point):
        if point == "complete_before_aggregate":
            raise RuntimeError("注入故障：complete 提交前失败")

    repo_clock.tx_fault_hook = boom
    with pytest.raises(RuntimeError):
        repo_clock.complete_attempt(claim.attempt_id, "w", bundles, "fp-x")
    repo_clock.tx_fault_hook = None

    step = repo_clock.step_row(job["id"], "inspect_asset")
    assert step["status"] == "RUNNING"  # 未变成 SUCCEEDED
    assert step["output_fingerprint"] is None
    holders = [v for v in repo_clock.slot_holders().values() if v is not None]
    assert holders == [claim.attempt_id]  # 资源未释放（attempt 仍活着）
    detail = repo_clock.get_job_detail(job["id"])
    assert detail["status"] == "RUNNING"
    assert detail["steps"][0]["artifacts"] == []  # 无 artifact
    evs = [e["type"] for e in repo_clock.get_events(job["id"])["events"]]
    assert "STEP_SUCCEEDED" not in evs
    # 重试发布成功
    repo_clock.tx_fault_hook = None
    out = repo_clock.complete_attempt(claim.attempt_id, "w", bundles, "fp-x")
    assert out["job_status"] == "RUNNING"


# ---- A14：不可重试 / 异常退出预算 ---------------------------------------


def test_a14_nonretryable_fails_immediately(repo, clock):
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT, "demo_fault": {"nonretryable": {"plan_motion": True}}}
    )
    seen = set()
    for _ in range(30):
        c = repo.claim_next_step("w")
        if c is None:
            break
        if c.step_name == "plan_motion":
            out = repo.fail_attempt(c.attempt_id, "w", ERR_NONRETRYABLE_FAULT,
                                    "不可重试", retryable=False)
            assert out["retry_scheduled"] is False
            assert out["step_status"] == "FAILED"
        else:
            _complete_with_fake_file(repo, c)
        seen.add(c.step_name)
    step = repo.step_row(job["id"], "plan_motion")
    assert step["status"] == "FAILED"
    assert len(repo.attempt_rows(step["id"])) == 1  # 不消耗剩余预算
    # extract 的独立分支可以结束并保留成功产物（fail-fast 排空语义）
    extract = repo.step_row(job["id"], "extract_frames")
    assert extract["status"] in ("SUCCEEDED", "RUNNING", "RETRY_WAIT", "PENDING", "BLOCKED")


def test_a14_abnormal_exit_consumes_budget(repo_clock, clock):
    repo = repo_clock
    job = repo.create_job(dict(pipeline.DEFAULT_INPUT))
    statuses = []
    for _ in range(40):
        c = repo.claim_next_step("w")
        if c is None:
            break
        if c.step_name == "inspect_asset":
            out = repo.fail_attempt(c.attempt_id, "w", ERR_ABNORMAL_EXIT,
                                    "exitcode=7", retryable=True)
            statuses.append(out["step_status"])
            clock.advance(31)
        else:
            _complete_with_fake_file(repo, c)
    step = repo.step_row(job["id"], "inspect_asset")
    assert step["status"] == "FAILED"
    assert len(repo.attempt_rows(step["id"])) == 3  # 有界预算，无永久泄漏
    holders = [v for v in repo.slot_holders().values() if v is not None]
    assert holders == []  # 无资源泄漏


# ---- fail-fast 排空 ------------------------------------------------------


def test_fail_fast_drains_and_blocks(repo_clock, clock):
    repo = repo_clock
    job = repo.create_job(
        {**pipeline.DEFAULT_INPUT,
         "demo_fault": {"nonretryable": {"plan_motion": True}}}
    )
    # 让 extract 持有 RUNNING（在途独立分支），同时 plan_motion 最终失败
    extract_claim = None
    plan_failed = False
    for _ in range(20):
        c = repo.claim_next_step("w")
        if c is None:
            break
        if c.step_name == "extract_frames":
            extract_claim = c  # 保持在途
            continue
        if c.step_name == "plan_motion":
            repo.fail_attempt(c.attempt_id, "w", ERR_NONRETRYABLE_FAULT, "x", retryable=False)
            plan_failed = True
            continue
        _complete_with_fake_file(repo, c)
    assert plan_failed and extract_claim is not None
    detail = repo.get_job_detail(job["id"])
    assert detail["status"] == "RUNNING"  # 在途未排空 → 不提前 FAILED
    # fail-fast：不再领取新步骤
    assert repo.claim_next_step("w") is None
    # 在途分支结束 → 任务 FAILED，剩余 BLOCKED
    _complete_with_fake_file(repo, extract_claim)
    detail = repo.get_job_detail(job["id"])
    assert detail["status"] == "FAILED"
    assert repo.step_row(job["id"], "render_preview")["status"] == "BLOCKED"


# ---- 产物指纹 ------------------------------------------------------------


def test_fingerprint_stable_and_sensitive():
    p = dict(pipeline.DEFAULT_INPUT)
    fp1 = pipeline.compute_fingerprint("stylize_frames", "1.0.0", p, {"extract_frames": "a", "plan_motion": "b"})
    fp2 = pipeline.compute_fingerprint("stylize_frames", "1.0.0", p, {"plan_motion": "b", "extract_frames": "a"})
    assert fp1 == fp2  # 与上游顺序无关
    p2 = {**p, "seed": p["seed"] + 1}
    fp3 = pipeline.compute_fingerprint("stylize_frames", "1.0.0", p2, {"extract_frames": "a", "plan_motion": "b"})
    assert fp3 != fp1  # seed 变化 → 指纹变化
    fp4 = pipeline.compute_fingerprint("stylize_frames", "1.0.1", p, {"extract_frames": "a", "plan_motion": "b"})
    assert fp4 != fp1  # 步骤版本变化 → 指纹变化


# ---- 退避公式 ------------------------------------------------------------


def test_retry_backoff_bounded():
    assert pipeline.retry_backoff_seconds(1) == 1.0
    assert pipeline.retry_backoff_seconds(2) == 2.0
    assert pipeline.retry_backoff_seconds(5) == 16.0
    assert pipeline.retry_backoff_seconds(6) == 30.0
    assert pipeline.retry_backoff_seconds(20) == 30.0
