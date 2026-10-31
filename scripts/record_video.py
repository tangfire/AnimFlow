"""Record an uncut, captioned real browser demonstration (no voiceover).

Explicitly makes two paid DeepSeek requests, plus offline recovery/resource
demonstrations. Only terminates the processes created by this script.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"scripts"))
from demo import kill_tree

OVERLAY=r"""() => {
  window.addEventListener('DOMContentLoaded', () => {
    const style=document.createElement('style');
    style.textContent=`body { padding-bottom: 125px !important; }
      #record-caption { position:fixed;bottom:0;left:0;right:0;z-index:999999;
      background:rgba(15,23,42,.97);color:#f8fafc;padding:14px 30px;
      min-height:100px;box-sizing:border-box;pointer-events:none;
      font-family:'Microsoft YaHei','Segoe UI',sans-serif;border-top:3px solid #38bdf8; }
      #record-caption strong {color:#7dd3fc;font-size:17px;display:block;margin-bottom:6px}
      #record-caption span {font-size:21px;line-height:1.6}
      #record-cursor { position:fixed;width:22px;height:22px;border:2px solid #f97316;
      border-radius:50%;pointer-events:none;z-index:1000000;transform:translate(-50%,-50%); }
      .record-click {background:rgba(249,115,22,.3)} `;
    document.head.appendChild(style);
    const caption=document.createElement('div');caption.id='record-caption';
    caption.innerHTML='<strong>AnimFlow · 自动化浏览器实录 · 无旁白</strong><span>准备连接本机服务…</span>';
    document.body.appendChild(caption);
    const cursor=document.createElement('div');cursor.id='record-cursor';
    document.body.appendChild(cursor);
    document.addEventListener('mousemove',e=>{cursor.style.left=e.clientX+'px';cursor.style.top=e.clientY+'px'});
    document.addEventListener('mousedown',()=>cursor.classList.add('record-click'));
    document.addEventListener('mouseup',()=>cursor.classList.remove('record-click'));
  });
}"""


def main():
    from app.config import load_config
    load_config()
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        raise SystemExit("Configure the local .env first; recording includes paid API requests.")
    from playwright.sync_api import sync_playwright
    import imageio_ffmpeg
    stamp=time.strftime("%Y%m%d-%H%M%S")
    data=ROOT/"data"/"video-recording"/stamp
    data.mkdir(parents=True,exist_ok=False)
    output=ROOT/"deliverables"
    output.mkdir(exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1",0));port=s.getsockname()[1]
    base=f"http://127.0.0.1:{port}"
    env=os.environ.copy()
    env.update({"ANIMFLOW_DATA_DIR":str(data),"ANIMFLOW_DB_PATH":str(data/"animflow.db"),
                "ANIMFLOW_ARTIFACTS_DIR":str(data/"artifacts"),"ANIMFLOW_LOGS_DIR":str(data/"logs"),
                "ANIMFLOW_POLL_S":"0.2","ANIMFLOW_HEARTBEAT_S":"0.5",
                "ANIMFLOW_LEASE_S":"3","ANIMFLOW_STEP_SPEED":"2","PYTHONPATH":str(ROOT)})
    handles=[]
    processes=[]
    events=[]
    start=time.monotonic()
    def spawn(args,label):
        handle=(data/(label+".log")).open("w",encoding="utf-8")
        handles.append(handle)
        proc=subprocess.Popen([sys.executable,*args],cwd=ROOT,env=env,stdout=handle,stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        processes.append(proc)
        return proc
    def note(kind,**detail):
        record={"elapsed_seconds":round(time.monotonic()-start,2),"kind":kind,**detail}
        events.append(record)
        print(json.dumps(record,ensure_ascii=True),flush=True)
    api_proc=spawn(["-m","uvicorn","app.main:app","--host","127.0.0.1","--port",str(port),"--log-level","warning"],"api")
    worker=spawn(["-m","app.worker"],"worker-1")
    note("SERVICES_STARTED",api_pid=api_proc.pid,worker_pid=worker.pid)
    context=None
    video=None
    completed=False
    try:
        import urllib.request
        for _ in range(100):
            try:
                with urllib.request.urlopen(base+"/healthz",timeout=1) as r:
                    if r.status==200:break
            except OSError:time.sleep(.2)
        else:raise AssertionError("API did not start")
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            context=browser.new_context(viewport={"width":1600,"height":900},
                                        record_video_dir=str(data/"raw-video"),
                                        record_video_size={"width":1600,"height":900},
                                        accept_downloads=True)
            context.add_init_script("("+OVERLAY+")()")
            page=context.new_page()
            video=page.video
            page.goto(base,wait_until="networkidle")
            page.wait_for_function("document.querySelector('#planner-hint').textContent.includes('已配置')")
            def caption(title,text,hold=3):
                page.evaluate("([title,text])=>{const e=document.querySelector('#record-caption');e.querySelector('strong').textContent=title;e.querySelector('span').textContent=text}",[title,text])
                note("CAPTION",title=title,text=text)
                page.wait_for_timeout(hold*1000)
            def click(selector):
                previous=page.evaluate("state.selectedJobId") if "data-preset" in selector else None
                loc=page.locator(selector)
                loc.scroll_into_view_if_needed()
                box=loc.bounding_box()
                page.mouse.move(box["x"]+box["width"]/2,box["y"]+box["height"]/2,steps=15)
                page.wait_for_timeout(500)
                loc.click()
                if "data-preset" in selector:
                    page.wait_for_function("old=>state.selectedJobId!==old && document.querySelector('#d-id').textContent===state.selectedJobId",arg=previous)
            def job_id():return page.evaluate("state.selectedJobId")
            def details(jid):
                response=page.request.get(base+f"/api/jobs/{jid}")
                assert response.ok
                return response.json()
            def done():page.wait_for_selector("#d-status.SUCCEEDED",timeout=100000)
            def show(selector,hold=5):
                page.locator(selector).scroll_into_view_if_needed()
                page.wait_for_timeout(hold*1000)

            caption("AnimFlow｜可观测、可恢复的 AI 任务管线", "真实浏览器连续实录；字幕是说明层，所有任务状态均来自实际 API。",6)
            caption("运行方式｜API + 独立 Worker + SQLite", "模型负责动作规划，Pillow 渲染 GIF；GPU 只模拟逻辑预约，不代表真实显卡。",6)

            caption("01｜真实 DeepSeek 规划", "输入一句动画要求；调用模型产生结构化方案，并实际控制颜色和运动。",4)
            page.locator("#p-brief").fill("青蓝色光球沿圆形轨道运动，深蓝背景，紫色光环，科技感")
            assert page.input_value("#p-planner")=="deepseek"
            click('button[data-preset="ok"]')
            done()
            real_job=job_id()
            plan=page.request.get(base+f"/api/jobs/{real_job}/motion-plan").json()["plan"]
            assert plan["provider"]=="deepseek" and plan["request_id"]
            note("REAL_MODEL_SUCCESS",job_id=real_job,model=plan["model"],usage=plan["usage"],latency_ms=plan["latency_ms"])
            caption("真实返回｜模型、延迟、token 和 request_id", "规划产物已经持久化；这些数值来自本次 DeepSeek 响应，不是模拟数据。",1)
            show("#d-plan",8)
            caption("真实产物｜JSON 方案 → Pillow → 36 帧 GIF", "光球运动和配色来自规划；不是通用文生视频，输出范围是有限运动原语。",1)
            show("#d-gif",7)
            gif=page.locator("#gif-img")
            page.wait_for_function("document.querySelector('#gif-img').naturalWidth>0")
            ziplink=page.locator('.artifact-row').filter(has=page.locator('.kind',has_text="final_zip")).locator("a")
            with page.expect_download() as download_info:
                ziplink.click()
            download=download_info.value
            archive=data/"downloaded-result.zip"
            download.save_as(archive)
            with zipfile.ZipFile(archive) as zf:
                assert zf.testzip() is None
                names=zf.namelist()
            note("ZIP_VERIFIED",files=names)
            caption("可交付产物｜ZIP 下载通过完整性检查", "已实际下载并验证 ZIP：preview.gif、params.json、source_summary.json、README.txt。",6)

            page.evaluate("window.scrollTo(0,0)")
            caption("02｜失败耗尽预算 → 人工重试", "再创建一个真实模型任务，仅在下游风格处理持续注入失败；每轮最多三次尝试。",4)
            page.locator("#p-brief").fill("橙色光球上下弹跳，深紫色背景，金色光环，轻快活泼，使用bounce")
            click('button[data-preset="permanent"]')
            page.wait_for_selector("#d-status.FAILED",timeout=100000)
            retry_job=job_id()
            before=page.request.get(base+f"/api/jobs/{retry_job}/motion-plan").json()
            assert before["ready"] and before["plan"]["provider"]=="deepseek"
            before_details=details(retry_job)
            failed_step=next(s for s in before_details["steps"] if s["name"]=="stylize_frames")
            assert failed_step["attempt_count"]==3
            caption("失败状态真实保留｜FAILED + BLOCKED", "风格处理三次失败后停止推进，下游 BLOCKED；成功的模型规划保持 SUCCEEDED。",6)
            show("#d-attempts",5)
            show("#btn-retry",1)
            caption("人工重试｜清除演示故障，保留历史", "重试只恢复失败/阻塞步骤；已经成功的规划应复用，而不是重新消耗模型 token。",3)
            click("#btn-retry")
            done()
            after=page.request.get(base+f"/api/jobs/{retry_job}/motion-plan").json()
            final=details(retry_job)
            planned=next(s for s in final["steps"] if s["name"]=="plan_motion")
            assert planned["attempt_count"]==1 and after["artifact_id"]==before["artifact_id"]
            assert before["plan"]["request_id"]==after["plan"]["request_id"]
            note("REAL_PLAN_REUSED",job_id=retry_job,plan_attempts=1,artifact_id=after["artifact_id"],usage=after["plan"]["usage"])
            caption("复用已验证｜plan_motion attempt = 1", "规划 artifact_id 和 request_id 不变；风格处理新增成功尝试，旧三次失败历史保留。",1)
            show("#d-attempts",8)
            show("#d-gif",5)

            page.evaluate("window.scrollTo(0,0)")
            page.select_option("#p-planner","mock")
            caption("03｜自动重试（切换离线 MOCK）", "下面专注恢复机制，避免重复模型消费；第一次风格处理失败后自动退避并重试。",4)
            click('button[data-preset="transient"]')
            done()
            auto_job=job_id()
            at=page.request.get(base+f"/api/jobs/{auto_job}/attempts").json()["items"]
            states=[a["status"] for a in at if a["step_name"]=="stylize_frames"]
            assert states==["FAILED","SUCCEEDED"]
            note("AUTO_RETRY_VERIFIED",job_id=auto_job,attempt_states=states)
            caption("自动重试完成｜旧失败 + 新成功", "可以查询每次 attempt；成功上游没有重跑。模型模式明确显示 MOCK。",1)
            show("#d-attempts",7)

            page.evaluate("window.scrollTo(0,0)")
            caption("04｜强制终止 Worker 后恢复", "提交慢任务，等 stylize_frames RUNNING 后，实际强杀本脚本启动的 Worker 进程树。",4)
            click('button[data-preset="slow"]')
            page.wait_for_function("[...document.querySelectorAll('.step')].some(e=>e.querySelector('.sname').textContent.includes('stylize_frames') && e.querySelector('.status').textContent==='RUNNING')",timeout=100000)
            crash_job=job_id()
            pre=details(crash_job)
            upstream={s["name"]:s["attempt_count"] for s in pre["steps"] if s["status"]=="SUCCEEDED"}
            old_pid=worker.pid
            kill_tree(worker)
            assert worker.poll() is not None
            note("WORKER_HARD_KILLED",pid=old_pid,job_id=crash_job,exit_code=worker.returncode)
            caption("实际执行 taskkill /F /T｜PID "+str(old_pid), "Worker 已强制结束。API 仍可查询；短暂 RUNNING 属于租约尚未回收的状态。",5)
            worker=spawn(["-m","app.worker"],"worker-2")
            note("WORKER_RESTARTED",pid=worker.pid)
            caption("启动新的 Worker｜PID "+str(worker.pid), "新进程回收过期租约，记录 WORKER_LOST，再用新的 attempt 执行中断步骤。",3)
            done()
            crash=details(crash_job)
            for s in crash["steps"]:
                if s["name"] in upstream:assert s["attempt_count"]==upstream[s["name"]]
            attempts=page.request.get(base+f"/api/jobs/{crash_job}/attempts").json()["items"]
            crash_states=[a["status"] for a in attempts if a["step_name"]=="stylize_frames"]
            assert crash_states==["WORKER_LOST","SUCCEEDED"]
            note("CRASH_RECOVERY_VERIFIED",job_id=crash_job,states=crash_states,upstream_unchanged=True)
            caption("恢复已验证｜WORKER_LOST → 新 attempt 成功", "上游尝试次数保持不变。执行语义是 at-least-once，单步中断会整步重跑。",1)
            show("#d-attempts",8)
            show("#d-events",5)

            page.evaluate("window.scrollTo(0,0)")
            caption("05｜两个任务争抢一个模拟 GPU 槽", "CPU 与 GPU 分池；等待 GPU 不消耗尝试。此处只演示数据库逻辑预约，不是真实 CUDA 隔离。",4)
            click('button[data-preset="gpu"]')
            # Observe actual slot holders while the two jobs progress.
            deadline=time.monotonic()+60
            max_holders=0
            while time.monotonic()<deadline:
                resources=page.request.get(base+"/api/resources").json()
                holders=[s for s in resources["slots"] if s["kind"]=="gpu" and s["holder"]]
                max_holders=max(max_holders,len(holders))
                assert len(holders)<=1
                page.wait_for_timeout(700)
                listing=page.request.get(base+"/api/jobs?limit=2").json()["items"]
                if len(listing)==2 and all(j["status"]=="SUCCEEDED" for j in listing):break
            else:raise AssertionError("GPU competition did not finish")
            assert max_holders==1
            note("GPU_LOGICAL_MUTEX_VERIFIED",peak_holders=max_holders)
            caption("逻辑预约验证｜GPU holder 峰值 = 1", "两个任务都已完成。真实显卡的隔离、显存和温度采集仍属于未实现范围。",6)

            selected=job_id()
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#d-status.SUCCEEDED",timeout=15000)
            assert job_id()==selected
            caption("刷新后恢复｜状态、历史与产物来自持久化", "单机 SQLite · 步骤边界恢复 · 真实 DeepSeek 规划 + Pillow 渲染 · GPU 模拟预约。",7)
            note("RELOAD_VERIFIED",job_id=selected)
            caption("演示结束｜源码与测试证据", "github.com/tangfire/AnimFlow ｜ 75 项自动化测试；本视频连续实录，无语音旁白。",7)
            page.screenshot(path=str(output/"AnimFlow-demo-poster.png"))
            context.close();context=None
            raw=Path(video.path())
            final=output/"AnimFlow-demo.mp4"
            command=[imageio_ffmpeg.get_ffmpeg_exe(),"-y","-i",str(raw),"-c:v","libx264",
                     "-preset","fast","-crf","21","-pix_fmt","yuv420p","-movflags","+faststart","-an",str(final)]
            with (data/"encode.log").open("w",encoding="utf-8") as log:
                subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
            completed=final.is_file() and final.stat().st_size>0
            note("VIDEO_EXPORTED",path=str(final),bytes=final.stat().st_size,raw=str(raw))
            browser.close()
    finally:
        if context is not None:
            try:context.close()
            except Exception:pass
        for proc in reversed(processes):
            kill_tree(proc)
        for handle in handles:handle.close()
        (output/"AnimFlow-demo-evidence.json").write_text(
            json.dumps({"completed":completed,"data_dir":str(data),"events":events},ensure_ascii=False,indent=2),encoding="utf-8")
    if not completed:raise SystemExit(1)


if __name__=="__main__":
    main()
