"""FastAPI 应用：提交、状态、历史、资源、下载接口 + 同源静态看板。

不在请求里做计算；不在启动钩子里偷偷启动 Worker（API 与 Worker 独立）。
统一错误信封：{"error": {"code", "message"}}。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import pipeline
from app.artifacts import resolve_under_root, zip_directory_to
from app.config import Config, ensure_dirs, load_config
from app.db import db_reachable, init_db
from app.domain import ArtifactInvalid, DomainError, InvalidJobState
from app.repository import Repository

log = logging.getLogger("animflow.api")

STATIC_DIR = Path(__file__).parent / "static"


# ---- 输入模型（模块级定义，FastAPI 才能正确解析注解）---------------------


class JobCreateBody(BaseModel):
    # 兼容两种形态：{"params": {...}, "demo_fault": {...}} 或直接平铺业务参数
    params: dict[str, Any] | None = None
    demo_fault: dict[str, Any] | None = None
    model_config = {"extra": "allow"}


class RetryBody(BaseModel):
    clear_fault: bool = False


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    ensure_dirs(cfg)
    init_db(cfg.db_path, cpu_slots=cfg.cpu_slots, gpu_slots=cfg.gpu_slots,
            busy_timeout_ms=cfg.busy_timeout_ms)
    repo = Repository(cfg.db_path, cfg.artifacts_dir, lease_seconds=cfg.lease_seconds,
                      busy_timeout_ms=cfg.busy_timeout_ms)

    app = FastAPI(title="AnimFlow", version="0.1.0", docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.state.cfg = cfg
    app.state.repo = repo

    # ---- 任务 ------------------------------------------------------------

    @app.post("/api/jobs", status_code=202)
    def create_job(body: JobCreateBody) -> JSONResponse:
        # Pydantic v2 的 model_dump() 会把平铺 extra 拍平到顶层，不能用它读
        # extra；直接取 __pydantic_extra__，否则平铺参数被静默丢弃。
        extras = dict(body.__pydantic_extra__ or {})
        if body.params is not None:
            if extras:
                return _error(
                    422, "INPUT_INVALID",
                    f"params 嵌套格式不能与平铺参数混用，多余的键: {sorted(extras)}",
                )
            params_raw = dict(body.params)
            fault_raw = body.demo_fault
        else:
            fault_raw = body.demo_fault if body.demo_fault is not None else extras.pop(
                "demo_fault", None)
            params_raw = extras  # 平铺业务参数；未知键由 normalize_input 拒绝
        try:
            params = pipeline.normalize_input(params_raw)
            fault = pipeline.normalize_fault(fault_raw)
        except DomainError as exc:
            return _error(422, exc.code, exc.message)
        if fault and not cfg.faults_enabled:
            return _error(422, "FAULTS_DISABLED",
                          "本实例已关闭 demo_fault 故障注入（ANIMFLOW_FAULTS_ENABLED=0）")
        job = repo.create_job(params, fault)
        return JSONResponse(
            status_code=202,
            content={"id": job["id"], "status": job["status"],
                     "status_url": f"/api/jobs/{job['id']}"},
        )

    @app.get("/api/jobs")
    def list_jobs(limit: int = Query(20, ge=1, le=100),
                  offset: int = Query(0, ge=0)) -> dict[str, Any]:
        return repo.list_jobs(limit, offset)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return repo.get_job_detail(job_id)
        except LookupError:
            return _error(404, "NOT_FOUND", f"任务 {job_id} 不存在")

    @app.get("/api/jobs/{job_id}/attempts")
    def get_attempts(job_id: str, limit: int = Query(100, ge=1, le=500),
                     offset: int = Query(0, ge=0)) -> dict[str, Any]:
        try:
            return repo.get_attempts(job_id, limit, offset)
        except LookupError:
            return _error(404, "NOT_FOUND", f"任务 {job_id} 不存在")

    @app.get("/api/jobs/{job_id}/events")
    def get_events(job_id: str, after_id: int = Query(0, ge=0),
                   limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        """增量事件消费：以返回的 next_after_id 继续；空页也返回 next_after_id，
        按 id 升序、不重复不遗漏（id 为全局自增，跨任务过滤 job_id）。"""
        try:
            return repo.get_events(job_id, after_id, limit)
        except LookupError:
            return _error(404, "NOT_FOUND", f"任务 {job_id} 不存在")

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, body: RetryBody | None = None) -> dict[str, Any]:
        clear_fault = bool(body.clear_fault) if body else False
        try:
            return repo.manual_retry(job_id, clear_fault=clear_fault)
        except LookupError:
            return _error(404, "NOT_FOUND", f"任务 {job_id} 不存在")
        except InvalidJobState as exc:
            return _error(409, exc.code, exc.message)
        except ArtifactInvalid as exc:
            return _error(409, exc.code, exc.message, artifact_health="invalid")

    # ---- 资源 ------------------------------------------------------------

    @app.get("/api/planner")
    def planner_config() -> dict[str, Any]:
        import os
        return {"deepseek_configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
                "default_model": "deepseek-flash", "modes": ["mock", "deepseek"]}

    @app.get("/api/jobs/{job_id}/motion-plan")
    def motion_plan(job_id: str):
        try:
            detail = repo.get_job_detail(job_id)
        except LookupError:
            return _error(404, "NOT_FOUND", "任务不存在")
        step = next(s for s in detail["steps"] if s["name"] == "plan_motion")
        for artifact in step["artifacts"]:
            if artifact["kind"] != "motion_plan":
                continue
            rec = repo.get_artifact_record(artifact["id"])
            problems = repo.verify_artifact_for_download(artifact["id"])
            if problems:
                return _error(409, "ARTIFACT_INVALID", "动作规划产物损坏")
            path = resolve_under_root(cfg.artifacts_dir, rec["relative_path"])
            if path is None or path.stat().st_size > 64 * 1024:
                return _error(409, "ARTIFACT_INVALID", "动作规划产物超出限制")
            return {"ready": True, "artifact_id": artifact["id"],
                    "plan": json.loads(path.read_text(encoding="utf-8"))}
        return {"ready": False, "plan": None}

    @app.get("/api/resources")
    def resources() -> dict[str, Any]:
        return repo.list_resources()

    # ---- 下载 ------------------------------------------------------------

    @app.get("/api/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: str):
        rec = repo.get_artifact_record(artifact_id)
        if rec is None:
            return _error(404, "NOT_FOUND", f"产物 {artifact_id} 不存在")
        problems = repo.verify_artifact_for_download(artifact_id)
        if problems:
            repo.record_artifact_invalid(artifact_id, problems)
            return _error(409, "ARTIFACT_INVALID",
                          f"产物校验失败，拒绝下载：{'; '.join(problems[:3])}",
                          artifact_health="invalid")
        target = resolve_under_root(cfg.artifacts_dir, rec["relative_path"])
        assert target is not None  # verify 已通过，路径必在根内
        manifest = json.loads(rec["manifest_json"])
        filename = target.name if not manifest.get("is_dir") else f"{rec['kind']}.zip"
        if manifest.get("is_dir"):
            # 多文件产物：按需打包为 ZIP 流式返回
            import io
            buf = io.BytesIO()
            import zipfile
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for fe in manifest.get("files", []):
                    zf.write(target / fe["path"], fe["path"])
            buf.seek(0)
            return StreamingResponse(
                iter([buf.read()]), media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        return FileResponse(target, media_type="application/octet-stream",
                            filename=filename)

    # ---- 健康 ------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        db_ok = db_reachable(cfg.db_path)
        return JSONResponse(
            status_code=200 if db_ok else 503,
            content={"status": "ok" if db_ok else "db_unavailable",
                     "database": "ok" if db_ok else "unreachable",
                     "note": "数据库连通不等价于有 Worker 在运行；"
                             "Worker 状态见 /api/resources 的 holder 与 data/logs"},
        )

    # ---- 统一异常信封 ----------------------------------------------------

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        status = 409 if isinstance(exc, (InvalidJobState, ArtifactInvalid)) else 422
        return _error(status, exc.code, exc.message)

    # ---- 静态看板 --------------------------------------------------------

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
