"""SQLite 初始化、连接与事务封装。

约定：
- isolation_level=None：由本模块显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK，
  避免 sqlite3 隐式事务与 BEGIN IMMEDIATE 相互冲突。
- WAL + 外键 + busy_timeout；每个进程使用自己的连接（每次操作新建连接，
  线程安全且避免嵌套事务问题）。
- 涉及竞争的写路径统一 BEGIN IMMEDIATE；锁忙由 busy_timeout + 有界重试兜底。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  input_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  pipeline_name TEXT NOT NULL,
  pipeline_version TEXT NOT NULL,
  dag_snapshot TEXT NOT NULL,
  fatal_error_code TEXT,
  fatal_error_message TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);

CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  order_index INTEGER NOT NULL,
  depends_on_json TEXT NOT NULL,
  status TEXT NOT NULL,
  resource TEXT NOT NULL,
  active_attempt_id TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  retry_round INTEGER NOT NULL DEFAULT 0,
  attempts_in_round INTEGER NOT NULL DEFAULT 0,
  max_attempts_per_round INTEGER NOT NULL,
  next_run_at REAL,
  timeout_seconds REAL NOT NULL,
  progress REAL,
  progress_message TEXT,
  progress_updated_at REAL,
  output_fingerprint TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(job_id, name)
);

CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL REFERENCES steps(id),
  job_id TEXT NOT NULL REFERENCES jobs(id),
  attempt_no INTEGER NOT NULL,
  retry_round INTEGER NOT NULL,
  owner TEXT NOT NULL,
  status TEXT NOT NULL,
  lease_expires_at REAL NOT NULL,
  heartbeat_at REAL NOT NULL,
  started_at REAL NOT NULL,
  deadline_at REAL NOT NULL,
  finished_at REAL,
  error_code TEXT,
  error_message TEXT,
  work_dir TEXT NOT NULL,
  UNIQUE(step_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL REFERENCES steps(id),
  attempt_id TEXT NOT NULL REFERENCES attempts(id),
  job_id TEXT NOT NULL REFERENCES jobs(id),
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  kind TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  step_id TEXT,
  attempt_id TEXT,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_slots (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  device_label TEXT NOT NULL,
  holder_attempt_id TEXT REFERENCES attempts(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_steps_job ON steps(job_id, order_index);
CREATE INDEX IF NOT EXISTS idx_steps_claimable ON steps(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_attempts_step ON attempts(step_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_attempts_lease ON attempts(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_step ON artifacts(step_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""


def connect(db_path: Path | str, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """每个进程/每次操作使用自己的连接。"""
    conn = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(
    db_path: Path | str,
    cpu_slots: int = 2,
    gpu_slots: int = 1,
    busy_timeout_ms: int = 5000,
) -> None:
    """幂等初始化：schema + 资源槽位。可重复执行。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path, busy_timeout_ms)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        # 槽位初始化幂等：只补齐缺失的，不删除已有（不把“清空 slots”当恢复）。
        for i in range(cpu_slots):
            conn.execute(
                "INSERT OR IGNORE INTO resource_slots(id, kind, device_label) VALUES(?, 'cpu', ?)",
                (f"cpu-{i}", f"mock-cpu-{i}"),
            )
        for i in range(gpu_slots):
            conn.execute(
                "INSERT OR IGNORE INTO resource_slots(id, kind, device_label) VALUES(?, 'gpu', ?)",
                (f"gpu-{i}", f"mock-gpu-{i}"),
            )
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection, retries: int = 25, retry_delay: float = 0.08) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE 写事务；锁忙时有界重试。事务内不做计算、不等待资源。"""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            last_exc = exc
            time.sleep(retry_delay * (1 + attempt * 0.2))
            continue
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute("COMMIT")
            return
    raise sqlite3.OperationalError(f"写事务在 {retries} 次重试后仍拿不到锁: {last_exc}")


def db_reachable(db_path: Path | str) -> bool:
    """healthz 用：数据库文件可打开且 schema 就绪。不等价于有 Worker。"""
    try:
        conn = connect(db_path, busy_timeout_ms=1000)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False
