"""六步演示管线的计算实现（子进程内执行）。

真实产物：Pillow 生成 PNG 帧 → 确定性风格变换 → 真实 GIF → 真实 ZIP。
动作规划可选择 DeepSeek 或确定性 MOCK；图像处理使用 Pillow，GPU 为模拟槽位。

约定：
- 只读取不可变输入与成功上游的产物清单；只写本次 attempt 独享目录；
- 文件先写临时名再原子重命名，最后写 manifest；
- 结果经 progress.json / result.json 结构化传递（有限大小），
  不跨进程传 sqlite 连接、不做无界 IPC；
- 计算函数不直接改业务状态；发布由监督进程受租约保护地完成。

故障注入（demo_fault）只影响指定任务的指定步骤，均为受限预设，
不改业务指纹，不向正常请求暴露任意进程终止能力。
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from app.artifacts import (
    atomic_write_json,
    bundle_directory,
    bundle_single_file,
    read_json,
)
from app.domain import (
    ERR_ABNORMAL_EXIT,
    ERR_NONRETRYABLE_FAULT,
    ERR_RESULT_MISSING,
    ERR_STEP_EXCEPTION,
    ERR_TRANSIENT_FAULT,
)
from app.pipeline import compute_fingerprint
from app.planner import MotionPlan, PlannerError, deepseek_plan, mock_plan


class RetryableStepError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NonRetryableStepError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- 父进程存活检查（尽力而为，不是强隔离保证）--------------------------


def parent_alive(ppid: int | None) -> bool:
    if not ppid:
        return True
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, ppid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(ppid, 0)
            return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


# ---- 进度与结果 ---------------------------------------------------------


class StepContext:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.attempt_id: str = payload["attempt_id"]
        self.step_name: str = payload["step_name"]
        self.job_id: str = payload["job_id"]
        self.work_dir = Path(payload["work_dir"])
        self.params: dict[str, Any] = payload["input_params"]
        self.fault: dict[str, Any] = payload.get("demo_fault", {}) or {}
        self.upstream: dict[str, Any] = payload.get("upstream", {}) or {}
        self.attempt_no: int = payload["attempt_no"]
        self.parent_pid: int | None = payload.get("parent_pid")
        self.speed: float = float(payload.get("step_speed", 1.0))
        self._last_progress_write = 0.0

    # -- 故障注入（仅预设） --

    def fault_triggered(self, ftype: str) -> Any:
        return self.fault.get(ftype, {}).get(self.step_name)

    def maybe_inject_failure(self) -> None:
        n = self.fault_triggered("fail_first_n")
        if isinstance(n, int) and self.attempt_no <= n:
            raise RetryableStepError(
                ERR_TRANSIENT_FAULT,
                f"演示注入：{self.step_name} 第 {self.attempt_no}/{n} 次尝试前瞬时失败",
            )
        if self.fault_triggered("always_fail"):
            raise RetryableStepError(
                ERR_TRANSIENT_FAULT, f"演示注入：{self.step_name} 持续失败"
            )
        if self.fault_triggered("nonretryable"):
            raise NonRetryableStepError(
                ERR_NONRETRYABLE_FAULT, f"演示注入：{self.step_name} 不可重试错误"
            )

    def maybe_exit_early(self) -> None:
        n = self.fault_triggered("exit_early")
        if isinstance(n, int) and self.attempt_no == n:
            # 模拟子进程异常退出（写文件前）
            os._exit(7)

    def maybe_hang(self) -> None:
        if self.fault_triggered("timeout"):
            # 无限占住直到监督进程按墙钟 deadline 结束；期间检查父进程存活
            while parent_alive(self.parent_pid):
                time.sleep(0.2)

    def slow_extra(self) -> None:
        s = self.fault_triggered("slow")
        if isinstance(s, (int, float)):
            end = time.monotonic() + float(s)
            while time.monotonic() < end:
                if not parent_alive(self.parent_pid):
                    raise RetryableStepError("PARENT_GONE", "父进程退出，子进程主动停止")
                time.sleep(0.1)

    def progress(self, done: int, total: int, message: str = "") -> None:
        now = time.monotonic()
        if now - self._last_progress_write < 0.2 and done < total:
            return
        self._last_progress_write = now
        atomic_write_json(
            self.work_dir / "progress.json",
            {
                "attempt_id": self.attempt_id,
                "completed": done,
                "total": total,
                "ratio": (done / total) if total else 0.0,
                "message": message,
            },
        )

    def check_parent(self) -> None:
        if not parent_alive(self.parent_pid):
            raise RetryableStepError("PARENT_GONE", "父进程已退出，子进程尽快结束")


# ---- 确定性绘制 ---------------------------------------------------------


def _frame_image(params: dict[str, Any], frame_idx: int, seed: int):
    from PIL import Image, ImageDraw

    w, h = params["width"], params["height"]
    style = params["style"]
    rng = random.Random(seed * 100003 + frame_idx)
    img = Image.new("RGB", (w, h), (12, 14, 22))
    draw = ImageDraw.Draw(img)
    # 背景渐变条带（确定性）
    bands = 8
    for i in range(bands):
        t = i / bands
        phase = frame_idx / max(params["frame_count"], 1)
        r = int(30 + 80 * (0.5 + 0.5 * math.sin(2 * math.pi * (t + phase))))
        g = int(20 + 60 * (0.5 + 0.5 * math.sin(2 * math.pi * (t + phase * 0.7 + 0.3))))
        b = int(60 + 120 * (0.5 + 0.5 * math.sin(2 * math.pi * (t + phase * 1.3 + 0.6))))
        y0 = int(h * i / bands)
        y1 = int(h * (i + 1) / bands)
        draw.rectangle([0, y0, w, y1], fill=(r, g, b))
    # 移动的圆形角色（seed 决定初始位置与半径）
    cx0 = w * (0.3 + 0.4 * rng.random())
    cy0 = h * (0.3 + 0.4 * rng.random())
    radius = min(w, h) * (0.12 + 0.08 * rng.random())
    phase = 2 * math.pi * frame_idx / max(params["frame_count"], 1)
    cx = cx0 + w * 0.18 * math.sin(phase)
    cy = cy0 + h * 0.18 * math.cos(phase * 1.5)
    color = (
        int(180 + 75 * rng.random()),
        int(180 + 75 * rng.random()),
        int(180 + 75 * rng.random()),
    )
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius], fill=color, outline=(255, 255, 255)
    )
    # 星点
    for _ in range(6):
        x, y = rng.random() * w, rng.random() * h
        s = 1 + rng.random() * 2
        draw.ellipse([x - s, y - s, x + s, y + s], fill=(240, 240, 255))
    # 风格标记
    draw.text((6, 6), f"{style} #{frame_idx}", fill=(255, 255, 255))
    if style == "pixel":
        small = img.resize((max(w // 8, 4), max(h // 8, 4)), Image.Resampling.NEAREST)
        img = small.resize((w, h), Image.Resampling.NEAREST)
    return img


def _stylize_image(img, style: str, seed: int, frame_idx: int):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    rng = random.Random(seed * 7919 + frame_idx)
    if style == "neon":
        img = img.filter(ImageFilter.EDGE_ENHANCE_MORE)
        r, g, b = img.split()
        shift = int(20 + 20 * rng.random())
        r = r.point(lambda v: min(255, v + shift))
        b = b.point(lambda v: min(255, v + shift // 2))
        img = Image.merge("RGB", (r, g, b))
        img = ImageEnhance.Contrast(img).enhance(1.4)
    elif style == "watercolor":
        img = img.filter(ImageFilter.GaussianBlur(1.6))
        img = img.filter(ImageFilter.SMOOTH)
        img = ImageEnhance.Color(img).enhance(0.85)
        img = img.quantize(colors=64, method=Image.Quantize.MEDIANCUT).convert("RGB")
    elif style == "comic":
        img = img.filter(ImageFilter.FIND_EDGES)
        base = img.filter(ImageFilter.SMOOTH)
        img = ImageEnhance.Contrast(base).enhance(2.2)
        img = ImageOps.posterize(img, 4)
    elif style == "pixel":
        w, h = img.size
        img = img.resize((max(w // 4, 4), max(h // 4, 4)), Image.Resampling.NEAREST)
        img = img.resize((w, h), Image.Resampling.NEAREST)
        img = ImageEnhance.Color(img).enhance(1.3)
    else:  # pragma: no cover - 输入已校验
        raise NonRetryableStepError("BAD_OUTPUT", f"未知风格 {style}")
    return img


def apply_motion_plan(img, plan: dict, frame_idx: int, frame_count: int):
    """Draw bounded motion primitives from the plan; no model-generated code."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

    w, h = img.size
    def rgb(hex_color):
        return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    bg, accent, secondary = (rgb(plan[k]) for k in
                             ("background_color", "accent_color", "secondary_color"))
    canvas = Image.new("RGB", img.size, bg)
    texture = ImageOps.colorize(ImageOps.grayscale(img), bg, secondary)
    canvas = Image.blend(canvas, texture, 0.22)
    t = frame_idx / max(frame_count, 1)
    phase = 2 * math.pi * t * plan["speed"]
    travel = min(w, h) * 0.32 * plan["intensity"]
    cx, cy = w / 2, h / 2
    radius = min(w, h) * 0.13
    motion = plan["motion"]
    if motion == "orbit":
        cx += travel * math.cos(phase)
        cy += travel * math.sin(phase)
    elif motion in ("pan_left", "pan_right"):
        direction = -1 if motion == "pan_left" else 1
        cx += direction * travel * math.sin(phase)
    elif motion == "bounce":
        cy += travel * math.cos(phase)
        cx += travel * 0.4 * math.sin(phase)
    elif motion == "zoom_in":
        radius *= 1 + plan["intensity"] * (0.5 + 0.5 * math.sin(phase))
    glow = Image.new("RGB", img.size, (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((cx-radius*1.6,cy-radius*1.6,cx+radius*1.6,cy+radius*1.6),fill=accent)
    canvas = ImageChops.add(canvas, glow.filter(ImageFilter.GaussianBlur(max(1, radius*.6))))
    draw = ImageDraw.Draw(canvas)
    for ring in (1.8, 2.5):
        r = radius * ring
        draw.ellipse((cx-r,cy-r,cx+r,cy+r),outline=secondary,width=max(1,w//170))
    draw.ellipse((cx-radius,cy-radius,cx+radius,cy+radius),fill=accent)
    highlight = radius * 0.22
    draw.ellipse((cx-radius*.4-highlight,cy-radius*.4-highlight,
                  cx-radius*.4+highlight,cy-radius*.4+highlight),fill=(255,255,255))
    return canvas


# ---- 六个步骤 -----------------------------------------------------------


def _upstream_files(ctx: StepContext, dep: str, kind: str) -> list[Path]:
    """从已发布上游产物目录读取文件列表。"""
    entries = []
    for art_entry in ctx.upstream.get(dep, {}).get("artifacts", []):
        if art_entry["kind"] == kind:
            base = Path(art_entry["absolute_path"])
            manifest = art_entry["manifest"]
            if manifest.get("is_dir"):
                for fe in manifest["files"]:
                    entries.append(Path(base) / fe["path"])
            else:
                entries.append(base)
    return entries


def step_inspect_asset(ctx: StepContext) -> list:
    ctx.maybe_inject_failure()
    ctx.maybe_exit_early()
    ctx.maybe_hang()
    ctx.progress(0, 2, "规范化参数")
    p = ctx.params
    normalized = {
        "width": int(p["width"]), "height": int(p["height"]),
        "frame_count": int(p["frame_count"]), "fps": int(p["fps"]),
        "style": p["style"], "seed": int(p["seed"]),
        "duration_ms": int(1000 * p["frame_count"] / p["fps"]),
        "source": "demo_asset_v1（程序生成的合成素材配方）",
    }
    time.sleep(0.1 / ctx.speed)
    ctx.check_parent()
    ctx.progress(1, 2, "写参数")
    out = ctx.work_dir / "inspect.json"
    atomic_write_json(out, normalized)
    time.sleep(0.05 / ctx.speed)
    ctx.progress(2, 2, "完成")
    ctx.slow_extra()
    return [bundle_single_file("params", out, ctx.work_dir, {"type": "inspect"})]


def step_extract_frames(ctx: StepContext) -> list:
    ctx.maybe_inject_failure()
    ctx.maybe_exit_early()
    ctx.maybe_hang()
    p = ctx.params
    frames_dir = ctx.work_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    n = p["frame_count"]
    file_entries = []
    from PIL import Image

    for i in range(n):
        ctx.check_parent()
        img = _frame_image(p, i, p["seed"])
        fp = frames_dir / f"f{i:04d}.png"
        tmp = fp.with_name(fp.name + f".tmp-{os.getpid()}")
        img.save(tmp, format="PNG")
        os.replace(tmp, fp)
        import hashlib

        h = hashlib.sha256(fp.read_bytes()).hexdigest()
        file_entries.append({"name": fp.name, "sha256": h, "size": fp.stat().st_size})
        ctx.progress(i + 1, n, f"已生成 {i + 1}/{n} 帧")
        time.sleep(0.04 / ctx.speed)
    manifest = {"type": "frames", "count": n, "files": file_entries}
    atomic_write_json(ctx.work_dir / "frames_manifest.json", manifest)
    return [bundle_directory("frames", frames_dir, ctx.work_dir, manifest)]


def step_plan_motion(ctx: StepContext) -> list:
    ctx.maybe_inject_failure()
    ctx.maybe_exit_early()
    ctx.maybe_hang()
    p = ctx.params
    real = p.get("planner_mode", "mock") == "deepseek"
    ctx.progress(0, 2, "等待 DeepSeek 真实动作规划" if real else "确定性动作规划（MOCK）")
    try:
        plan = deepseek_plan(p) if real else mock_plan(p)
    except PlannerError as exc:
        err = RetryableStepError if exc.retryable else NonRetryableStepError
        raise err(exc.code, exc.message) from None
    time.sleep(0.1 / ctx.speed)
    ctx.progress(1, 2, "写规划")
    out = ctx.work_dir / "motion_plan.json"
    atomic_write_json(out, plan)
    time.sleep(0.05 / ctx.speed)
    ctx.progress(2, 2, "完成")
    ctx.slow_extra()
    return [bundle_single_file("motion_plan", out, ctx.work_dir, {"type": "motion_plan"})]


def step_stylize_frames(ctx: StepContext) -> list:
    ctx.maybe_inject_failure()
    ctx.maybe_exit_early()
    ctx.maybe_hang()
    p = ctx.params
    src_files = _upstream_files(ctx, "extract_frames", "frames")
    if not src_files:
        raise NonRetryableStepError("BAD_OUTPUT", "缺少上游 extract_frames 帧产物")
    plan_path = None
    for a in ctx.upstream.get("plan_motion", {}).get("artifacts", []):
        if a["kind"] == "motion_plan":
            plan_path = Path(a["absolute_path"])
    if not plan_path or not plan_path.is_file():
        raise NonRetryableStepError("BAD_OUTPUT", "缺少已发布的动作规划")
    plan = read_json(plan_path)
    try:
        MotionPlan.model_validate({k: plan[k] for k in MotionPlan.model_fields})
    except (ValueError, KeyError):
        raise NonRetryableStepError("BAD_OUTPUT", "动作规划不符合渲染契约") from None
    ctx.progress(0, len(src_files), "准备风格化（模拟 GPU）")
    styled_dir = ctx.work_dir / "styled"
    styled_dir.mkdir(parents=True, exist_ok=True)
    file_entries = []
    from PIL import Image

    for i, src in enumerate(src_files):
        ctx.check_parent()
        img = Image.open(src).convert("RGB")
        img = _stylize_image(img, p["style"], p["seed"], i)
        img = apply_motion_plan(img, plan, i, len(src_files))
        fp = styled_dir / f"s{i:04d}.png"
        tmp = fp.with_name(fp.name + f".tmp-{os.getpid()}")
        img.save(tmp, format="PNG")
        os.replace(tmp, fp)
        import hashlib

        file_entries.append(
            {"name": fp.name, "sha256": hashlib.sha256(fp.read_bytes()).hexdigest(),
             "size": fp.stat().st_size}
        )
        ctx.progress(i + 1, len(src_files), f"风格化 {i + 1}/{len(src_files)}")
        time.sleep(0.08 / ctx.speed)
    manifest = {
        "type": "styled_frames", "count": len(file_entries),
        "files": file_entries, "applied_plan": plan,
    }
    atomic_write_json(ctx.work_dir / "styled_manifest.json", manifest)
    ctx.slow_extra()
    return [bundle_directory("styled_frames", styled_dir, ctx.work_dir, manifest)]


def step_render_preview(ctx: StepContext) -> list:
    ctx.maybe_inject_failure()
    ctx.maybe_exit_early()
    ctx.maybe_hang()
    p = ctx.params
    src_files = _upstream_files(ctx, "stylize_frames", "styled_frames")
    if not src_files:
        raise NonRetryableStepError("BAD_OUTPUT", "缺少上游 stylize_frames 产物")
    ctx.progress(0, len(src_files), "合成 GIF")
    from PIL import Image

    frames = []
    for i, src in enumerate(src_files):
        ctx.check_parent()
        frames.append(Image.open(src).convert("RGB"))
        ctx.progress(i + 1, len(src_files) + 1, f"读取帧 {i + 1}")
        time.sleep(0.01 / ctx.speed)
    out = ctx.work_dir / "preview.gif"
    duration_ms = max(int(1000 / p["fps"]), 20)
    frames[0].save(
        out, save_all=True, append_images=frames[1:], duration=duration_ms, loop=0, format="GIF"
    )
    ctx.progress(len(src_files) + 1, len(src_files) + 1, "GIF 完成")
    ctx.slow_extra()
    return [bundle_single_file("preview_gif", out, ctx.work_dir,
                              {"type": "gif", "frame_count": len(frames), "duration_ms": duration_ms})]


def step_package_artifact(ctx: StepContext) -> list:
    """打包：只读取声明上游（render_preview）的产物与不可变输入参数。

    ZIP 内容：preview.gif + params.json + 来源摘要 + 说明；不塞诊断日志。
    """
    import zipfile

    ctx.maybe_inject_failure()
    ctx.maybe_exit_early()
    ctx.maybe_hang()
    ctx.progress(0, 3, "收集产物")
    p = ctx.params
    gif_path = None
    for a in ctx.upstream.get("render_preview", {}).get("artifacts", []):
        if a["kind"] == "preview_gif":
            gif_path = Path(a["absolute_path"])
    if gif_path is None or not gif_path.is_file():
        raise NonRetryableStepError("BAD_OUTPUT", "缺少上游 render_preview GIF")
    ctx.progress(1, 3, "写 ZIP")
    out = ctx.work_dir / "final_artifacts.zip"
    tmp = out.with_name(out.name + f".tmp-{os.getpid()}")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(gif_path, "preview.gif")
        zf.writestr("params.json", json.dumps(p, sort_keys=True, indent=1))
        zf.writestr(
            "source_summary.json",
            json.dumps(
                {
                    "job_id": ctx.job_id,
                    "pipeline": "demo_asset_pipeline@" + str(ctx.payload.get("pipeline_version", "unknown")),
                    "asset_source": "程序生成素材；动作规划模式=" + p.get("planner_mode", "mock") + "；GPU 为模拟槽位",
                    "steps": ["inspect_asset", "extract_frames", "plan_motion",
                              "stylize_frames", "render_preview", "package_artifact"],
                },
                sort_keys=True, indent=1,
            ),
        )
        zf.writestr(
            "README.txt",
            "AnimFlow：素材由程序生成，Pillow 渲染 GIF；动作规划模式="
            + p.get("planner_mode", "mock") + "；GPU 为模拟槽位。\n",
        )
    os.replace(tmp, out)
    ctx.progress(2, 3, "校验 ZIP")
    if not zipfile.is_zipfile(out):
        raise NonRetryableStepError("BAD_OUTPUT", "ZIP 生成失败")
    ctx.progress(3, 3, "完成")
    ctx.slow_extra()
    return [bundle_single_file("final_zip", out, ctx.work_dir, {"type": "zip"})]


STEP_EXECUTORS = {
    "inspect_asset": step_inspect_asset,
    "extract_frames": step_extract_frames,
    "plan_motion": step_plan_motion,
    "stylize_frames": step_stylize_frames,
    "render_preview": step_render_preview,
    "package_artifact": step_package_artifact,
}


# ---- 子进程入口 ---------------------------------------------------------


def run_step_subprocess(payload: dict[str, Any]) -> None:
    """步骤子进程入口（multiprocessing spawn 目标）。

    只写本 attempt 目录：progress.json / result.json；绝不触碰数据库。
    """
    work_dir = Path(payload["work_dir"])
    work_dir.mkdir(parents=True, exist_ok=True)
    attempt_id = payload["attempt_id"]
    step_name = payload["step_name"]
    ctx = StepContext(payload)
    try:
        executor = STEP_EXECUTORS.get(step_name)
        if executor is None:
            raise NonRetryableStepError("BAD_OUTPUT", f"未知步骤 {step_name}")
        bundles = executor(ctx)
        result = {
            "attempt_id": attempt_id,
            "step_name": step_name,
            "status": "succeeded",
            "bundles": [b.to_json() for b in bundles],
        }
        atomic_write_json(work_dir / "result.json", result)
        # crash_after_files：产物与结果都已写盘，但子进程不退出（模拟
        # “文件写完、成功事务提交前”的失效窗口；由测试/演示脚本硬杀监督进程）
        n = ctx.fault_triggered("crash_after_files")
        if isinstance(n, int) and ctx.attempt_no == n:
            atomic_write_json(work_dir / "barrier.flag", {"ready": True, "attempt_id": attempt_id})
            while parent_alive(ctx.parent_pid):
                time.sleep(0.2)
            os._exit(8)
    except NonRetryableStepError as exc:
        atomic_write_json(work_dir / "result.json", {
            "attempt_id": attempt_id, "step_name": step_name, "status": "failed",
            "error": {"code": exc.code, "message": exc.message, "retryable": False},
        })
    except RetryableStepError as exc:
        atomic_write_json(work_dir / "result.json", {
            "attempt_id": attempt_id, "step_name": step_name, "status": "failed",
            "error": {"code": exc.code, "message": exc.message, "retryable": True},
        })
    except Exception as exc:  # noqa: BLE001 — 子进程兜底：未预期异常记录后按可重试上抛
        atomic_write_json(work_dir / "result.json", {
            "attempt_id": attempt_id, "step_name": step_name, "status": "failed",
            "error": {"code": ERR_STEP_EXCEPTION,
                      "message": f"{type(exc).__name__}: {exc}",
                      "retryable": True,
                      "traceback": traceback.format_exc(limit=8)},
        })


def compute_fingerprint_for(
    step_name: str, step_version: str, params: dict[str, Any],
    upstream_fingerprints: dict[str, str],
) -> str:
    return compute_fingerprint(step_name, step_version, params, upstream_fingerprints)
