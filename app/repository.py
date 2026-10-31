"""仓储层：所有业务状态写入的唯一入口。

不变量（plan.md 第 4 节）：
- 创建任务与全部步骤同一事务；
- claim 校验依赖/产物/重试时间/资源，占槽与建 attempt 同事务；
- attempt_id 是唯一执行身份：heartbeat/complete/fail/release 必须匹配
  active_attempt_id、owner 且租约有效；旧 attempt 的写入一律拒绝；
- 步骤成功、attempt 终态、artifact、资源释放、事件、任务聚合同一事务；
- 重试有上限，历史追加不覆盖。

时间通过 now_fn 注入，便于精确测试租约与退避；生产默认 time.time。
每个方法使用独立连接（每进程短事务），涉及竞争的路径 BEGIN IMMEDIATE。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from app import artifacts as art
from app import pipeline
from app.db import connect, write_tx, init_db
from app.domain import (
    ArtifactInvalid,
    AttemptStatus,
    ERR_ARTIFACT_INVALID,
    ERR_PIPELINE_VERSION,
    ERR_WORKER_LOST,
    EventType,
    InvalidJobState,
    IdentityRejected,
    JobStatus,
    REJ_LEASE,
    REJ_NOT_ACTIVE,
    REJ_OWNER,
    REJ_SLOT,
    REJ_STATUS,
    StepStatus,
    is_retryable,
)


def _now_system() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ClaimedStep:
    """claim 成功后交给监督进程启动子进程的全部信息。"""

    job_id: str
    step_id: str
    step_name: str
    step_version: str
    attempt_id: str
    attempt_no: int
    retry_round: int
    owner: str
    resource: str
    work_dir: str            # 绝对路径
    deadline_at: float
    lease_expires_at: float
    timeout_seconds: float
    input_params: dict[str, Any]
    demo_fault: dict[str, Any]
    upstream: dict[str, dict[str, Any]]   # dep -> {relative_path, absolute_dir, manifest, files}
    pipeline_version: str


@dataclass
class RecoveredAttempt:
    attempt_id: str
    job_id: str
    step_id: str
    step_name: str
    retry_scheduled: bool
    next_run_at: float | None


@dataclass
class ArtifactRecord:
    id: str
    step_id: str
    attempt_id: str
    job_id: str
    relative_path: str
    sha256: str
    size_bytes: int
    kind: str
    manifest: dict[str, Any]
    created_at: float


class Repository:
    def __init__(
        self,
        db_path: Path | str,
        artifacts_root: Path | str,
        lease_seconds: float = 10.0,
        busy_timeout_ms: int = 5000,
        now_fn: Callable[[], float] | None = None,
        step_timeout_override: float = 0.0,
    ):
        self.db_path = Path(db_path)
        self.artifacts_root = Path(artifacts_root)
        self.lease_seconds = lease_seconds
        self.busy_timeout_ms = busy_timeout_ms
        self.now_fn: Callable[[], float] = now_fn or _now_system
        # >0 时覆盖步骤墙钟期限（测试缩短超时用）
        self.step_timeout_override = step_timeout_override
        # 测试可替换的事务内注入点；生产恒为 None
        self.tx_fault_hook: Callable[[str], None] | None = None

    # ---- 基础设施 --------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path, self.busy_timeout_ms)

    def now(self) -> float:
        return float(self.now_fn())

    def _inject(self, point: str) -> None:
        if self.tx_fault_hook is not None:
            self.tx_fault_hook(point)

    def _tx(self, conn: sqlite3.Connection):
        return write_tx(conn)

    # ---- 事件 ------------------------------------------------------------

    def _event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        etype: EventType,
        payload: dict[str, Any],
        step_id: str | None = None,
        attempt_id: str | None = None,
        now: float | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO events(job_id, step_id, attempt_id, type, payload_json, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (job_id, step_id, attempt_id, etype.value,
             json.dumps(payload, ensure_ascii=True, sort_keys=True), now if now is not None else self.now()),
        )

    # ---- 创建任务 --------------------------------------------------------

    def create_job(
        self,
        params: dict[str, Any],
        demo_fault: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建任务及全部步骤：单事务。返回任务摘要。

        params 中内嵌的 demo_fault 会被拆分为故障配置（不属于业务参数，
        不进业务指纹）。
        """
        now = self.now()
        job_id = new_id()
        snapshot = pipeline.dag_snapshot()
        params = dict(params)
        if demo_fault is None and "demo_fault" in params:
            demo_fault = params.pop("demo_fault")
        params = pipeline.normalize_input(params)
        fault = demo_fault or {}
        input_json = json.dumps(
            {"params": params, "demo_fault": fault}, ensure_ascii=True, sort_keys=True
        )
        with self._conn() as conn:
            with self._tx(conn) as c:
                c.execute(
                    "INSERT INTO jobs(id, status, input_json, input_hash, pipeline_name,"
                    " pipeline_version, dag_snapshot, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (job_id, JobStatus.QUEUED.value, input_json,
                     pipeline.business_input_hash(params), pipeline.PIPELINE_NAME,
                     pipeline.PIPELINE_VERSION, json.dumps(snapshot, sort_keys=True), now, now),
                )
                for sd in pipeline.STEPS:
                    c.execute(
                        "INSERT INTO steps(id, job_id, name, version, order_index, depends_on_json,"
                        " status, resource, max_attempts_per_round, next_run_at, timeout_seconds,"
                        " created_at, updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (new_id(), job_id, sd.name, sd.version, pipeline.STEP_ORDER[sd.name],
                         json.dumps(list(sd.depends_on)), StepStatus.PENDING.value, sd.resource,
                         sd.max_attempts_per_round, now, sd.timeout_seconds, now, now),
                    )
                self._event(c, job_id, EventType.JOB_CREATED,
                            {"params": params, "fault_types": sorted(fault)}, now=now)
                self._inject("create_job")
        return self.get_job_detail(job_id)

    # ---- 查询 ------------------------------------------------------------

    def _job_row(self, conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = self._job_row(conn, job_id)
            return dict(row) if row else None
        finally:
            conn.close()

    def list_jobs(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return {"total": total, "limit": limit, "offset": offset,
                    "items": [self._job_summary(conn, dict(r)) for r in rows]}
        finally:
            conn.close()

    def _steps_rows(self, conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM steps WHERE job_id=? ORDER BY order_index", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _job_summary(self, conn: sqlite3.Connection, job: dict[str, Any]) -> dict[str, Any]:
        steps = self._steps_rows(conn, job["id"])
        done = sum(1 for s in steps if s["status"] == StepStatus.SUCCEEDED.value)
        return {
            "id": job["id"],
            "status": job["status"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "input_hash": job["input_hash"],
            "pipeline_version": job["pipeline_version"],
            "steps_done": done,
            "steps_total": len(steps),
            "fatal_error_code": job["fatal_error_code"],
        }

    def list_jobs_summary(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.list_jobs(limit, offset)

    def wait_reason(self, conn: sqlite3.Connection, step: dict[str, Any],
                    steps_by_name: dict[str, dict[str, Any]], now: float) -> dict[str, Any] | None:
        """PENDING/RETRY_WAIT 的等待原因：查询时计算，不落事件。"""
        st = step["status"]
        if st == StepStatus.RETRY_WAIT.value:
            return {"reason": "RETRY_BACKOFF", "next_run_at": step["next_run_at"],
                    "message": f"等待自动重试（第 {step['attempts_in_round']}/{step['max_attempts_per_round']} 次尝试后退避）"}
        if st != StepStatus.PENDING.value:
            return None
        deps = json.loads(step["depends_on_json"])
        missing = [d for d in deps
                   if steps_by_name[d]["status"] != StepStatus.SUCCEEDED.value]
        if missing:
            return {"reason": "DEPENDENCIES", "message": f"等待上游: {', '.join(missing)}"}
        # 依赖已齐 → 资源或等待领取
        busy = conn.execute(
            "SELECT COUNT(*) AS n FROM resource_slots WHERE kind=? AND holder_attempt_id IS NOT NULL",
            (step["resource"],),
        ).fetchone()["n"]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM resource_slots WHERE kind=?", (step["resource"],)
        ).fetchone()["n"]
        if busy >= total:
            return {"reason": f"{step['resource'].upper()}_RESOURCE",
                    "message": f"等待 {step['resource'].upper()} 资源槽位"}
        return {"reason": "READY", "message": "依赖已满足，等待 Worker 领取"}

    def artifact_health_for_job(self, conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        """完整健康检查（存在+尺寸+摘要），用于任务详情展示。

        产物均为小文件（KB 级），任务详情可承受全量校验；“已成功”与
        “当前文件仍可下载”是两个概念（见 README/docs）。
        """
        rows = conn.execute("SELECT * FROM artifacts WHERE job_id=?", (job_id,)).fetchall()
        invalid: list[dict[str, Any]] = []
        checked = 0
        for r in rows:
            checked += 1
            problems = art.verify_published_artifact(
                self.artifacts_root, r["relative_path"], r["sha256"], r["size_bytes"],
                _artifact_is_dir(r["manifest_json"]), json.loads(r["manifest_json"]),
            )
            if problems:
                invalid.append({"artifact_id": r["id"], "relative_path": r["relative_path"],
                                "kind": r["kind"], "problem": "; ".join(problems[:3])})
        status = "valid" if not invalid else "invalid"
        return {"status": status if checked else "unknown", "checked": checked,
                "invalid": invalid}

    def get_job_detail(self, job_id: str) -> dict[str, Any]:
        conn = self._conn()
        try:
            row = self._job_row(conn, job_id)
            if row is None:
                raise LookupError(job_id)
            job = dict(row)
            now = self.now()
            steps = self._steps_rows(conn, job_id)
            steps_by_name = {s["name"]: s for s in steps}
            out_steps = []
            for s in steps:
                dep = json.loads(s["depends_on_json"])
                attempts = conn.execute(
                    "SELECT COUNT(*) AS n FROM attempts WHERE step_id=?", (s["id"],)
                ).fetchone()["n"]
                lease = None
                if s["active_attempt_id"]:
                    a = conn.execute(
                        "SELECT * FROM attempts WHERE id=?", (s["active_attempt_id"],)
                    ).fetchone()
                    if a:
                        lease = {"attempt_id": a["id"], "owner": a["owner"],
                                 "lease_expires_at": a["lease_expires_at"],
                                 "heartbeat_at": a["heartbeat_at"],
                                 "expired": a["lease_expires_at"] <= now}
                arts = conn.execute(
                    "SELECT id, attempt_id, kind, relative_path, sha256, size_bytes, created_at"
                    " FROM artifacts WHERE step_id=? ORDER BY created_at", (s["id"],)
                ).fetchall()
                item = {
                    "id": s["id"], "name": s["name"], "version": s["version"],
                    "status": s["status"], "resource": s["resource"],
                    "depends_on": dep, "order_index": s["order_index"],
                    "attempt_count": s["attempt_count"],
                    "retry_round": s["retry_round"],
                    "attempts_in_round": s["attempts_in_round"],
                    "max_attempts_per_round": s["max_attempts_per_round"],
                    "next_run_at": s["next_run_at"],
                    "timeout_seconds": s["timeout_seconds"],
                    "progress": s["progress"],
                    "progress_message": s["progress_message"],
                    "progress_updated_at": s["progress_updated_at"],
                    "output_fingerprint": s["output_fingerprint"],
                    "error_code": s["error_code"],
                    "error_message": s["error_message"],
                    "lease": lease,
                    "wait": self.wait_reason(conn, s, steps_by_name, now),
                    "artifacts": [dict(a) for a in arts],
                }
                out_steps.append(item)
            params, fault = _parse_input(job["input_json"])
            return {
                "id": job["id"], "status": job["status"],
                "input": {"params": params, "demo_fault": fault},
                "input_hash": job["input_hash"],
                "pipeline_name": job["pipeline_name"],
                "pipeline_version": job["pipeline_version"],
                "dag": json.loads(job["dag_snapshot"]),
                "fatal_error_code": job["fatal_error_code"],
                "fatal_error_message": job["fatal_error_message"],
                "created_at": job["created_at"], "updated_at": job["updated_at"],
                "started_at": job["started_at"], "finished_at": job["finished_at"],
                "steps": out_steps,
                "artifact_health": self.artifact_health_for_job(conn, job_id),
            }
        finally:
            conn.close()

    def get_attempts(self, job_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        conn = self._conn()
        try:
            if self._job_row(conn, job_id) is None:
                raise LookupError(job_id)
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE job_id=?", (job_id,)
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT a.*, s.name AS step_name FROM attempts a JOIN steps s ON s.id=a.step_id"
                " WHERE a.job_id=? ORDER BY a.started_at, a.attempt_no LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                d["error"] = {"code": d.pop("error_code"), "message": d.pop("error_message")}
                items.append(d)
            return {"total": total, "limit": limit, "offset": offset, "items": items}
        finally:
            conn.close()

    def get_events(self, job_id: str, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
        """按 id 升序的增量事件。消费规则：始终以返回的 next_after_id 继续。"""
        limit = max(1, min(limit, 500))
        after_id = max(0, after_id)
        conn = self._conn()
        try:
            if self._job_row(conn, job_id) is None:
                raise LookupError(job_id)
            rows = conn.execute(
                "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (job_id, after_id, limit),
            ).fetchall()
            items = []
            for r in rows:
                items.append({
                    "id": r["id"], "job_id": r["job_id"], "step_id": r["step_id"],
                    "attempt_id": r["attempt_id"], "type": r["type"],
                    "payload": json.loads(r["payload_json"]), "created_at": r["created_at"],
                })
            next_after = items[-1]["id"] if items else after_id
            return {"events": items, "next_after_id": next_after,
                    "has_more": len(items) == limit}
        finally:
            conn.close()

    def list_resources(self) -> dict[str, Any]:
        conn = self._conn()
        try:
            now = self.now()
            slots = conn.execute("SELECT * FROM resource_slots ORDER BY id").fetchall()
            out = []
            for s in slots:
                holder = None
                expired = False
                if s["holder_attempt_id"]:
                    a = conn.execute(
                        "SELECT a.owner, a.lease_expires_at, a.status, a.job_id, s2.name AS step_name"
                        " FROM attempts a JOIN steps s2 ON s2.id=a.step_id WHERE a.id=?",
                        (s["holder_attempt_id"],),
                    ).fetchone()
                    if a:
                        expired = a["lease_expires_at"] <= now
                        holder = {"attempt_id": s["holder_attempt_id"], "owner": a["owner"],
                                  "job_id": a["job_id"], "step_name": a["step_name"],
                                  "lease_expires_at": a["lease_expires_at"],
                                  "attempt_status": a["status"]}
                    else:
                        holder = {"attempt_id": s["holder_attempt_id"], "owner": None,
                                  "note": "holder attempt 不存在（等待回收）"}
                out.append({
                    "id": s["id"], "kind": s["kind"], "device_label": s["device_label"],
                    "holder": holder, "occupied": holder is not None, "holder_expired": expired,
                    "metrics": "未采集 / MOCK（P0 仅逻辑槽位预约，无真实 GPU 指标）",
                })
            return {"slots": out, "server_time": now}
        finally:
            conn.close()

    # ---- 领取 ------------------------------------------------------------

    def _precheck_upstream_artifacts(
        self, conn: sqlite3.Connection, job: dict[str, Any], deps: Iterable[str],
        now: float,
    ) -> str | None:
        """领取前校验成功上游产物的磁盘完整性。损坏 → 任务级 fatal，返回错误码。"""
        for dep_name in deps:
            row = conn.execute(
                "SELECT * FROM steps WHERE job_id=? AND name=?", (job["id"], dep_name)
            ).fetchone()
            if row is None or row["status"] != StepStatus.SUCCEEDED.value:
                continue
            arts = conn.execute("SELECT * FROM artifacts WHERE step_id=?", (row["id"],)).fetchall()
            for a in arts:
                problems = art.verify_published_artifact(
                    self.artifacts_root, a["relative_path"], a["sha256"],
                    a["size_bytes"], _artifact_is_dir(a["manifest_json"]),
                    json.loads(a["manifest_json"]),
                )
                if problems:
                    self._set_job_fatal(
                        conn, job["id"], ERR_ARTIFACT_INVALID,
                        f"上游 {dep_name} 产物损坏: {'; '.join(problems)}", now,
                        artifact_id=a["id"], step_name=dep_name,
                    )
                    return ERR_ARTIFACT_INVALID
        return None

    def _set_job_fatal(
        self, conn: sqlite3.Connection, job_id: str, code: str, message: str,
        now: float | None, artifact_id: str | None = None, step_name: str | None = None,
    ) -> None:
        ts = now if now is not None else self.now()
        conn.execute(
            "UPDATE jobs SET fatal_error_code=?, fatal_error_message=?, updated_at=?"
            " WHERE id=? AND fatal_error_code IS NULL",
            (code, message[:500], ts, job_id),
        )
        if artifact_id is not None:
            self._event(conn, job_id, EventType.ARTIFACT_INVALID,
                        {"code": code, "message": message[:500], "step": step_name},
                        now=ts)
        else:
            self._event(conn, job_id, EventType.JOB_FATAL_ERROR,
                        {"code": code, "message": message[:500]}, now=ts)

    def claim_next_step(self, worker_id: str) -> ClaimedStep | None:
        """公平扫描（job created_at → step order_index），同事务建 attempt + 占槽。

        资源忙时跳过继续找其他工作；不为“等待资源”消耗 attempt。
        时间在 BEGIN IMMEDIATE 之后取：等待写锁期间到达的重试窗口、
        新到期的 next_run_at 都以新时间为准。
        """
        with self._conn() as conn:
            with self._tx(conn) as c:
                self._inject("claim_begin")
                now = self.now()
                jobs = c.execute(
                    "SELECT * FROM jobs WHERE status IN (?,?) ORDER BY created_at, id",
                    (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
                ).fetchall()
                for job in jobs:
                    claimed = self._try_claim_job(c, dict(job), worker_id, now)
                    if claimed is not None:
                        self._inject("claim_before_commit")
                        return claimed
                return None

    def _try_claim_job(
        self, c: sqlite3.Connection, job: dict[str, Any], worker_id: str, now: float
    ) -> ClaimedStep | None:
        # fail-fast：出现最终失败或任务级 fatal 后不再领取新步骤
        if job["fatal_error_code"]:
            return None
        steps = self._steps_rows(c, job["id"])
        if any(s["status"] == StepStatus.FAILED.value for s in steps):
            return None
        # 版本支持检查：任务级错误必须停止新领取并聚合终态
        if job["pipeline_version"] not in pipeline.SUPPORTED_PIPELINE_VERSIONS:
            self._set_job_fatal(
                c, job["id"], ERR_PIPELINE_VERSION,
                f"pipeline_version={job['pipeline_version']} 不受当前 Worker 支持",
                now,
            )
            self._aggregate_job(c, job["id"], now)
            return None
        steps_by_name = {s["name"]: s for s in steps}
        for s in steps:
            st = s["status"]
            if st == StepStatus.PENDING.value:
                deps = json.loads(s["depends_on_json"])
                if not all(steps_by_name[d]["status"] == StepStatus.SUCCEEDED.value for d in deps):
                    continue
            elif st == StepStatus.RETRY_WAIT.value:
                if s["next_run_at"] is not None and s["next_run_at"] > now:
                    continue
            else:
                continue
            # 支持的步骤版本
            cur = pipeline.STEP_BY_NAME.get(s["name"])
            if cur is None or cur.version != s["version"]:
                self._set_job_fatal(
                    c, job["id"], ERR_PIPELINE_VERSION,
                    f"步骤 {s['name']} 版本 {s['version']} 与当前代码不一致", now,
                )
                self._aggregate_job(c, job["id"], now)
                return None
            # 依赖产物完整性预检（A11：损坏检测必须发生在新依赖执行前）
            deps = json.loads(s["depends_on_json"])
            if self._precheck_upstream_artifacts(c, job, deps, now) is not None:
                self._aggregate_job(c, job["id"], now)
                return None
            # 收集上游产物清单——必须在占槽之前：缺产物时干净地拒绝并聚合，
            # 不留半套 claim（attempt/槽位/步骤已提交却无人执行）
            _, fault = _parse_input(job["input_json"])
            upstream: dict[str, dict[str, Any]] = {}
            for d in deps:
                drow = steps_by_name[d]
                darts = c.execute(
                    "SELECT * FROM artifacts WHERE step_id=? ORDER BY created_at", (drow["id"],)
                ).fetchall()
                if not darts:
                    self._set_job_fatal(c, job["id"], ERR_ARTIFACT_INVALID,
                                        f"上游 {d} SUCCEEDED 但无产物记录", now)
                    self._aggregate_job(c, job["id"], now)
                    return None
                upstream[d] = {
                    "artifacts": [
                        {
                            "relative_path": a["relative_path"],
                            "absolute_path": str((self.artifacts_root / a["relative_path"]).resolve()),
                            "kind": a["kind"],
                            "sha256": a["sha256"],
                            "manifest": json.loads(a["manifest_json"]),
                        }
                        for a in darts
                    ]
                }
            # 资源槽位
            slot = c.execute(
                "SELECT * FROM resource_slots WHERE kind=? AND holder_attempt_id IS NULL"
                " ORDER BY id LIMIT 1",
                (s["resource"],),
            ).fetchone()
            if slot is None:
                continue  # 资源忙 → 跳过找其他工作，不消耗 attempt
            # 建 attempt + 占槽
            attempt_id = new_id()
            attempt_no = s["attempt_count"] + 1
            if st == StepStatus.PENDING.value:
                attempts_in_round = 1
            else:
                attempts_in_round = s["attempts_in_round"] + 1
            work_dir = self.artifacts_root / job["id"] / s["name"] / f"a{attempt_no}"
            work_dir.mkdir(parents=True, exist_ok=True)
            lease_expires = now + self.lease_seconds
            effective_timeout = (
                self.step_timeout_override if self.step_timeout_override > 0
                else s["timeout_seconds"]
            )
            deadline = now + effective_timeout
            # 先插 attempt（slot.holder 外键引用它），再条件占槽
            c.execute(
                "INSERT INTO attempts(id, step_id, job_id, attempt_no, retry_round, owner, status,"
                " lease_expires_at, heartbeat_at, started_at, deadline_at, work_dir)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, s["id"], job["id"], attempt_no, s["retry_round"], worker_id,
                 AttemptStatus.RUNNING.value, lease_expires, now, now, deadline, str(work_dir)),
            )
            upd = c.execute(
                "UPDATE resource_slots SET holder_attempt_id=? WHERE id=? AND holder_attempt_id IS NULL",
                (attempt_id, slot["id"]),
            )
            if upd.rowcount != 1:
                raise RuntimeError(f"占槽失败: slot={slot['id']}")
            self._inject("claim_after_slot")
            c.execute(
                "UPDATE steps SET status=?, active_attempt_id=?, attempt_count=?,"
                " attempts_in_round=?, next_run_at=NULL, progress=0, progress_message=NULL,"
                " progress_updated_at=NULL, error_code=NULL, error_message=NULL, updated_at=?"
                " WHERE id=? AND status=?",
                (StepStatus.RUNNING.value, attempt_id, attempt_no, attempts_in_round,
                 now, s["id"], st),
            )
            if c.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError("step claim 条件更新失败")
            c.execute(
                "UPDATE jobs SET status=?, started_at=COALESCE(started_at, ?), updated_at=?"
                " WHERE id=? AND status IN (?,?)",
                (JobStatus.RUNNING.value, now, now, job["id"],
                 JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            )
            self._event(c, job["id"], EventType.STEP_CLAIMED,
                        {"step": s["name"], "attempt_no": attempt_no, "retry_round": s["retry_round"],
                         "resource": s["resource"], "worker": worker_id, "slot": slot["id"]},
                        step_id=s["id"], attempt_id=attempt_id, now=now)
            return ClaimedStep(
                job_id=job["id"], step_id=s["id"], step_name=s["name"], step_version=s["version"],
                attempt_id=attempt_id, attempt_no=attempt_no, retry_round=s["retry_round"],
                owner=worker_id, resource=s["resource"], work_dir=str(work_dir),
                deadline_at=deadline, lease_expires_at=lease_expires,
                timeout_seconds=effective_timeout,
                input_params=_parse_input(job["input_json"])[0], demo_fault=fault,
                upstream=upstream, pipeline_version=job["pipeline_version"],
            )
        return None

    # ---- 身份校验（heartbeat/complete/fail 共用）--------------------------

    def _load_active_attempt(
        self, conn: sqlite3.Connection, attempt_id: str, owner: str, now: float
    ) -> dict[str, Any]:
        a = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if a is None:
            raise IdentityRejected(REJ_STATUS, f"attempt {attempt_id} 不存在")
        if a["owner"] != owner:
            raise IdentityRejected(REJ_OWNER, f"owner 不匹配: 期望 {a['owner']}，实际 {owner}")
        if a["status"] != AttemptStatus.RUNNING.value:
            raise IdentityRejected(REJ_STATUS, f"attempt 状态为 {a['status']}，非 RUNNING")
        if a["lease_expires_at"] <= now:
            raise IdentityRejected(
                REJ_LEASE, f"租约已于 {a['lease_expires_at']} 过期（now={now}），旧 attempt 不可提交"
            )
        s = conn.execute("SELECT * FROM steps WHERE id=?", (a["step_id"],)).fetchone()
        if s is None or s["active_attempt_id"] != attempt_id:
            raise IdentityRejected(REJ_NOT_ACTIVE, "该 attempt 已不是步骤的 active_attempt")
        return {"attempt": dict(a), "step": dict(s)}

    def _release_slot(self, conn: sqlite3.Connection, attempt_id: str, now: float) -> None:
        upd = conn.execute(
            "UPDATE resource_slots SET holder_attempt_id=NULL WHERE holder_attempt_id=?",
            (attempt_id,),
        )
        if upd.rowcount != 1:
            raise IdentityRejected(REJ_SLOT, f"释放资源失败：{attempt_id} 不是任何槽位的有效 holder")

    # ---- 心跳与进度 ------------------------------------------------------

    def heartbeat(
        self, attempt_id: str, owner: str,
        progress: float | None = None, progress_message: str | None = None,
    ) -> dict[str, Any]:
        """续租 + 限频持久化进展。

        时间在 BEGIN IMMEDIATE 之后取：方法入口到拿到写锁之间的时间流逝
        （等待锁、调度延迟）必须计入租约检查，过期即拒绝，不得续活。
        """
        with self._conn() as conn:
            with self._tx(conn):
                self._inject("heartbeat_begin")
                now = self.now()
                ctx = self._load_active_attempt(conn, attempt_id, owner, now)
                step = ctx["step"]
                new_lease = now + self.lease_seconds
                conn.execute(
                    "UPDATE attempts SET heartbeat_at=?, lease_expires_at=? WHERE id=?",
                    (now, new_lease, attempt_id),
                )
                progress_persisted = False
                if progress is not None:
                    last = step["progress_updated_at"]
                    if last is None or (now - last) >= 1.0:
                        conn.execute(
                            "UPDATE steps SET progress=?, progress_message=?,"
                            " progress_updated_at=?, updated_at=? WHERE id=?",
                            (max(0.0, min(1.0, float(progress))), progress_message, now, now,
                             step["id"]),
                        )
                        progress_persisted = True
                return {"ok": True, "lease_expires_at": new_lease,
                        "progress_persisted": progress_persisted}

    # ---- 完成 ------------------------------------------------------------

    def complete_attempt(
        self, attempt_id: str, owner: str, bundles: list[art.OutputBundle],
        fingerprint: str,
    ) -> dict[str, Any]:
        """发布成功：先在事务外校验文件，再在租约保护的事务内发布。

        返回 {"job_status": ...}。身份/租约不符抛 IdentityRejected。
        持有权检查在 BEGIN IMMEDIATE 之后用**新的时间**执行：文件校验与
        等待写锁期间的流逝都计入租约，过期不得发布。
        """
        # 1) 事务外校验产物完整性（plan.md §8：校验后再发布）
        work_dir = None
        with self._conn() as conn:
            a = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if a is None:
                raise IdentityRejected(REJ_STATUS, f"attempt {attempt_id} 不存在")
            work_dir = Path(a["work_dir"])
        problems: list[str] = []
        for b in bundles:
            problems.extend(art.verify_bundle(work_dir, b))
        if problems:
            raise ArtifactInvalid("ARTIFACT_MISMATCH", "; ".join(problems[:5]))
        # 2) 受租约保护的发布事务
        with self._conn() as conn:
            with self._tx(conn):
                self._inject("complete_begin")
                now = self.now()
                ctx = self._load_active_attempt(conn, attempt_id, owner, now)
                step = ctx["step"]
                job_id = step["job_id"]
                for b in bundles:
                    conn.execute(
                        "INSERT INTO artifacts(id, step_id, attempt_id, job_id, relative_path,"
                        " sha256, size_bytes, kind, manifest_json, created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (new_id(), step["id"], attempt_id, job_id,
                         f"{job_id}/{step['name']}/a{step['attempt_count']}/{b.relative_path}",
                         art.bundle_digest(b), b.total_size, b.kind,
                         json.dumps(b.to_json(), ensure_ascii=True, sort_keys=True), now),
                    )
                conn.execute(
                    "UPDATE attempts SET status=?, finished_at=?, error_code=NULL,"
                    " error_message=NULL WHERE id=?",
                    (AttemptStatus.SUCCEEDED.value, now, attempt_id),
                )
                conn.execute(
                    "UPDATE steps SET status=?, output_fingerprint=?, progress=1.0,"
                    " progress_message=NULL, progress_updated_at=?, active_attempt_id=NULL,"
                    " error_code=NULL, error_message=NULL, updated_at=? WHERE id=?"
                    " AND active_attempt_id=?",
                    (StepStatus.SUCCEEDED.value, fingerprint, now, now, step["id"], attempt_id),
                )
                self._release_slot(conn, attempt_id, now)
                self._event(conn, job_id, EventType.STEP_SUCCEEDED,
                            {"step": step["name"], "attempt_no": ctx["attempt"]["attempt_no"],
                             "fingerprint": fingerprint,
                             "artifacts": [b.kind for b in bundles]},
                            step_id=step["id"], attempt_id=attempt_id, now=now)
                self._inject("complete_before_aggregate")
                job_status = self._aggregate_job(conn, job_id, now)
                return {"job_status": job_status}

    # ---- 失败 ------------------------------------------------------------

    def fail_attempt(
        self, attempt_id: str, owner: str, error_code: str, message: str,
        retryable: bool | None = None,
    ) -> dict[str, Any]:
        """失败/超时/异常退出的统一路径。可重试 → RETRY_WAIT + 退避；否则步骤 FAILED。

        持有权检查在 BEGIN IMMEDIATE 之后取时；等待写锁期间过期即拒绝。
        """
        if retryable is None:
            retryable = is_retryable(error_code)
        with self._conn() as conn:
            with self._tx(conn):
                self._inject("fail_begin")
                now = self.now()
                ctx = self._load_active_attempt(conn, attempt_id, owner, now)
                step = ctx["step"]
                attempt = ctx["attempt"]
                job_id = step["job_id"]
                conn.execute(
                    "UPDATE attempts SET status=?, finished_at=?, error_code=?, error_message=?"
                    " WHERE id=?",
                    (AttemptStatus.FAILED.value, now, error_code, message[:500], attempt_id),
                )
                conn.execute(
                    "UPDATE steps SET active_attempt_id=NULL, updated_at=? WHERE id=?"
                    " AND active_attempt_id=?",
                    (now, step["id"], attempt_id),
                )
                self._release_slot(conn, attempt_id, now)
                retry_scheduled = False
                next_run_at = None
                if retryable and step["attempts_in_round"] < step["max_attempts_per_round"]:
                    next_run_at = now + pipeline.retry_backoff_seconds(step["attempts_in_round"])
                    conn.execute(
                        "UPDATE steps SET status=?, next_run_at=?, error_code=?, error_message=?,"
                        " updated_at=? WHERE id=?",
                        (StepStatus.RETRY_WAIT.value, next_run_at, error_code, message[:500],
                         now, step["id"]),
                    )
                    self._event(conn, job_id, EventType.STEP_RETRY_SCHEDULED,
                                {"step": step["name"], "attempt_no": attempt["attempt_no"],
                                 "error_code": error_code, "next_run_at": next_run_at,
                                 "attempts_in_round": step["attempts_in_round"],
                                 "max_attempts_per_round": step["max_attempts_per_round"]},
                                step_id=step["id"], attempt_id=attempt_id, now=now)
                    retry_scheduled = True
                else:
                    conn.execute(
                        "UPDATE steps SET status=?, error_code=?, error_message=?, updated_at=?"
                        " WHERE id=?",
                        (StepStatus.FAILED.value, error_code, message[:500], now, step["id"]),
                    )
                    self._event(conn, job_id, EventType.STEP_FAILED,
                                {"step": step["name"], "attempt_no": attempt["attempt_no"],
                                 "error_code": error_code, "retryable": retryable,
                                 "message": message[:300]},
                                step_id=step["id"], attempt_id=attempt_id, now=now)
                self._inject("fail_before_aggregate")
                job_status = self._aggregate_job(conn, job_id, now)
                return {"job_status": job_status, "retry_scheduled": retry_scheduled,
                        "next_run_at": next_run_at,
                        "step_status": StepStatus.RETRY_WAIT.value if retry_scheduled
                        else StepStatus.FAILED.value}

    def fail_attempt_timed_out(self, attempt_id: str, owner: str, message: str) -> dict[str, Any]:
        with self._conn() as conn:
            with self._tx(conn):
                now = self.now()
                ctx = self._load_active_attempt(conn, attempt_id, owner, now)
                step = ctx["step"]
                attempt = ctx["attempt"]
                job_id = step["job_id"]
                conn.execute(
                    "UPDATE attempts SET status=?, finished_at=?, error_code=?, error_message=?"
                    " WHERE id=?",
                    (AttemptStatus.TIMED_OUT.value, now, "TIMED_OUT", message[:500], attempt_id),
                )
                conn.execute(
                    "UPDATE steps SET active_attempt_id=NULL, updated_at=? WHERE id=?"
                    " AND active_attempt_id=?",
                    (now, step["id"], attempt_id),
                )
                self._release_slot(conn, attempt_id, now)
                self._event(conn, job_id, EventType.ATTEMPT_TIMED_OUT,
                            {"step": step["name"], "attempt_no": attempt["attempt_no"],
                             "message": message[:300], "deadline_at": attempt["deadline_at"]},
                            step_id=step["id"], attempt_id=attempt_id, now=now)
                retry_scheduled = False
                next_run_at = None
                if step["attempts_in_round"] < step["max_attempts_per_round"]:
                    next_run_at = now + pipeline.retry_backoff_seconds(step["attempts_in_round"])
                    conn.execute(
                        "UPDATE steps SET status=?, next_run_at=?, error_code='TIMED_OUT',"
                        " error_message=?, updated_at=? WHERE id=?",
                        (StepStatus.RETRY_WAIT.value, next_run_at, message[:500], now, step["id"]),
                    )
                    self._event(conn, job_id, EventType.STEP_RETRY_SCHEDULED,
                                {"step": step["name"], "attempt_no": attempt["attempt_no"],
                                 "error_code": "TIMED_OUT", "next_run_at": next_run_at},
                                step_id=step["id"], attempt_id=attempt_id, now=now)
                    retry_scheduled = True
                else:
                    conn.execute(
                        "UPDATE steps SET status=?, error_code='TIMED_OUT', error_message=?,"
                        " updated_at=? WHERE id=?",
                        (StepStatus.FAILED.value, message[:500], now, step["id"]),
                    )
                    self._event(conn, job_id, EventType.STEP_FAILED,
                                {"step": step["name"], "attempt_no": attempt["attempt_no"],
                                 "error_code": "TIMED_OUT", "retryable": True},
                                step_id=step["id"], attempt_id=attempt_id, now=now)
                job_status = self._aggregate_job(conn, job_id, now)
                return {"job_status": job_status, "retry_scheduled": retry_scheduled,
                        "next_run_at": next_run_at}

    # ---- 租约回收 --------------------------------------------------------

    def recover_expired(self) -> list[RecoveredAttempt]:
        """回收过期租约：结束旧 attempt、释放它拥有的资源、安排重试或终止。"""
        recovered: list[RecoveredAttempt] = []
        with self._conn() as conn:
            with self._tx(conn):
                now = self.now()
                rows = conn.execute(
                    "SELECT a.*, s.name AS step_name, s.attempts_in_round,"
                    " s.max_attempts_per_round, s.active_attempt_id AS step_active"
                    " FROM attempts a JOIN steps s ON s.id=a.step_id"
                    " WHERE a.status=? AND a.lease_expires_at<=?",
                    (AttemptStatus.RUNNING.value, now),
                ).fetchall()
                jobs_touched: set[str] = set()
                for r in rows:
                    attempt_id = r["id"]
                    conn.execute(
                        "UPDATE attempts SET status=?, finished_at=?, error_code=?,"
                        " error_message=? WHERE id=? AND status=?",
                        (AttemptStatus.WORKER_LOST.value, now, ERR_WORKER_LOST,
                         "租约过期，监督进程心跳停止（可能被硬杀）", attempt_id,
                         AttemptStatus.RUNNING.value),
                    )
                    if r["step_active"] == attempt_id:
                        conn.execute(
                            "UPDATE steps SET active_attempt_id=NULL, updated_at=? WHERE id=?",
                            (now, r["step_id"]),
                        )
                    # 释放它拥有的槽位；holder 条件更新，绝不误释放新 attempt 的资源
                    released = conn.execute(
                        "UPDATE resource_slots SET holder_attempt_id=NULL WHERE holder_attempt_id=?",
                        (attempt_id,),
                    ).rowcount
                    self._event(conn, r["job_id"], EventType.ATTEMPT_WORKER_LOST,
                                {"step": r["step_name"], "attempt_no": r["attempt_no"],
                                 "owner": r["owner"], "lease_expires_at": r["lease_expires_at"],
                                 "slot_released": released == 1},
                                step_id=r["step_id"], attempt_id=attempt_id, now=now)
                    retry_scheduled = False
                    next_run_at = None
                    if r["attempts_in_round"] < r["max_attempts_per_round"]:
                        next_run_at = now + pipeline.retry_backoff_seconds(r["attempts_in_round"])
                        conn.execute(
                            "UPDATE steps SET status=?, next_run_at=?, error_code=?,"
                            " error_message=?, updated_at=? WHERE id=? AND status=?",
                            (StepStatus.RETRY_WAIT.value, next_run_at, ERR_WORKER_LOST,
                             "worker 丢失，等待自动重试", now, r["step_id"],
                             StepStatus.RUNNING.value),
                        )
                        self._event(conn, r["job_id"], EventType.STEP_RETRY_SCHEDULED,
                                    {"step": r["step_name"], "attempt_no": r["attempt_no"],
                                     "error_code": ERR_WORKER_LOST, "next_run_at": next_run_at},
                                    step_id=r["step_id"], attempt_id=attempt_id, now=now)
                        retry_scheduled = True
                    else:
                        conn.execute(
                            "UPDATE steps SET status=?, error_code=?, error_message=?,"
                            " updated_at=? WHERE id=? AND status=?",
                            (StepStatus.FAILED.value, ERR_WORKER_LOST,
                             "worker 丢失且重试预算耗尽", now, r["step_id"],
                             StepStatus.RUNNING.value),
                        )
                        self._event(conn, r["job_id"], EventType.STEP_FAILED,
                                    {"step": r["step_name"], "attempt_no": r["attempt_no"],
                                     "error_code": ERR_WORKER_LOST, "retryable": True},
                                    step_id=r["step_id"], attempt_id=attempt_id, now=now)
                    recovered.append(RecoveredAttempt(
                        attempt_id=attempt_id, job_id=r["job_id"], step_id=r["step_id"],
                        step_name=r["step_name"], retry_scheduled=retry_scheduled,
                        next_run_at=next_run_at,
                    ))
                    jobs_touched.add(r["job_id"])
                for job_id in jobs_touched:
                    self._aggregate_job(conn, job_id, now)
                return recovered

    # ---- 任务聚合 --------------------------------------------------------

    def _aggregate_job(self, conn: sqlite3.Connection, job_id: str, now: float) -> str:
        job = self._job_row(conn, job_id)
        if job is None:
            raise RuntimeError(f"job {job_id} 不存在")
        steps = self._steps_rows(conn, job_id)
        statuses = [s["status"] for s in steps]
        fatal = job["fatal_error_code"] is not None
        has_failed = StepStatus.FAILED.value in statuses
        running = StepStatus.RUNNING.value in statuses
        all_succeeded = all(st == StepStatus.SUCCEEDED.value for st in statuses)
        current = job["status"]
        if all_succeeded and not fatal:
            if current != JobStatus.SUCCEEDED.value:
                conn.execute(
                    "UPDATE jobs SET status=?, finished_at=?, updated_at=? WHERE id=?",
                    (JobStatus.SUCCEEDED.value, now, now, job_id),
                )
                self._event(conn, job_id, EventType.JOB_SUCCEEDED, {}, now=now)
            return JobStatus.SUCCEEDED.value
        if fatal or has_failed:
            if running:
                # 排空在途步骤后再置 FAILED；不要一边 FAILED 一边步骤 RUNNING
                if current != JobStatus.RUNNING.value:
                    conn.execute(
                        "UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status!=?",
                        (JobStatus.RUNNING.value, now, job_id, JobStatus.RUNNING.value),
                    )
                return JobStatus.RUNNING.value
            # 在途已排空：未完成步骤 BLOCKED，任务 FAILED
            for s in steps:
                if s["status"] in (StepStatus.PENDING.value, StepStatus.RETRY_WAIT.value):
                    conn.execute(
                        "UPDATE steps SET status=?, error_code='JOB_FAILED',"
                        " error_message='任务已失败，该步骤被阻塞', updated_at=? WHERE id=?",
                        (StepStatus.BLOCKED.value, now, s["id"]),
                    )
                    self._event(conn, job_id, EventType.STEP_BLOCKED,
                                {"step": s["name"], "reason": "JOB_FAILED"},
                                step_id=s["id"], now=now)
            if current != JobStatus.FAILED.value:
                conn.execute(
                    "UPDATE jobs SET status=?, finished_at=?, updated_at=? WHERE id=?",
                    (JobStatus.FAILED.value, now, now, job_id),
                )
                payload: dict[str, Any] = {}
                if fatal:
                    payload["fatal_error_code"] = job["fatal_error_code"]
                    payload["fatal_error_message"] = job["fatal_error_message"]
                failed_steps = [s["name"] for s in steps if s["status"] == StepStatus.FAILED.value]
                if failed_steps:
                    payload["failed_steps"] = failed_steps
                self._event(conn, job_id, EventType.JOB_FAILED, payload, now=now)
            return JobStatus.FAILED.value
        # 仍在推进
        if current == JobStatus.QUEUED.value and any(s["attempt_count"] > 0 for s in steps):
            conn.execute(
                "UPDATE jobs SET status=?, started_at=COALESCE(started_at, ?), updated_at=?"
                " WHERE id=?",
                (JobStatus.RUNNING.value, now, now, job_id),
            )
            return JobStatus.RUNNING.value
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now, job_id))
        return current

    def aggregate_job(self, job_id: str) -> str:
        with self._conn() as conn:
            with self._tx(conn):
                now = self.now()
                return self._aggregate_job(conn, job_id, now)

    # ---- 人工 retry ------------------------------------------------------

    def verify_job_success_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        """完整校验该任务所有 SUCCEEDED 步骤的产物。返回问题列表。"""
        conn = self._conn()
        try:
            problems: list[dict[str, Any]] = []
            steps = conn.execute(
                "SELECT * FROM steps WHERE job_id=? AND status=?", (job_id, StepStatus.SUCCEEDED.value)
            ).fetchall()
            for s in steps:
                arts = conn.execute("SELECT * FROM artifacts WHERE step_id=?", (s["id"],)).fetchall()
                if not arts:
                    problems.append({"step": s["name"], "problem": "SUCCEEDED 但没有产物记录"})
                    continue
                for a in arts:
                    pr = art.verify_published_artifact(
                        self.artifacts_root, a["relative_path"], a["sha256"], a["size_bytes"],
                        _artifact_is_dir(a["manifest_json"]), json.loads(a["manifest_json"]),
                    )
                    if pr:
                        problems.append({"step": s["name"], "artifact_id": a["id"],
                                        "relative_path": a["relative_path"], "problem": "; ".join(pr)})
            return problems
        finally:
            conn.close()

    def manual_retry(self, job_id: str, clear_fault: bool = False) -> dict[str, Any]:
        """FAILED → QUEUED：恢复 FAILED/BLOCKED 步骤，保留成功步骤与全部历史。

        预检：成功产物完整性 + 版本支持；不通过 → ArtifactInvalid（HTTP 409）。
        fatal 写入先提交、异常在事务外抛出，避免回滚掉 ARTIFACT_INVALID 记录。
        """
        invalid_message: str | None = None
        with self._conn() as conn:
            with self._tx(conn):
                self._inject("retry_begin")
                now = self.now()
                job = self._job_row(conn, job_id)
                if job is None:
                    raise LookupError(job_id)
                if job["status"] != JobStatus.FAILED.value:
                    raise InvalidJobState(
                        "INVALID_STATE", f"只有 FAILED 任务可以人工重试，当前 {job['status']}"
                    )
                # 版本预检（此时还没有任何写入，直接拒绝是安全的）
                if job["pipeline_version"] not in pipeline.SUPPORTED_PIPELINE_VERSIONS:
                    raise ArtifactInvalid(
                        ERR_PIPELINE_VERSION,
                        f"pipeline_version={job['pipeline_version']} 不受支持，无法重试",
                    )
                steps = self._steps_rows(conn, job_id)
                for s in steps:
                    if s["status"] == StepStatus.SUCCEEDED.value:
                        cur = pipeline.STEP_BY_NAME.get(s["name"])
                        if cur is None or cur.version != s["version"]:
                            raise ArtifactInvalid(
                                ERR_PIPELINE_VERSION,
                                f"步骤 {s['name']} 版本 {s['version']} 不受支持，无法重试",
                            )
                # 产物完整性预检（A11）：不通过记录 fatal 后在事务外返回 409
                problems = self.verify_job_success_artifacts(job_id)
                if problems:
                    detail = "; ".join(
                        f"{p['step']}: {p['problem']}" for p in problems[:3]
                    )
                    self._set_job_fatal(
                        conn, job_id, ERR_ARTIFACT_INVALID,
                        f"人工重试预检发现产物损坏: {detail}", now,
                        artifact_id=problems[0].get("artifact_id"),
                        step_name=problems[0].get("step"),
                    )
                    self._aggregate_job(conn, job_id, now)
                    invalid_message = f"成功产物缺失或摘要不符，拒绝继续：{detail}。请新建任务。"
                else:
                    # 恢复 FAILED/BLOCKED 步骤
                    restored: list[str] = []
                    for s in steps:
                        if s["status"] in (StepStatus.FAILED.value, StepStatus.BLOCKED.value):
                            conn.execute(
                                "UPDATE steps SET status=?, retry_round=retry_round+1,"
                                " attempts_in_round=0, next_run_at=?, error_code=NULL,"
                                " error_message=NULL, progress=NULL, progress_message=NULL,"
                                " updated_at=? WHERE id=?",
                                (StepStatus.PENDING.value, now, now, s["id"]),
                            )
                            restored.append(s["name"])
                    # 成功步骤写 OUTPUT_REUSED（每轮重试一次，不追加假 attempt）
                    for s in steps:
                        if s["status"] == StepStatus.SUCCEEDED.value:
                            arts = conn.execute(
                                "SELECT id, kind FROM artifacts WHERE step_id=?", (s["id"],)
                            ).fetchall()
                            self._event(conn, job_id, EventType.OUTPUT_REUSED,
                                        {"step": s["name"],
                                         "artifacts": [dict(a) for a in arts],
                                         "fingerprint": s["output_fingerprint"],
                                         "trigger": "manual_retry"},
                                        step_id=s["id"], now=now)
                    if clear_fault:
                        params, _ = _parse_input(job["input_json"])
                        new_input = json.dumps(
                            {"params": params, "demo_fault": {}},
                            ensure_ascii=True, sort_keys=True,
                        )
                        conn.execute("UPDATE jobs SET input_json=? WHERE id=?",
                                     (new_input, job_id))
                    conn.execute(
                        "UPDATE jobs SET status=?, fatal_error_code=NULL,"
                        " fatal_error_message=NULL, finished_at=NULL, updated_at=? WHERE id=?",
                        (JobStatus.QUEUED.value, now, job_id),
                    )
                    self._event(conn, job_id, EventType.JOB_RETRIED,
                                {"restored_steps": restored, "clear_fault": clear_fault,
                                 "round": max(s["retry_round"] for s in steps) + 1 if steps else 1},
                                now=now)
                    self._inject("retry_before_commit")
        if invalid_message is not None:
            # fatal 已提交；在事务外抛出 409 语义错误
            raise ArtifactInvalid(ERR_ARTIFACT_INVALID, invalid_message)
        return self.get_job_detail(job_id)

    # ---- 下载辅助（API 用）----------------------------------------------

    def get_artifact_record(self, artifact_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            r = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def verify_artifact_for_download(self, artifact_id: str) -> list[str]:
        """下载前完整校验（存在+尺寸+摘要）。返回问题列表。"""
        rec = self.get_artifact_record(artifact_id)
        if rec is None:
            return ["产物记录不存在"]
        return art.verify_published_artifact(
            self.artifacts_root, rec["relative_path"], rec["sha256"], rec["size_bytes"],
            _artifact_is_dir(rec["manifest_json"]), json.loads(rec["manifest_json"]),
        )

    def record_artifact_invalid(self, artifact_id: str, problems: list[str]) -> None:
        """下载发现损坏：记录事件（不抹掉历史成功）。"""
        now = self.now()
        with self._conn() as conn:
            with self._tx(conn):
                r = conn.execute(
                    "SELECT a.job_id, a.step_id, a.relative_path FROM artifacts a WHERE a.id=?",
                    (artifact_id,),
                ).fetchone()
                if r is None:
                    return
                self._event(conn, r["job_id"], EventType.ARTIFACT_INVALID,
                            {"artifact_id": artifact_id, "relative_path": r["relative_path"],
                             "trigger": "download", "problem": "; ".join(problems[:3])},
                            step_id=r["step_id"], now=now)

    # ---- 便捷查询（测试与脚本）-------------------------------------------

    def step_row(self, job_id: str, step_name: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            r = conn.execute(
                "SELECT * FROM steps WHERE job_id=? AND name=?", (job_id, step_name)
            ).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def attempt_rows(self, step_id: str) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE step_id=? ORDER BY attempt_no", (step_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def slot_holders(self) -> dict[str, str | None]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT id, holder_attempt_id FROM resource_slots").fetchall()
            return {r["id"]: r["holder_attempt_id"] for r in rows}
        finally:
            conn.close()

    def release_resource_for_test(self, attempt_id: str, owner: str) -> None:
        """释放资源：走与 complete/fail 相同的身份/租约守卫（事务内取时）。"""
        with self._conn() as conn:
            with self._tx(conn):
                now = self.now()
                self._load_active_attempt(conn, attempt_id, owner, now)
                self._release_slot(conn, attempt_id, now)

    def raw_release_slot_for_test(self, attempt_id: str) -> None:
        """绕过身份守卫直接按 holder 条件释放：验证 holder 条件更新本身
        也不能误释放新 attempt 的资源（A05）。"""
        now = self.now()
        with self._conn() as conn:
            with self._tx(conn):
                self._release_slot(conn, attempt_id, now)


def _parse_input(input_json: str) -> tuple[dict[str, Any], dict[str, Any]]:
    obj = json.loads(input_json)
    return obj.get("params", {}), obj.get("demo_fault", {})


def _artifact_is_dir(manifest_json: str) -> bool:
    try:
        obj = json.loads(manifest_json)
        return bool(obj.get("is_dir", False))
    except (ValueError, TypeError):
        return False


def bootstrap(db_path: Path | str, artifacts_root: Path | str, cpu_slots: int = 2,
              gpu_slots: int = 1, lease_seconds: float = 10.0,
              busy_timeout_ms: int = 5000, step_timeout_override: float = 0.0) -> Repository:
    """幂等初始化数据库与产物目录，返回仓储实例。"""
    init_db(db_path, cpu_slots=cpu_slots, gpu_slots=gpu_slots, busy_timeout_ms=busy_timeout_ms)
    Path(artifacts_root).mkdir(parents=True, exist_ok=True)
    return Repository(db_path, artifacts_root, lease_seconds=lease_seconds,
                      busy_timeout_ms=busy_timeout_ms,
                      step_timeout_override=step_timeout_override)
