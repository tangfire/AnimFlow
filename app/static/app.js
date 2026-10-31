/* AnimFlow 看板：全部状态来自 API 查询；浏览器只做轮询与渲染。
 * 事件用 after_id 增量拉取（不重复不漏）；终态降频；断网显示状态并继续重试。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const TERMINAL = new Set(["SUCCEEDED", "FAILED"]);
const state = {
  selectedJobId: new URLSearchParams(location.search).get("job_id") || null,
  eventsAfterId: 0,
  events: [],            // 已接收事件，按 id 去重
  detailSeq: 0,          // 轮询代次，防止旧响应覆盖
  jobToken: 0,           // 选择代次：切换任务后丢弃所有在途的旧任务响应（防串台）
  offline: false,
  timers: { list: null, detail: null, events: null, resources: null },
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { /* 非 JSON */ }
  if (!res.ok) {
    const err = new Error((body && body.error && body.error.message) || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = body && body.error && body.error.code;
    throw err;
  }
  setOnline(true);
  return body;
}

function setOnline(ok) {
  state.offline = !ok;
  const badge = $("#conn-badge");
  badge.textContent = ok ? "已连接" : "断线，重试中…";
  badge.className = "badge " + (ok ? "green" : "red");
}

function fmtTime(epoch) {
  if (!epoch) return "-";
  return new Date(epoch * 1000).toLocaleTimeString();
}
function fmtFull(epoch) {
  if (!epoch) return "-";
  return new Date(epoch * 1000).toLocaleString();
}
function shortId(id) { return id ? id.slice(0, 8) : "-"; }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- 预设与创建 ---------- */

const PRESETS = {
  ok: { params: {}, fault: {} },
  transient: { params: {}, fault: { fail_first_n: { stylize_frames: 1 } } },
  permanent: { params: {}, fault: { always_fail: { stylize_frames: true } } },
  slow: { params: {}, fault: { slow: { stylize_frames: 6 } } },
};

async function createJob(params, fault) {
  const msg = $("#create-msg");
  msg.textContent = "提交中…"; msg.className = "msg";
  try {
    const body = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params: { ...currentFormParams(), ...params }, demo_fault: fault }),
    });
    msg.textContent = `已提交 ${shortId(body.id)}`; msg.className = "msg ok";
    selectJob(body.id);
    await refreshJobs();
  } catch (e) {
    msg.textContent = `失败: ${e.message}`; msg.className = "msg err";
  }
}

function currentFormParams() {
  return {
    width: Number($("#p-width").value),
    height: Number($("#p-height").value),
    frame_count: Number($("#p-frames").value),
    fps: Number($("#p-fps").value),
    style: $("#p-style").value,
    seed: Number($("#p-seed").value),
    planner_mode: $("#p-planner").value,
    planner_model: "deepseek-flash",
    brief: $("#p-brief").value,
  };
}

document.querySelectorAll(".preset").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const key = btn.dataset.preset;
    if (key === "gpu") {
      // 两个任务争抢一个 mock GPU 槽
      await createJob({}, { slow: { stylize_frames: 4 } });
      await createJob({}, { slow: { stylize_frames: 4 } });
    } else {
      const p = PRESETS[key];
      await createJob(p.params, p.fault);
    }
  });
});
$("#btn-create-custom").addEventListener("click", () => createJob(currentFormParams(), {}));
$("#btn-refresh").addEventListener("click", () => { refreshJobs(); refreshResources(); });

/* ---------- 任务列表 ---------- */

async function refreshJobs() {
  try {
    const data = await api("/api/jobs?limit=30");
    const box = $("#job-list");
    if (!data.items.length) { box.innerHTML = '<div class="hint">暂无任务，点上方预设创建。</div>'; return; }
    box.innerHTML = data.items.map((j) => `
      <div class="job-item ${j.id === state.selectedJobId ? "selected" : ""}" data-id="${j.id}">
        <div>
          <div class="jid">${shortId(j.id)}</div>
          <div class="jmeta">${new Date(j.created_at * 1000).toLocaleString()}</div>
        </div>
        <div style="text-align:right">
          <span class="status ${j.status}">${j.status}</span>
          <div class="jmeta">${j.steps_done}/${j.steps_total} 步${j.fatal_error_code ? " · " + j.fatal_error_code : ""}</div>
        </div>
      </div>`).join("");
    box.querySelectorAll(".job-item").forEach((el) =>
      el.addEventListener("click", () => selectJob(el.dataset.id)));
  } catch (e) {
    setOnline(false);
  }
}

