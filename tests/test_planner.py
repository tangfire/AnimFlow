"""Offline model contract tests. No paid API calls in pytest."""
import json
import socket
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app import pipeline, planner
from app.executors import (StepContext, NonRetryableStepError, RetryableStepError,
                           apply_motion_plan, step_plan_motion, _stylize_image)
from app.artifacts import read_json
from app.worker import Supervisor


class Response(BytesIO):
    pass


def reply(plan=None, content=None, finish="stop"):
    if content is None:
        raw = plan or planner.mock_plan(pipeline.DEFAULT_INPUT)
        content = json.dumps({k: raw[k] for k in planner.MotionPlan.model_fields})
    return Response(json.dumps({
        "id": "test-request", "model": "deepseek-flash",
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 60, "total_tokens": 160},
    }).encode())


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-never-log")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def test_live_contract_json_and_usage(credentials, monkeypatch):
    calls = []
    def call(req, timeout):
        calls.append(req)
        body = json.loads(req.data)
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        assert "test-secret-never-log" not in req.data.decode()
        assert timeout == 35
        return reply()
    monkeypatch.setattr(planner.urllib.request, "urlopen", call)
    result = planner.deepseek_plan(pipeline.DEFAULT_INPUT)
    assert len(calls) == 1
    assert result["provider"] == "deepseek"
    assert result["usage"]["total_tokens"] == 160
    assert result["request_id"] == "test-request"
    assert result["prompt_version"] == planner.PROMPT_VERSION
    assert "test-secret" not in json.dumps(result)


@pytest.mark.parametrize("status,retryable,code", [
    (401, False, "LLM_AUTH"), (403, False, "LLM_AUTH"),
    (402, False, "LLM_QUOTA"), (429, True, "LLM_RATE_LIMIT"),
    (503, True, "LLM_HTTP_ERROR"), (400, False, "LLM_HTTP_ERROR"),
])
def test_http_classification_no_hidden_retry(credentials, monkeypatch, status, retryable, code):
    count = []
    def call(*args, **kwargs):
        count.append(1)
        raise urllib.error.HTTPError("https://api.deepseek.com", status,
                                     "test-secret-never-log", {}, BytesIO(b"test-secret-never-log"))
    monkeypatch.setattr(planner.urllib.request, "urlopen", call)
    with pytest.raises(planner.PlannerError) as error:
        planner.deepseek_plan(pipeline.DEFAULT_INPUT)
    assert error.value.retryable is retryable
    assert error.value.code == code
    assert len(count) == 1
    assert "test-secret" not in str(error.value)


@pytest.mark.parametrize("response", [
    lambda: reply(content=" "), lambda: reply(content="not json"),
    lambda: reply(content='{"motion":"execute_python"}'),
    lambda: reply(finish="length"),
    lambda: reply(plan={**planner.mock_plan(pipeline.DEFAULT_INPUT), "accent_color": "red"}),
    lambda: Response(b"x" * (planner.MAX_RESPONSE_BYTES + 1)),
])
def test_invalid_output_rejected(credentials, monkeypatch, response):
    monkeypatch.setattr(planner.urllib.request, "urlopen", lambda *a, **kw: response())
    with pytest.raises(planner.PlannerError) as error:
        planner.deepseek_plan(pipeline.DEFAULT_INPUT)
    assert error.value.code == "LLM_INVALID_OUTPUT"
    assert error.value.retryable


def test_timeout_classified(credentials, monkeypatch):
    def call(*a, **kw):
        raise socket.timeout()
    monkeypatch.setattr(planner.urllib.request, "urlopen", call)
    with pytest.raises(planner.PlannerError) as error:
        planner.deepseek_plan(pipeline.DEFAULT_INPUT)
    assert error.value.code == "LLM_TIMEOUT"


