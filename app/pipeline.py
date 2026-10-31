"""固定 DAG 定义、输入参数校验、任务指纹。

步骤定义集中在此模块；启动/建库时做唯一名称、依赖存在、无环检查。
数据库为每个任务保存一份 DAG 快照；运行中的任务不随代码变化而静默变更，
Worker 遇到不支持的 pipeline_version 明确报告而不是误跑。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.domain import DomainError

PIPELINE_NAME = "demo_asset_pipeline"
PIPELINE_VERSION = "1.1.0"
# 演示“模型”版本：确定性 MOCK 计算的版本标识，进入业务指纹
DEMO_MODEL_VERSION = "motion-json-v1"

# Worker 声明支持的 pipeline 版本（与 jobs.pipeline_version 同构）；
# 快照不在此集合 → 任务级 fatal
SUPPORTED_PIPELINE_VERSIONS = {PIPELINE_VERSION}

RESOURCE_CPU = "cpu"
RESOURCE_GPU = "gpu"


@dataclass(frozen=True)
class StepDef:
    name: str
    version: str
    depends_on: tuple[str, ...]
    resource: str
    timeout_seconds: float
    max_attempts_per_round: int = 3
    description: str = ""


# 六步演示管线（见 plan.md 第 3 节）
STEPS: tuple[StepDef, ...] = (
    StepDef("inspect_asset", "1.0.0", (), RESOURCE_CPU, 60.0,
            description="素材检查：规范化参数 JSON"),
    StepDef("extract_frames", "1.0.0", ("inspect_asset",), RESOURCE_CPU, 60.0,
            description="按参数生成 PNG 帧与帧清单"),
    StepDef("plan_motion", "1.0.0", ("inspect_asset",), RESOURCE_CPU, 60.0,
            description="确定性动作/风格规划（模拟 AI）"),
    StepDef("stylize_frames", "1.0.0", ("extract_frames", "plan_motion"), RESOURCE_GPU, 60.0,
            description="逐帧确定性风格变换（模拟 GPU）"),
    StepDef("render_preview", "1.0.0", ("stylize_frames",), RESOURCE_CPU, 60.0,
            description="合成可播放的动画 GIF"),
    StepDef("package_artifact", "1.0.0", ("render_preview",), RESOURCE_CPU, 60.0,
            description="打包 GIF、配置与来源摘要 ZIP"),
)

STEP_BY_NAME: dict[str, StepDef] = {s.name: s for s in STEPS}
STEP_ORDER: dict[str, int] = {s.name: i for i, s in enumerate(STEPS)}


class PipelineError(DomainError):
    pass


def validate_dag(steps: tuple[StepDef, ...] = STEPS) -> None:
    """唯一名称、依赖存在、无环。非法则抛 PipelineError。"""
    names = [s.name for s in steps]
    if len(names) != len(set(names)):
        raise PipelineError("DAG_INVALID", f"步骤名不唯一: {names}")
    name_set = set(names)
    for s in steps:
        for dep in s.depends_on:
            if dep not in name_set:
                raise PipelineError("DAG_INVALID", f"步骤 {s.name} 依赖不存在的 {dep}")
            if dep == s.name:
                raise PipelineError("DAG_INVALID", f"步骤 {s.name} 依赖自身")
    # 拓扑排序检测环
    remaining = {s.name: set(s.depends_on) for s in steps}
    done: set[str] = set()
    while remaining:
        ready = [n for n, deps in remaining.items() if deps <= done]
        if not ready:
            raise PipelineError("DAG_INVALID", f"存在环: {remaining}")
        for n in ready:
            done.add(n)
            del remaining[n]


validate_dag()


def dag_snapshot() -> dict[str, Any]:
    """写入 jobs.dag_snapshot 的版本化快照。"""
    return {
        "pipeline_name": PIPELINE_NAME,
        "pipeline_version": PIPELINE_VERSION,
        "model_version": DEMO_MODEL_VERSION,
        "steps": [
            {
                "name": s.name,
                "version": s.version,
                "depends_on": list(s.depends_on),
                "resource": s.resource,
                "timeout_seconds": s.timeout_seconds,
                "max_attempts_per_round": s.max_attempts_per_round,
                "order_index": STEP_ORDER[s.name],
            }
            for s in STEPS
        ],
    }


def pipeline_key() -> str:
    return f"{PIPELINE_NAME}@{PIPELINE_VERSION}"


# ---- 输入参数 -----------------------------------------------------------

STYLE_CHOICES = ("neon", "watercolor", "comic", "pixel")

INPUT_BOUNDS = {
    "width": (16, 512),
    "height": (16, 512),
    "frame_count": (1, 48),
    "fps": (1, 50),
    "seed": (0, 2**31 - 1),
}

DEFAULT_INPUT: dict[str, Any] = {
    "width": 256,
    "height": 256,
    "frame_count": 12,
    "fps": 25,
    "style": "neon",
    "seed": 42,
    "planner_mode": "mock",
    "planner_model": "deepseek-flash",
    "brief": "霓虹色光球，沿圆形轨道缓慢移动",
}

BUSINESS_KEYS = ("width", "height", "frame_count", "fps", "style", "seed",
                 "planner_mode", "planner_model", "brief")


class InputInvalid(DomainError):
    pass


def normalize_input(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化业务参数；demo_fault 单独处理，不进业务指纹。"""
    if not isinstance(raw, dict):
        raise InputInvalid("INPUT_INVALID", "input 必须是对象")
    merged = dict(DEFAULT_INPUT)
    for key in BUSINESS_KEYS:
        if key in raw:
            merged[key] = raw[key]
    unknown = set(raw) - set(BUSINESS_KEYS) - {"demo_fault"}
    if unknown:
        raise InputInvalid("INPUT_INVALID", f"未知参数: {sorted(unknown)}")
    for key, (lo, hi) in INPUT_BOUNDS.items():
        v = merged[key]
        if not isinstance(v, int) or isinstance(v, bool):
            raise InputInvalid("INPUT_INVALID", f"{key} 必须是整数，实际 {v!r}")
        if not (lo <= v <= hi):
            raise InputInvalid("INPUT_INVALID", f"{key} 必须在 [{lo}, {hi}]，实际 {v}")
    if not isinstance(merged["style"], str) or merged["style"] not in STYLE_CHOICES:
        raise InputInvalid(
            "INPUT_INVALID", f"style 必须是 {list(STYLE_CHOICES)} 之一，实际 {merged['style']!r}"
        )
    if merged["planner_mode"] not in ("mock", "deepseek"):
        raise InputInvalid("INPUT_INVALID", "planner_mode 必须是 mock 或 deepseek")
    if merged["planner_model"] not in ("deepseek-flash", "deepseek-v4-pro"):
        raise InputInvalid("INPUT_INVALID", "planner_model 必须是 deepseek-flash 或 deepseek-v4-pro")
    if not isinstance(merged["brief"], str) or not (1 <= len(merged["brief"].strip()) <= 500):
        raise InputInvalid("INPUT_INVALID", "brief 必须为 1～500 字的动画描述")
    merged["brief"] = merged["brief"].strip()
    return merged