/* ---------- 任务详情 ---------- */

function selectJob(jobId) {
  state.selectedJobId = jobId;
  state.jobToken += 1;          // 使所有在途的旧任务请求结果作废
  state.eventsAfterId = 0;
  state.events = [];
  $("#d-events").innerHTML = "";
  $("#d-plan").textContent = "等待动作规划产物…";
  $("#event-count").textContent = "";
  const url = new URL(location);
  url.searchParams.set("job_id", jobId);
  history.replaceState(null, "", url);
  $("#detail-placeholder").hidden = true;
  $("#detail").hidden = false;
  refreshJobs();
  pollDetail(true);
}

async function pollDetail(immediate) {
  const jobId = state.selectedJobId;
  if (!jobId) return;
  const seq = ++state.detailSeq;
  clearTimeout(state.timers.detail);
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (seq !== state.detailSeq || jobId !== state.selectedJobId) return;
    renderDetail(job);
    scheduleDetail(TERMINAL.has(job.status) ? 3000 : 1000);
  } catch (e) {
    if (e.status === 404) {
      $("#detail").innerHTML = `<div class="card">任务 ${shortId(jobId)} 不存在（可能被清理）。</div>`;
      return;
    }
    setOnline(false);
    scheduleDetail(2000);
  }
  if (immediate) pollEvents();
}

function scheduleDetail(ms) {
  clearTimeout(state.timers.detail);
  state.timers.detail = setTimeout(() => pollDetail(false), ms);
}

