"""DeepSeek motion planning. One HTTP call per attempt; no hidden client retries.

Only validated bounded drawing instructions are accepted, never executable code.
Credentials are read from the environment and never copied into task/attempt data.
"""
from __future__ import annotations

import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROMPT_VERSION = "motion-json-v1"
DEFAULT_MODEL = "deepseek-flash"
MAX_RESPONSE_BYTES = 128 * 1024


class MotionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=80)
    explanation: str = Field(min_length=1, max_length=400)
    motion: Literal["orbit", "pan_left", "pan_right", "bounce", "zoom_in"]
    intensity: Annotated[float, Field(ge=0.2, le=0.9)]
    speed: Annotated[float, Field(ge=0.5, le=2.0)]
    background_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    accent_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    secondary_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class PlannerError(Exception):
    def __init__(self, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def mock_plan(params: dict) -> dict:
    rng = random.Random(params["seed"] ^ 0xA11CE)
    palette = {
        "neon": ("#0c0e26", "#38e8ff", "#ed46ff"),
        "watercolor": ("#e8e2d4", "#72a5b5", "#d39095"),
        "comic": ("#231d35", "#ffcc38", "#e8546a"),
        "pixel": ("#132c38", "#71e6a0", "#ff9e53"),
    }[params["style"]]
    plan = MotionPlan(
        title="Seed 驱动的演示动画", explanation="确定性 MOCK 规划，没有模型调用。",
        motion=rng.choice(["orbit", "pan_left", "pan_right", "bounce", "zoom_in"]),
        intensity=0.65, speed=1.0, background_color=palette[0],
        accent_color=palette[1], secondary_color=palette[2],
    ).model_dump()
    return {**plan, "provider": "mock", "model": "mock-deterministic-v2",
            "prompt_version": PROMPT_VERSION, "latency_ms": 0, "usage": None,
            "request_id": None}


def deepseek_plan(params: dict) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise PlannerError("LLM_CONFIG", "Worker 未配置 DEEPSEEK_API_KEY；真实模式不会降级 MOCK。", False)
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    # Prevent accidentally sending the key to arbitrary URLs from an untrusted setting.
    if base not in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
        raise PlannerError("LLM_CONFIG", "仅支持 DeepSeek 官方 HTTPS API 地址。", False)
    example = mock_plan(params)
    example = {k: example[k] for k in MotionPlan.model_fields}
    prompt = (
        "You are an animation art director. Return ONLY one JSON object. "
        "Interpret the user's brief into visible color and motion choices for an abstract orb animation. "
        "Do not promise to draw characters, text, landscapes or video: the renderer only supports an orb, "
        "rings, color palettes and the five motion primitives. Use Chinese for title and explanation. "
        "Keys and value constraints follow this JSON Schema: "
        + json.dumps(MotionPlan.model_json_schema(), ensure_ascii=False)
        + " Example JSON: " + json.dumps(example, ensure_ascii=False)
    )
    payload = {
        "model": params.get("planner_model", DEFAULT_MODEL),
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": json.dumps({
                         "brief": params.get("brief", "霓虹色光球，沿圆形轨道缓慢移动"),
                         "style": params["style"], "seed": params["seed"],
                         "frame_count": params["frame_count"], "fps": params["fps"],
                     }, ensure_ascii=False)}],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"}, "max_tokens": 700, "stream": False,
    }
    request = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        retryable = status in (408, 429) or status >= 500
        code = "LLM_RATE_LIMIT" if status == 429 else "LLM_HTTP_ERROR"
        if status in (401, 403):
            code = "LLM_AUTH"
        if status == 402:
            code = "LLM_QUOTA"
        # Do not log raw provider bodies/headers: they may include secrets.
        raise PlannerError(code, f"DeepSeek HTTP {status}；请检查额度、权限或服务状态。", retryable) from None
    except (TimeoutError, socket.timeout):
        raise PlannerError("LLM_TIMEOUT", "DeepSeek 请求超过网络期限；响应不确定，重试可能重复计费。", True) from None
    except (urllib.error.URLError, OSError):
        raise PlannerError("LLM_NETWORK", "DeepSeek 网络请求失败；重试可能重复计费。", True) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PlannerError("LLM_INVALID_OUTPUT", "DeepSeek 响应超出大小限制。", True)
    try:
        body = json.loads(raw)
        choice = body["choices"][0]
        content = choice["message"]["content"]
        if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
            raise ValueError("Incomplete content")
        plan = MotionPlan.model_validate_json(content).model_dump()
        usage_raw = body.get("usage") or {}
        usage = {name: usage_raw[name] for name in
                 ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens")
                 if isinstance(usage_raw.get(name), int) and not isinstance(usage_raw[name], bool)
                 and usage_raw[name] >= 0}
        request_id = body.get("id")
        resolved_model = body.get("model")
        if not isinstance(request_id, str) or len(request_id) > 200:
            request_id = None
        if not isinstance(resolved_model, str) or len(resolved_model) > 120:
            resolved_model = params.get("planner_model", DEFAULT_MODEL)
    except (ValueError, KeyError, IndexError, TypeError, ValidationError):
        raise PlannerError("LLM_INVALID_OUTPUT", "DeepSeek 返回空、截断或不符合动画规划 Schema 的 JSON。", True) from None
    return {**plan, "provider": "deepseek", "model": resolved_model,
            "requested_model": params.get("planner_model", DEFAULT_MODEL),
            "prompt_version": PROMPT_VERSION,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "usage": usage, "request_id": request_id}
