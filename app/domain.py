"""领域枚举、错误码与错误类型。

状态取值与 plan.md 第 6 节一一对应；错误码是 API、事件与 UI 共用的稳定标识。
"""
from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class AttemptStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    WORKER_LOST = "WORKER_LOST"


class ResourceKind(str, Enum):
    CPU = "cpu"
    GPU = "gpu"


class EventType(str, Enum):
    JOB_CREATED = "JOB_CREATED"
    JOB_SUCCEEDED = "JOB_SUCCEEDED"
    JOB_FAILED = "JOB_FAILED"
    JOB_RETRIED = "JOB_RETRIED"
    JOB_FATAL_ERROR = "JOB_FATAL_ERROR"
    STEP_CLAIMED = "STEP_CLAIMED"
    STEP_RETRY_SCHEDULED = "STEP_RETRY_SCHEDULED"
    STEP_SUCCEEDED = "STEP_SUCCEEDED"
    STEP_FAILED = "STEP_FAILED"
    STEP_BLOCKED = "STEP_BLOCKED"
    ATTEMPT_TIMED_OUT = "ATTEMPT_TIMED_OUT"
    ATTEMPT_WORKER_LOST = "ATTEMPT_WORKER_LOST"
    OUTPUT_REUSED = "OUTPUT_REUSED"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"


# ---- 错误码 -------------------------------------------------------------

# 可重试（进入 RETRY_WAIT 或租约恢复）
ERR_TRANSIENT_FAULT = "MOCK_TRANSIENT_FAULT"       # 演示注入的瞬时失败
ERR_SPAWN_FAILED = "SPAWN_FAILED"                  # 子进程启动失败
ERR_ABNORMAL_EXIT = "ABNORMAL_EXIT"                # 子进程异常退出码
ERR_TIMED_OUT = "TIMED_OUT"                        # 步骤墙钟超时
ERR_WORKER_LOST = "WORKER_LOST"                    # 租约过期，监督进程失联
ERR_RESULT_MISSING = "RESULT_MISSING"              # 子进程正常退出但缺结果文件
ERR_ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"        # 发布前产物校验失败

# 不可重试（直接步骤 FAILED）
ERR_NONRETRYABLE_FAULT = "NONRETRYABLE_FAULT"      # 演示注入的不可重试错误
ERR_BAD_OUTPUT = "BAD_OUTPUT"                      # 步骤产出结构非法
ERR_STEP_EXCEPTION = "STEP_EXCEPTION"              # 步骤代码未预期异常（仍按可重试处理，见 worker）

# 任务级 fatal（无自然归属步骤）
ERR_ARTIFACT_INVALID = "ARTIFACT_INVALID"
ERR_PIPELINE_VERSION = "PIPELINE_VERSION_UNSUPPORTED"

RETRYABLE_CODES = {
    ERR_TRANSIENT_FAULT,
    ERR_SPAWN_FAILED,
    ERR_ABNORMAL_EXIT,
    ERR_TIMED_OUT,
    ERR_WORKER_LOST,
    ERR_RESULT_MISSING,
    ERR_ARTIFACT_MISMATCH,
    ERR_STEP_EXCEPTION,
}


class DomainError(Exception):
    """业务错误基类。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class InvalidJobState(DomainError):
    """任务状态不允许该操作（如对 RUNNING 任务人工 retry）。"""


class ArtifactInvalid(DomainError):
    """成功产物缺失或摘要不符。"""


class IdentityRejected(DomainError):
    """attempt 身份/租约校验失败，写入被拒绝。"""

    def __init__(self, code: str, message: str):
        super().__init__(code, message)


# IdentityRejected 的具体原因码
REJ_OWNER = "REJECTED_OWNER_MISMATCH"
REJ_STATUS = "REJECTED_ATTEMPT_NOT_RUNNING"
REJ_LEASE = "REJECTED_LEASE_EXPIRED"
REJ_NOT_ACTIVE = "REJECTED_NOT_ACTIVE_ATTEMPT"
REJ_SLOT = "REJECTED_SLOT_NOT_HELD"


def is_retryable(code: str) -> bool:
    return code in RETRYABLE_CODES