FAULT_TYPES = (
    "fail_first_n",      # {step: N} 第 N 次尝试前抛瞬时失败
    "always_fail",       # {step: true} 每次尝试都瞬时失败
    "nonretryable",      # {step: true} 不可重试错误
    "exit_early",        # {step: N} 第 N 次尝试子进程提前退出（写文件前）
    "timeout",           # {step: true} 子进程卡住直到墙钟超时
    "crash_after_files", # {step: N} 第 N 次写完产物后不发布结果（失效窗口）
    "slow",              # {step: seconds} 合法慢任务
    "fail_launch",       # {step: true} 监督进程模拟子进程启动失败
)


def normalize_fault(raw: Any) -> dict[str, Any]:
    """校验 demo_fault：只允许预设类型 + 已知步骤名，防止开放任意危险接口。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InputInvalid("INPUT_INVALID", "demo_fault 必须是对象")
    out: dict[str, Any] = {}
    for ftype, steps_map in raw.items():
        if ftype not in FAULT_TYPES:
            raise InputInvalid("INPUT_INVALID", f"未知故障类型: {ftype!r}，允许 {list(FAULT_TYPES)}")
        if not isinstance(steps_map, dict):
            raise InputInvalid("INPUT_INVALID", f"demo_fault.{ftype} 必须是对象 {{step: 参数}}")
        for step_name, value in steps_map.items():
            if step_name not in STEP_BY_NAME:
                raise InputInvalid("INPUT_INVALID", f"故障引用了未知步骤: {step_name!r}")
            if ftype in ("fail_first_n", "exit_early", "crash_after_files"):
                if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 10:
                    raise InputInvalid("INPUT_INVALID", f"demo_fault.{ftype}.{step_name} 必须是 1..10 的整数")
            elif ftype == "slow":
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 < value <= 60):
                    raise InputInvalid("INPUT_INVALID", f"demo_fault.slow.{step_name} 必须在 (0, 60] 秒")
            else:
                if value is not True:
                    raise InputInvalid("INPUT_INVALID", f"demo_fault.{ftype}.{step_name} 必须为 true")
            out.setdefault(ftype, {})[step_name] = value
    return out


def business_input_hash(params: dict[str, Any]) -> str:
    """业务输入哈希：只含业务参数，不含 demo_fault 等执行参数。"""
    canonical = json.dumps({k: params[k] for k in BUSINESS_KEYS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_fingerprint(
    step_name: str,
    step_version: str,
    params: dict[str, Any],
    upstream_fingerprints: dict[str, str],
) -> str:
    """输出指纹 = SHA256(规范化 JSON(...))。

    包含：步骤名与版本、pipeline 版本、业务参数、按依赖名排序的上游输出摘要、
    seed（含于业务参数）、演示模型版本。排除耗时/worker/故障注入/重试次数等执行参数。
    """
    payload = {
        "step_name": step_name,
        "step_version": step_version,
        "pipeline_name": PIPELINE_NAME,
        "pipeline_version": PIPELINE_VERSION,
        "model_version": DEMO_MODEL_VERSION,
        "params": {k: params[k] for k in BUSINESS_KEYS},
        "upstream": {k: upstream_fingerprints[k] for k in sorted(upstream_fingerprints)},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def retry_backoff_seconds(attempts_in_round: int) -> float:
    """min(2^(attempts_in_round-1), 30)；attempts_in_round 是刚失败的那次（>=1）。"""
    return float(min(2 ** max(attempts_in_round - 1, 0), 30))
