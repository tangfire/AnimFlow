"""pytest 公共设施：独立临时 db/artifacts、可注入时钟、真实子进程管理。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts import bundle_single_file  # noqa: E402
from app.db import init_db  # noqa: E402
from app.repository import Repository  # noqa: E402


class FakeClock:
    """可注入时间：精确控制租约过期与退避。"""

    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tmp_env(tmp_path: Path) -> dict:
    db = tmp_path / "test.db"
    arts = tmp_path / "artifacts"
    init_db(db, cpu_slots=2, gpu_slots=1)
    arts.mkdir(parents=True, exist_ok=True)
    return {"db": db, "artifacts": arts, "root": tmp_path}


@pytest.fixture
def repo(tmp_env) -> Repository:
    return Repository(tmp_env["db"], tmp_env["artifacts"], lease_seconds=10.0)


@pytest.fixture
def repo_clock(tmp_env, clock) -> Repository:
    return Repository(tmp_env["db"], tmp_env["artifacts"], lease_seconds=10.0, now_fn=clock)


def make_fake_bundle(repo: Repository, claim, name: str = "out.txt", content: bytes = b"ok"):
    """在 claim 的 work_dir 里写一个真实文件并构建可校验的 bundle。"""
    work = Path(claim.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    fp = work / name
    fp.write_bytes(content)
    b = bundle_single_file("params", fp, work, {"type": "test"})
    return [b]


def wait_until(pred: Callable[[], bool], timeout: float = 20.0, interval: float = 0.2,
               message: str = "条件超时") -> float:
    """有 deadline 的轮询；返回实际等待秒数。不靠固定长 sleep 猜结果。"""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if pred():
            return time.monotonic() - start
        time.sleep(interval)
    raise AssertionError(f"{message}（{timeout}s 内未满足）")


def terminate_process_tree(proc: subprocess.Popen) -> None:
    """只结束本函数创建的进程树；Windows 用 taskkill /T /F，POSIX 用 killpg。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def spawn_worker(db: Path, artifacts: Path, extra_env: dict | None = None) -> subprocess.Popen:
    """启动独立 Worker 子进程，记录 PID 供硬杀。"""
    env = os.environ.copy()
    env["ANIMFLOW_DATA_DIR"] = str(db.parent)
    env["ANIMFLOW_DB_PATH"] = str(db)
    env["ANIMFLOW_ARTIFACTS_DIR"] = str(artifacts)
    env["PYTHONPATH"] = str(ROOT)
    env.setdefault("ANIMFLOW_POLL_S", "0.2")
    env.setdefault("ANIMFLOW_HEARTBEAT_S", "0.5")
    env.setdefault("ANIMFLOW_LEASE_S", "2.0")
    env.setdefault("ANIMFLOW_STEP_SPEED", "10")
    if extra_env:
        env.update(extra_env)
    out = db.parent / f"worker-{os.getpid()}-{len(list(db.parent.glob('worker-*.out')))}.out"
    return subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=str(ROOT), env=env,
        stdout=open(out, "w", encoding="utf-8"), stderr=subprocess.STDOUT,
    )


@pytest.fixture
def worker_spawner(tmp_env):
    """返回 (spawn 函数, 已启动进程列表)；测试结束 finally 清理。"""
    started: list[subprocess.Popen] = []

    def _spawn(extra_env: dict | None = None) -> subprocess.Popen:
        proc = spawn_worker(tmp_env["db"], tmp_env["artifacts"], extra_env)
        started.append(proc)
        return proc

    try:
        yield _spawn, started
    finally:
        for proc in started:
            terminate_process_tree(proc)
