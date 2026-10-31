"""运行配置：路径、并发槽位与时间参数。

所有参数都可用 ANIMFLOW_* 环境变量覆盖，便于测试注入短租约/短轮询。
配置在加载时做边界校验（例如 lease 必须明显大于 heartbeat）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """配置非法。"""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，实际是 {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，实际是 {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    artifacts_dir: Path
    logs_dir: Path
    poll_interval_s: float = 0.5
    heartbeat_interval_s: float = 2.0
    lease_seconds: float = 10.0
    cpu_slots: int = 2
    gpu_slots: int = 1
    faults_enabled: bool = True
    step_speed: float = 1.0  # 演示计算的缩放系数，测试可调小
    step_timeout_s: float = 0.0  # >0 时覆盖所有步骤的墙钟期限（测试用）
    busy_timeout_ms: int = 5000
    max_events_page: int = 500
    max_jobs_page: int = 100

    def __post_init__(self) -> None:
        if self.poll_interval_s <= 0:
            raise ConfigError("poll_interval_s 必须 > 0")
        if self.heartbeat_interval_s <= 0:
            raise ConfigError("heartbeat_interval_s 必须 > 0")
        if self.lease_seconds < 3 * self.heartbeat_interval_s:
            raise ConfigError(
                f"lease_seconds({self.lease_seconds}) 必须 >= 3 * heartbeat_interval_s"
                f"({self.heartbeat_interval_s})，否则心跳稍慢租约就过期"
            )
        if self.cpu_slots < 1 or self.gpu_slots < 1:
            raise ConfigError("cpu_slots / gpu_slots 必须 >= 1")
        if self.step_speed <= 0:
            raise ConfigError("step_speed 必须 > 0")


def load_config() -> Config:
    # Local developer secret; never persisted in jobs or sent to the browser.
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    data_dir = Path(os.environ.get("ANIMFLOW_DATA_DIR", "data")).resolve()
    db_path = Path(os.environ.get("ANIMFLOW_DB_PATH", str(data_dir / "animflow.db"))).resolve()
    artifacts_dir = Path(
        os.environ.get("ANIMFLOW_ARTIFACTS_DIR", str(data_dir / "artifacts"))
    ).resolve()
    logs_dir = Path(os.environ.get("ANIMFLOW_LOGS_DIR", str(data_dir / "logs"))).resolve()
    cfg = Config(
        data_dir=data_dir,
        db_path=db_path,
        artifacts_dir=artifacts_dir,
        logs_dir=logs_dir,
        poll_interval_s=_env_float("ANIMFLOW_POLL_S", 0.5),
        heartbeat_interval_s=_env_float("ANIMFLOW_HEARTBEAT_S", 2.0),
        lease_seconds=_env_float("ANIMFLOW_LEASE_S", 10.0),
        cpu_slots=_env_int("ANIMFLOW_CPU_SLOTS", 2),
        gpu_slots=_env_int("ANIMFLOW_GPU_SLOTS", 1),
        faults_enabled=os.environ.get("ANIMFLOW_FAULTS_ENABLED", "1") not in ("0", "false", "False"),
        step_speed=_env_float("ANIMFLOW_STEP_SPEED", 1.0),
        step_timeout_s=_env_float("ANIMFLOW_STEP_TIMEOUT_S", 0.0),
        busy_timeout_ms=_env_int("ANIMFLOW_BUSY_TIMEOUT_MS", 5000),
    )
    return cfg


def ensure_dirs(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
