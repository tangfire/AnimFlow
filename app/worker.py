"""独立 Worker 监督进程：`python -m app.worker`。

监督循环每次执行有界工作，绝不阻塞等待步骤结束，顺序（plan.md 第 5 节）：
1. 处理已结束子进程的结果（身份/租约/产物校验后提交）
2. 续租 + 从 progress.json 限频持久化进展
3. 检查步骤墙钟期限，超时结束子进程并记录 TIMED_OUT
4. 事务内回收过期租约（含其他 Worker 的）
5. 公平扫描领取新步骤（资源忙则跳过）
6. 事务提交后启动子进程；启动失败走统一失败路径

不持有数据库事务等待计算；不跨进程传 sqlite 连接。
硬杀监督进程后，租约过期由下一个 Worker 回收 —— at-least-once，
不是 exactly-once。
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import signal
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import pipeline
from app.artifacts import OutputBundle, read_json
from app.config import Config, ensure_dirs, load_config
from app.db import init_db
from app.domain import (
    ERR_ABNORMAL_EXIT,
    ERR_ARTIFACT_MISMATCH,
    ERR_RESULT_MISSING,
    ERR_SPAWN_FAILED,
    IdentityRejected,
)
from app.executors import run_step_subprocess
from app.repository import ClaimedStep, Repository

log = logging.getLogger("animflow.worker")

RESULT_MAX_BYTES = 2 * 1024 * 1024


def setup_logging(logs_dir: Path, worker_id: str) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [worker=%(name)s] %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
        fh = logging.FileHandler(logs_dir / f"worker-{worker_id}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


@dataclass
class RunningAttempt:
    claim: ClaimedStep
    proc: Any
    spawned_at: float
    last_heartbeat: float = 0.0
    last_progress_push: float = 0.0


class Supervisor:
    def __init__(self, cfg: Config, worker_id: str):
        self.cfg = cfg
        self.worker_id = worker_id
        self.repo = Repository(
            cfg.db_path, cfg.artifacts_dir,
            lease_seconds=cfg.lease_seconds, busy_timeout_ms=cfg.busy_timeout_ms,
        )
        self.running: dict[str, RunningAttempt] = {}
        self.stop = False
        self.mp_ctx = mp.get_context("spawn")

    # ---- 信号 ------------------------------------------------------------

    def request_stop(self, signum: int | None = None, frame: Any = None) -> None:
        log.info("收到停止信号 %s，进入收尾", signum)
        self.stop = True

    # ---- 主循环 ----------------------------------------------------------

    def run(self) -> None:
        log.info(
            "Worker 启动 id=%s db=%s artifacts=%s lease=%.1fs heartbeat=%.1fs poll=%.2fs",
            self.worker_id, self.cfg.db_path, self.cfg.artifacts_dir,
            self.cfg.lease_seconds, self.cfg.heartbeat_interval_s, self.cfg.poll_interval_s,
        )
        while not self.stop:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — 监督循环不能因单次异常退出
                log.exception("监督循环异常，继续下一轮")
            # 有界睡眠；停止时尽快醒
            deadline = time.monotonic() + self.cfg.poll_interval_s
            while not self.stop and time.monotonic() < deadline:
                time.sleep(0.05)
        self.shutdown()

    def tick(self) -> None:
        now = time.time()
        # 1) 已结束子进程
        self.reap_finished(now)
        # 2) 续租与进展
        self.heartbeat_running(now)
        # 3) 墙钟超时
        self.check_timeouts(now)
        # 4) 回收过期租约（含其他 Worker）
        recovered = self.repo.recover_expired()
        for r in recovered:
            log.info(
                "回收过期租约 attempt=%s job=%s step=%s retry_scheduled=%s next_run_at=%s",
                r.attempt_id, r.job_id[:8], r.step_name, r.retry_scheduled, r.next_run_at,
            )
        # 5) 领取 + 6) 启动
        self.claim_and_spawn()

    # ---- 结果处理 --------------------------------------------------------

    def reap_finished(self, now: float) -> None:
        for attempt_id, ra in list(self.running.items()):
            if ra.proc.is_alive():
                continue
            exitcode = ra.proc.exitcode
            del self.running[attempt_id]
            self._handle_child_exit(ra, exitcode, now)

    def _handle_child_exit(self, ra: RunningAttempt, exitcode: int, now: float) -> None:
        claim = ra.claim
        result_path = Path(claim.work_dir) / "result.json"
        result: dict[str, Any] | None = None
        try:
            if result_path.is_file() and result_path.stat().st_size <= RESULT_MAX_BYTES:
                result = read_json(result_path)
        except (ValueError, OSError):
            result = None
        if result is not None and result.get("attempt_id") == claim.attempt_id:
            if result.get("status") == "succeeded":
                try:
                    bundles = [OutputBundle.from_json(b) for b in result["bundles"]]
                    fp = self._fingerprint(claim)
                    outcome = self.repo.complete_attempt(
                        claim.attempt_id, claim.owner, bundles, fp,
                    )
                    log.info(
                        "发布成功 attempt=%s step=%s job=%s job_status=%s",
                        claim.attempt_id, claim.step_name, claim.job_id[:8],
                        outcome["job_status"],
                    )
                except IdentityRejected as exc:
                    log.warning(
                        "旧/无效 attempt 的成功结果被拒绝 attempt=%s step=%s reason=%s: %s",
                        claim.attempt_id, claim.step_name, exc.code, exc.message,
                    )
                except Exception as exc:  # noqa: BLE001 — 产物校验失败等
                    log.warning("产物发布失败 attempt=%s: %s", claim.attempt_id, exc)
                    self._safe_fail(claim, ERR_ARTIFACT_MISMATCH, str(exc), retryable=True)
                return
            err = result.get("error") or {}
            self._safe_fail(
                claim,
                err.get("code", "UNKNOWN"),
                err.get("message", "步骤报告失败"),
                retryable=bool(err.get("retryable", True)),
            )
            return
        # 无结构化结果 → 执行失败
        if exitcode == 0:
            self._safe_fail(claim, ERR_RESULT_MISSING,
                            "子进程退出码 0 但缺少 result.json", retryable=True)
        else:
            self._safe_fail(
                claim, ERR_ABNORMAL_EXIT,
                f"子进程异常退出 exitcode={exitcode}，无有效 result.json",
                retryable=True,
            )

    def _fingerprint(self, claim: ClaimedStep) -> str:
        deps = pipeline.STEP_BY_NAME[claim.step_name].depends_on
        upstream: dict[str, str] = {}
        for dep in deps:
            row = self.repo.step_row(claim.job_id, dep)
            if row is None or not row.get("output_fingerprint"):
                raise RuntimeError(f"上游 {dep} 无输出指纹，不可计算 {claim.step_name} 指纹")
            upstream[dep] = row["output_fingerprint"]
        return pipeline.compute_fingerprint(
            claim.step_name, claim.step_version, claim.input_params, upstream,
        )

    def _safe_fail(self, claim: ClaimedStep, code: str, message: str,
                   retryable: bool | None = None) -> None:
        try:
            outcome = self.repo.fail_attempt(
                claim.attempt_id, claim.owner, code, message, retryable=retryable,
            )
            log.info(
                "步骤失败 attempt=%s step=%s code=%s retry_scheduled=%s job_status=%s",
                claim.attempt_id, claim.step_name, code,
                outcome["retry_scheduled"], outcome["job_status"],
            )
        except IdentityRejected as exc:
            log.warning(
                "失败提交被拒（身份/租约）attempt=%s reason=%s: %s",
                claim.attempt_id, exc.code, exc.message,
            )

    # ---- 心跳与进展 ------------------------------------------------------

    def heartbeat_running(self, now: float) -> None:
        for attempt_id, ra in list(self.running.items()):
            if now - ra.last_heartbeat < self.cfg.heartbeat_interval_s:
                continue
            progress = None
            message = None
            pfile = Path(ra.claim.work_dir) / "progress.json"
            try:
                if pfile.is_file() and pfile.stat().st_size <= 64 * 1024:
                    obj = read_json(pfile)
                    if obj.get("attempt_id") == attempt_id:
                        progress = float(obj.get("ratio", 0.0))
                        message = str(obj.get("message", ""))[:200]
            except (ValueError, OSError, TypeError):
                pass
            try:
                self.repo.heartbeat(attempt_id, ra.claim.owner, progress, message)
                ra.last_heartbeat = now
            except IdentityRejected as exc:
                # 租约已失效/被新 attempt 取代：停止续租，结果提交时也会被拒绝
                log.warning("心跳被拒 attempt=%s reason=%s: %s", attempt_id, exc.code, exc.message)
                ra.last_heartbeat = now  # 不再每轮重试打爆日志；提交路径仍会校验

    # ---- 超时 ------------------------------------------------------------

    def check_timeouts(self, now: float) -> None:
        for attempt_id, ra in list(self.running.items()):
            claim = ra.claim
            if now < claim.deadline_at:
                continue
            log.warning(
                "步骤墙钟超时 attempt=%s step=%s deadline=%s，结束子进程",
                attempt_id, claim.step_name, claim.deadline_at,
            )
            self._terminate_proc(ra.proc)
            del self.running[attempt_id]
            try:
                outcome = self.repo.fail_attempt_timed_out(
                    attempt_id, claim.owner,
                    f"超过步骤执行期限 {claim.timeout_seconds}s（监督进程存活但业务无进展）",
                )
                log.info(
                    "记录 TIMED_OUT attempt=%s retry_scheduled=%s job_status=%s",
                    attempt_id, outcome["retry_scheduled"], outcome["job_status"],
                )
            except IdentityRejected as exc:
                log.warning("超时提交被拒 attempt=%s reason=%s", attempt_id, exc.code)

    @staticmethod
    def _terminate_proc(proc: Any) -> None:
        try:
            proc.terminate()
            proc.join(timeout=1.0)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=1.0)
        except Exception:  # noqa: BLE001
            log.exception("结束子进程失败 pid=%s", getattr(proc, "pid", None))

    # ---- 领取与启动 ------------------------------------------------------

    def claim_and_spawn(self) -> None:
        total_slots = self.cfg.cpu_slots + self.cfg.gpu_slots
        while len(self.running) < total_slots and not self.stop:
            claim = self.repo.claim_next_step(self.worker_id)
            if claim is None:
                return
            log.info(
                "领取步骤 job=%s step=%s attempt_no=%s attempt=%s resource=%s",
                claim.job_id[:8], claim.step_name, claim.attempt_no,
                claim.attempt_id, claim.resource,
            )
            self._spawn(claim)

    def _spawn(self, claim: ClaimedStep) -> None:
        fault = claim.demo_fault or {}
        if fault.get("fail_launch", {}).get(claim.step_name):
            # 演示预设：模拟“claim 后子进程启动失败”，走统一失败路径（消耗预算）
            log.warning("演示注入：模拟子进程启动失败 step=%s", claim.step_name)
            self._safe_fail(claim, ERR_SPAWN_FAILED,
                            "演示注入：模拟 multiprocessing 启动失败", retryable=True)
            return
        payload = {
            "attempt_id": claim.attempt_id,
            "step_name": claim.step_name,
            "step_version": claim.step_version,
            "job_id": claim.job_id,
            "work_dir": claim.work_dir,
            "input_params": claim.input_params,
            "demo_fault": claim.demo_fault,
            "upstream": claim.upstream,
            "attempt_no": claim.attempt_no,
            "retry_round": claim.retry_round,
            "pipeline_version": claim.pipeline_version,
            "parent_pid": os.getpid(),
            "step_speed": self.cfg.step_speed,
        }
        try:
            proc = self.mp_ctx.Process(
                target=run_step_subprocess, args=(payload,), name=f"step-{claim.step_name}",
            )
            proc.start()
        except Exception as exc:  # noqa: BLE001 — claim 后启动失败 → 统一失败路径
            log.exception("子进程启动失败 step=%s", claim.step_name)
            self._safe_fail(claim, ERR_SPAWN_FAILED, f"启动失败: {exc}", retryable=True)
            return
        self.running[claim.attempt_id] = RunningAttempt(
            claim=claim, proc=proc, spawned_at=time.time(),
        )
        log.info("启动子进程 attempt=%s pid=%s", claim.attempt_id, proc.pid)

    # ---- 收尾 ------------------------------------------------------------

    def shutdown(self) -> None:
        # 优雅停止：结束自己启动的子进程；未提交的 attempt 留给租约回收
        # （下一个 Worker 会记 WORKER_LOST 并重试 —— 这就是恢复路径本身）。
        for attempt_id, ra in list(self.running.items()):
            log.info("停止中，结束子进程 attempt=%s pid=%s",
                     attempt_id, getattr(ra.proc, "pid", None))
            self._terminate_proc(ra.proc)
        self.running.clear()
        log.info("Worker 停止 id=%s", self.worker_id)


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def main() -> int:
    cfg = load_config()
    ensure_dirs(cfg)
    init_db(cfg.db_path, cpu_slots=cfg.cpu_slots, gpu_slots=cfg.gpu_slots,
            busy_timeout_ms=cfg.busy_timeout_ms)
    worker_id = make_worker_id()
    setup_logging(cfg.logs_dir, worker_id)
    sup = Supervisor(cfg, worker_id)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, sup.request_stop)
        except (ValueError, OSError):  # pragma: no cover
            pass
    try:
        sup.run()
    except KeyboardInterrupt:  # pragma: no cover
        sup.request_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