function renderDetail(job) {
  const st = $("#d-status");
  st.textContent = job.status;
  st.className = "status " + job.status;
  $("#d-id").textContent = job.id;
  $("#d-meta").innerHTML =
    `创建 ${fmtFull(job.created_at)} · 开始 ${fmtFull(job.started_at)} · 结束 ${fmtFull(job.finished_at)} · ` +
    `pipeline <code>${esc(job.pipeline_version)}</code> · input_hash <code>${job.input_hash.slice(0, 12)}…</code>`;
  const fatal = $("#d-fatal");
  if (job.fatal_error_code) {
    fatal.hidden = false;
    fatal.textContent = `${job.fatal_error_code}: ${job.fatal_error_message || ""}`;
  } else fatal.hidden = true;

  const done = job.steps.filter((s) => s.status === "SUCCEEDED").length;
  $("#d-progress").textContent = `已完成 ${done}/${job.steps.length} 步` +
    (job.artifact_health.status === "invalid" ? " · ⚠ 产物健康: invalid" : "");

  const canRetry = job.status === "FAILED";
  $("#btn-retry").hidden = !canRetry;
  $("#clear-fault-wrap").hidden = !canRetry;
  $("#btn-retry").onclick = async () => {
    $("#btn-retry").disabled = true;
    try {
      const body = JSON.stringify({ clear_fault: $("#d-clear-fault").checked });
      await api(`/api/jobs/${job.id}/retry`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
      });
      state.eventsAfterId = 0; state.events = []; $("#d-events").innerHTML = "";
      pollDetail(true);
    } catch (e) {
      alert(`重试被拒绝: ${e.message}`);
    } finally { $("#btn-retry").disabled = false; }
  };

  // 步骤卡
  $("#d-steps").innerHTML = job.steps.map((s) => {
    const running = s.status === "RUNNING";
    const pct = s.progress != null ? Math.round(s.progress * 100) : null;
    const hasBar = running;
    const indet = running && pct == null;
    return `<div class="step ${s.status.toLowerCase()} ${running ? "running" : ""} ${s.status === "FAILED" ? "failed" : ""}">
      <div class="sname">${esc(s.name)} <span class="dim">v${s.version}</span></div>
      <div class="srow"><span class="status ${s.status}">${s.status}</span>
        · ${esc(s.resource)} · attempt ${s.attempt_count}
        ${s.attempts_in_round ? `（本轮 ${s.attempts_in_round}/${s.max_attempts_per_round}）` : ""}
        ${s.retry_round ? ` · 第 ${s.retry_round + 1} 轮` : ""}</div>
      <div class="sdep">依赖: ${s.depends_on.length ? s.depends_on.map(esc).join(", ") : "无"}</div>
      ${hasBar ? `<div class="bar ${indet ? "indet" : ""}"><div style="width:${indet ? 40 : (pct || 0)}%"></div></div>
        <div class="srow">${pct != null ? pct + "%" : "进度未知"}${s.progress_message ? " · " + esc(s.progress_message) : ""}</div>` : ""}
      ${s.wait && s.status !== "SUCCEEDED" ? `<span class="wait-chip">${esc(s.wait.reason)}: ${esc(s.wait.message)}</span>` : ""}
      ${s.output_fingerprint ? `<div class="srow mono">fp ${s.output_fingerprint.slice(0, 16)}…</div>` : ""}
      ${s.error_code ? `<div class="err-line">${esc(s.error_code)}: ${esc(s.error_message || "")}</div>` : ""}
      ${s.lease ? `<div class="srow">租约 ${esc(shortId(s.lease.attempt_id))} · ${esc(s.lease.owner || "")}
        · ${s.lease.expired ? "已过期待回收" : "有效至 " + fmtTime(s.lease.lease_expires_at)}</div>` : ""}
      ${s.next_run_at && s.status === "RETRY_WAIT" ? `<div class="srow">下次尝试 ${fmtTime(s.next_run_at)}</div>` : ""}
    </div>`;
  }).join("");

  // 产物
  renderPlan(job.id);
  const arts = [];
  job.steps.forEach((s) => s.artifacts.forEach((a) => arts.push({ ...a, step: s.name })));
  if (!arts.length) {
    $("#d-artifacts").textContent = "无";
  } else {
    $("#d-artifacts").innerHTML = arts.map((a) => `
      <div class="artifact-row">
        <span class="kind">${esc(a.kind)}</span>
        <span class="dim mono">${esc(a.step)}</span>
        <span class="dim mono">${shortId(a.id)}</span>
        <a href="/api/artifacts/${a.id}/download" target="_blank" rel="noopener">下载</a>
      </div>`).join("");
  }
  const gif = arts.find((a) => a.kind === "preview_gif");
  const gifBox = $("#d-gif");
  if (gif) {
    gifBox.hidden = false;
    const img = $("#gif-img");
    // 仅在首次/切换时加载；浏览器对 <img> 会按需拉取（终态不再刷新）
    if (img.dataset.artifactId !== gif.id) {
      img.dataset.artifactId = gif.id;
      img.src = `/api/artifacts/${gif.id}/download?ts=${gif.created_at}`;
    }
  } else gifBox.hidden = true;

  // attempt 表
  renderAttempts(job.id);
  // 事件增量
  pollEvents();
}

async function renderPlan(jobId) {
  const token = state.jobToken;
  try {
    const data = await api(`/api/jobs/${jobId}/motion-plan`);
    if (token !== state.jobToken || jobId !== state.selectedJobId) return;
    if (!data.ready) {
      $("#d-plan").textContent = "等待动作规划产物…（真实调用失败会显示步骤错误）";
      return;
    }
    const p = data.plan;
    const usage = p.usage;
    $("#d-plan").innerHTML = `
      <strong>${esc(p.title)}</strong>
      <p>${esc(p.explanation)}</p>
      <div><span class="badge ${p.provider === "deepseek" ? "green" : "gray"}">${p.provider === "deepseek" ? "真实 DeepSeek" : "MOCK"}</span>
      ${esc(p.model)} · ${esc(p.latency_ms)} ms · prompt ${esc(p.prompt_version)}</div>
      <p>运动 <code>${esc(p.motion)}</code> · 幅度 ${esc(p.intensity)} · 速度 ${esc(p.speed)}</p>
      <p>配色 ${[p.background_color, p.accent_color, p.secondary_color].map(c =>
        /^#[0-9a-f]{6}$/i.test(c) ? `<span style="display:inline-block;background:${c};width:18px;height:18px;vertical-align:middle;border:1px solid #888"></span> ${esc(c)}` : esc(c)).join(" · ")}</p>
      <div>${usage ? `tokens：输入 ${esc(usage.prompt_tokens ?? "未返回")} / 输出 ${esc(usage.completion_tokens ?? "未返回")} / 合计 ${esc(usage.total_tokens ?? "未返回")}` : "MOCK：无模型 token 消耗"}</div>
      <div class="mono dim">request_id ${esc(p.request_id || "无")}</div>`;
  } catch (e) {
    if (token === state.jobToken) $("#d-plan").textContent = `规划查询失败：${e.message}`;
  }
}