def test_no_key_and_wrong_endpoint_fail_closed(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(planner.PlannerError) as error:
        planner.deepseek_plan(pipeline.DEFAULT_INPUT)
    assert not error.value.retryable
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.com")
    with pytest.raises(planner.PlannerError) as error:
        planner.deepseek_plan(pipeline.DEFAULT_INPUT)
    assert error.value.code == "LLM_CONFIG"


def context(tmp_path, mode):
    return StepContext({"attempt_id": "a", "step_name": "plan_motion", "job_id": "j",
                        "work_dir": str(tmp_path), "attempt_no": 1, "step_speed": 100,
                        "input_params": {**pipeline.DEFAULT_INPUT, "planner_mode": mode}})


def test_mock_never_calls_network(tmp_path, monkeypatch):
    def fail(*a, **kw):
        pytest.fail("MOCK must never call a provider")
    monkeypatch.setattr(planner.urllib.request, "urlopen", fail)
    bundles = step_plan_motion(context(tmp_path, "mock"))
    plan = read_json(tmp_path / "motion_plan.json")
    assert plan["provider"] == "mock" and plan["usage"] is None
    assert bundles[0].kind == "motion_plan"


def test_real_failure_not_silently_mocked(tmp_path, monkeypatch):
    def fail(params):
        raise planner.PlannerError("LLM_AUTH", "auth failed", False)
    monkeypatch.setattr("app.executors.deepseek_plan", fail)
    with pytest.raises(NonRetryableStepError) as error:
        step_plan_motion(context(tmp_path, "deepseek"))
    assert error.value.code == "LLM_AUTH"
    assert not (tmp_path / "motion_plan.json").exists()


def test_live_failure_uses_task_retry_budget(repo_clock, clock, monkeypatch):
    from conftest import make_fake_bundle
    repo=repo_clock
    job=repo.create_job({**pipeline.DEFAULT_INPUT,"planner_mode":"deepseek"})
    for expected in ("inspect_asset","extract_frames"):
        claim=repo.claim_next_step("w")
        assert claim.step_name==expected
        repo.complete_attempt(claim.attempt_id,"w",make_fake_bundle(repo,claim),"fp")
    calls=[]
    def fail(params):
        calls.append(1)
        raise planner.PlannerError("LLM_RATE_LIMIT", "rate limited", True)
    monkeypatch.setattr("app.executors.deepseek_plan", fail)
    for attempt in range(1,4):
        claim=repo.claim_next_step("w")
        assert claim.step_name=="plan_motion" and claim.attempt_no==attempt
        ctx=context(Path(claim.work_dir),"deepseek")
        with pytest.raises(RetryableStepError) as err:
            step_plan_motion(ctx)
        repo.fail_attempt(claim.attempt_id,"w",err.value.code,err.value.message,retryable=True)
        clock.advance(3)
    assert len(calls)==3
    assert repo.get_job(job["id"])["status"]=="FAILED"
    assert repo.claim_next_step("w") is None


def test_plan_changes_pixels_and_motion():
    image = Image.new("RGB", (128,128), (70,60,40))
    p = planner.mock_plan(pipeline.DEFAULT_INPUT)
    first = apply_motion_plan(image, {**p,"motion":"pan_right"}, 3, 12)
    direction = apply_motion_plan(image, {**p,"motion":"pan_left"}, 3, 12)
    palette = apply_motion_plan(image, {**p,"motion":"pan_right","accent_color":"#ff0000"}, 3, 12)
    assert first.tobytes() != direction.tobytes()
    assert first.tobytes() != palette.tobytes()
    assert first.tobytes() == apply_motion_plan(image,{**p,"motion":"pan_right"},3,12).tobytes()


@pytest.mark.parametrize("style", pipeline.STYLE_CHOICES)
def test_all_style_filters_work(style):
    result = _stylize_image(Image.new("RGB",(64,64)), style, 42, 0)
    assert result.size == (64,64)


@pytest.mark.parametrize("overrides", [
    {"brief":""}, {"brief":"x"*501}, {"planner_mode":"invalid"},
    {"planner_model":"arbitrary"}, {"style":{}},
])
def test_planner_input_bounds(overrides):
    with pytest.raises(pipeline.InputInvalid):
        pipeline.normalize_input(overrides)


def test_fingerprint_covers_mode_model_brief_and_actual_content():
    p = dict(pipeline.DEFAULT_INPUT)
    fp = pipeline.compute_fingerprint("plan_motion", "1.0.0",p,{})
    for change in ({"brief":"orange bouncing orb"},{"planner_mode":"deepseek"},
                   {"planner_model":"deepseek-v4-pro"}):
        assert fp != pipeline.compute_fingerprint("plan_motion","1.0.0",{**p,**change},{})
    class Repo:
        def step_row(self,*args):
            return {"output_fingerprint":"same-lineage"}
    class Sup:
        repo = Repo()
    class Claim:
        step_name="stylize_frames"
        step_version="1.0.0"
        job_id="j"
        input_params=p
        upstream={"extract_frames":{"artifacts":[{"sha256":"frames"}]},
                  "plan_motion":{"artifacts":[{"sha256":"response-one"}]}}
    claim=Claim()
    first=Supervisor._fingerprint(Sup(),claim)
    claim.upstream["plan_motion"]["artifacts"][0]["sha256"]="response-two"
    assert first != Supervisor._fingerprint(Sup(),claim)


def test_plan_query_is_persisted_and_detects_corruption(tmp_env, monkeypatch):
    from app.main import create_app
    from app.config import Config
    from fastapi.testclient import TestClient
    from app.artifacts import atomic_write_json, bundle_single_file
    from conftest import make_fake_bundle
    cfg=Config(data_dir=tmp_env["root"],db_path=tmp_env["db"],
               artifacts_dir=tmp_env["artifacts"],logs_dir=tmp_env["root"]/"logs")
    app=create_app(cfg)
    repo=app.state.repo
    job=repo.create_job(dict(pipeline.DEFAULT_INPUT))
    monkeypatch.setenv("DEEPSEEK_API_KEY","test-secret")
    with TestClient(app) as client:
        assert client.get(f"/api/jobs/{job['id']}/motion-plan").json()["ready"] is False
        assert "test-secret" not in client.get("/api/planner").text
        for name in ("inspect_asset","extract_frames"):
            claim=repo.claim_next_step("w")
            assert claim.step_name==name
            repo.complete_attempt(claim.attempt_id,"w",make_fake_bundle(repo,claim),"fp-"+name)
        claim=repo.claim_next_step("w")
        assert claim.step_name=="plan_motion"
        out=Path(claim.work_dir)/"motion_plan.json"
        atomic_write_json(out,planner.mock_plan(pipeline.DEFAULT_INPUT))
        bundle=bundle_single_file("motion_plan",out,Path(claim.work_dir),{})
        repo.complete_attempt(claim.attempt_id,"w",[bundle],"fp-plan")
        result=client.get(f"/api/jobs/{job['id']}/motion-plan")
        assert result.status_code==200 and result.json()["plan"]["provider"]=="mock"
        out.write_text("corrupted",encoding="utf-8")
        assert client.get(f"/api/jobs/{job['id']}/motion-plan").status_code==409
        assert client.get("/api/jobs/missing/motion-plan").status_code==404