async function renderAttempts(jobId) {
  const token = state.jobToken;
  try {
    const data = await api(`/api/jobs/${jobId}/attempts?limit=100`);
    if (token !== state.jobToken || jobId !== state.selectedJobId) return; // 已切换任务，丢弃
    const tbody = $("#d-attempts tbody");
    tbody.innerHTML = data.items.map((a) => `
      <tr>
        <td>${a.attempt_no}</td><td class="mono">${esc(a.step_name)}</td>
        <td><span class="status ${a.status}">${a.status}</span></td>
        <td class="mono">${esc(a.owner)}</td>
        <td>${fmtTime(a.started_at)}</td><td>${fmtTime(a.finished_at)}</td>
        <td>${a.error.code ? esc(a.error.code + ": " + (a.error.message || "")) : "-"}</td>
        <td class="mono dim" title="${esc(a.work_dir)}">${esc(a.work_dir.slice(-42))}</td>
      </tr>`).join("") || '<tr><td colspan="8" class="dim">暂无 attempt</td></tr>';
  } catch (_) { /* 详情刷新会重试 */ }
}

/* ---------- 事件增量（after_id 不重不漏）---------- */

async function pollEvents() {
  const jobId = state.selectedJobId;
  if (!jobId) return;
  const token = state.jobToken;
  try {
    let guard = 0;
    while (guard++ < 20) {
      const page = await api(`/api/jobs/${jobId}/events?after_id=${state.eventsAfterId}&limit=100`);
      if (token !== state.jobToken || jobId !== state.selectedJobId) return; // 已切换任务
      const known = new Set(state.events.map((e) => e.id));
      page.events.forEach((e) => { if (!known.has(e.id)) state.events.push(e); });
      state.eventsAfterId = page.next_after_id;
      if (!page.events.length || !page.has_more) break;
    }
    if (token !== state.jobToken || jobId !== state.selectedJobId) return;
    state.events.sort((a, b) => a.id - b.id);
    renderEvents();
  } catch (_) { /* 断线由 detail 轮询统一处理 */ }
}

function renderEvents() {
  $("#event-count").textContent = `（${state.events.length} 条，增量拉取）`;
  $("#d-events").innerHTML = state.events.map((e) => `
    <div class="event-row">
      <span class="etime">${fmtTime(e.created_at)}</span>
      <span class="etype">${esc(e.type)}</span>
      <span class="epay mono">${esc(JSON.stringify(e.payload))}</span>
    </div>`).join("") || '<div class="dim">暂无事件</div>';
}

/* ---------- 资源 ---------- */

async function refreshResources() {
  try {
    const data = await api("/api/resources");
    $("#resources").innerHTML = data.slots.map((s) => `
      <div class="slot">
        <span><strong>${esc(s.id)}</strong> <span class="dim">${esc(s.device_label)}</span></span>
        <span>${s.holder
          ? `占用 ${esc(shortId(s.holder.attempt_id))} · ${esc(s.holder.step_name || "?")}
             ${s.holder_expired ? '<span class="status TIMED_OUT">过期待回收</span>' : "· 有效"}`
          : "空闲"}</span>
      </div>`).join("");
  } catch (e) { setOnline(false); }
}

/* ---------- 启动轮询 ---------- */

function startPolling() {
  api("/api/planner").then(data => {
    $("#p-planner").value = data.deepseek_configured ? "deepseek" : "mock";
    $("#planner-hint").textContent = data.deepseek_configured
      ? "服务端已配置 Key，默认真实 DeepSeek；需要同时重启 API 和 Worker 使配置生效。"
      : "服务端未配置 Key，默认离线 MOCK。真实模式不会自动降级。";
  }).catch(() => { $("#planner-hint").textContent = "配置查询失败，可手动选择规划模式。"; });
  refreshJobs();
  refreshResources();
  state.timers.list = setInterval(refreshJobs, 2000);
  state.timers.resources = setInterval(refreshResources, 3000);
  if (state.selectedJobId) selectJob(state.selectedJobId);
}
startPolling();
