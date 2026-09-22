/* 最小可操作界面:登录 / 会谈列表 / 上传与进度 / 人工转写导入 / 纪要 / 问答。
 * 只做已有 API 的呈现,不含任何业务判断:权限、模型选择、证据校验全部在后端。
 */
"use strict";

const state = { token: null, user: null, meetingId: null, pollers: {} };

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (resp.status === 401) { logout(); throw new Error("登录已过期,请重新登录"); }
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!resp.ok) {
    const detail = data && data.detail
      ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))
      : resp.statusText;
    throw new Error(`${resp.status} ${detail}`);
  }
  return data;
}

function toast(msg, isErr) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), isErr ? 9000 : 4000);
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const show = (id) => ["login", "meetings", "detail"].forEach((v) =>
  document.getElementById("view-" + v).classList.toggle("hidden", v !== id));
const tag = (label, ok) => `<span class="tag ${ok ? "real" : "mock"}">${esc(label)}</span>`;

/* ---------- 模型组合徽标(如实显示哪些环节是真实模型) ---------- */
async function loadMeta() {
  const el = document.getElementById("meta-badge");
  try {
    const m = await api("/meta");
    const p = m.providers;
    el.innerHTML = ["asr", "llm", "embed"].map((k) =>
      tag(`${k.toUpperCase()} ${p[k].model}`, !p[k].is_mock)).join(" ");
    el.title = m.asr_note || "";
  } catch (e) {
    el.textContent = "模型配置不可用:" + e.message;
    el.classList.add("err");
  }
}

/* ---------- 登录 ---------- */
document.getElementById("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = document.getElementById("login-msg");
  msg.textContent = "";
  try {
    const r = await api("/auth/login", {
      method: "POST",
      json: {
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
      },
    });
    state.token = r.access_token;
    state.user = await api("/auth/me");
    document.getElementById("who").innerHTML =
      `${esc(state.user.display_name)} · ${esc(state.user.role)} · 机构 #${state.user.org_id}`
      + ` <button id="btn-logout">退出</button>`;
    document.getElementById("btn-logout").onclick = logout;
    await loadMeta();   // 元信息需登录后再取(仅认证用户可读)
    await loadMeetings();
  } catch (e) { msg.textContent = e.message; }
});

function logout() {
  state.token = null; state.user = null; state.meetingId = null;
  Object.values(state.pollers).forEach(clearInterval);
  state.pollers = {};
  document.getElementById("who").innerHTML = "";
  show("login");
}

/* ---------- 会谈列表 ---------- */
document.getElementById("btn-new").onclick = () =>
  document.getElementById("new-meeting").classList.toggle("hidden");

document.getElementById("new-meeting").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/meetings", {
      method: "POST",
      json: {
        title: document.getElementById("m-title").value,
        vendor_name: document.getElementById("m-vendor").value,
        visibility: document.getElementById("m-vis").value,
        scheduled_at: new Date().toISOString(),
      },
    });
    document.getElementById("new-meeting").classList.add("hidden");
    toast("会谈已创建");
    await loadMeetings();
  } catch (e) { toast(e.message, true); }
});

async function loadMeetings() {
  const list = await api("/meetings");
  const ul = document.getElementById("meeting-list");
  ul.innerHTML = list.map((m) => `
    <li><a href="#" data-id="${m.id}">${esc(m.title)}</a>
      <span class="sub">${esc(m.vendor_name)} · ${esc(m.visibility)} · ${esc(m.scheduled_at)}</span></li>`
  ).join("") || "<li>暂无会谈</li>";
  ul.querySelectorAll("a").forEach((a) => a.onclick = (ev) => {
    ev.preventDefault(); openMeeting(Number(a.dataset.id));
  });
  show("meetings");
}

document.getElementById("btn-back").onclick = loadMeetings;

/* ---------- 会谈详情 ---------- */
async function openMeeting(id) {
  state.meetingId = id;
  const m = await api(`/meetings/${id}`);
  document.getElementById("d-title").textContent = m.title;
  document.getElementById("d-meta").innerHTML =
    `厂商:${esc(m.vendor_name)} · 议题:${esc(m.topic || "-")} · `
    + `可见性:${esc(m.visibility)} · 机构 #${m.org_id}`;
  renderRecordings(m.recordings || []);
  await loadMinutes();
  show("detail");
}

function renderRecordings(recs) {
  const box = document.getElementById("jobs");
  if (!recs.length) { box.innerHTML = '<p class="hint">尚未上传录音。</p>'; return; }
  box.innerHTML = recs.map((r) => `
    <div class="rec">
      <div><b>${esc(r.original_filename)}</b>
        <span class="sub">SHA-256 ${esc(r.checksum.slice(0, 12))}…</span></div>
      <div class="sub">转写来源:${transcriptLabel(r)}</div>
      <div class="sub">任务 #${r.job_id} 状态:
        <span id="st-${r.job_id}">${esc(r.job_status || "-")}</span>
        ${r.job_current_stage ? "· 当前阶段 " + esc(r.job_current_stage) : ""}</div>
      <div id="stages-${r.job_id}"></div>
    </div>`).join("");
  recs.forEach((r) => r.job_id && pollJob(r.job_id));
}

const STAGES = ["prepare", "transcribe", "minutes", "index"];

const SOURCE_LABEL = {
  asr: "自动转写(ASR)", human_transcription: "人工转写导入",
  human_correction: "人工校对(在既有版本上追加)", re_asr: "重新转写",
};

function transcriptLabel(r) {
  if (!r.transcript_source) return '<span class="hint">尚无转写版本</span>';
  const mock = r.transcript_source === "asr";   // asr 版本由当前配置的 ASR 产出
  return `v${r.transcript_version_no} · ${esc(SOURCE_LABEL[r.transcript_source] || r.transcript_source)}`
    + (mock ? ' <span class="tag mock">来自 mock ASR</span>' : "");
}

function pollJob(jobId) {
  clearInterval(state.pollers[jobId]);
  const tick = async () => {
    let job;
    try { job = await api(`/jobs/${jobId}`); } catch (e) { return; }
    const st = document.getElementById("st-" + jobId);
    if (st) st.textContent = job.status;
    const box = document.getElementById("stages-" + jobId);
    if (box) {
      box.innerHTML = STAGES.map((name) => {
        const s = job.stages.find((x) => x.stage_name === name);
        const status = s ? s.status : "pending";
        const cls = status === "succeeded" ? "ok" : status === "failed" ? "bad" : "";
        return `<span class="stage ${cls}">${name}:${esc(status)}`
          + (s && s.attempt_count ? `(第${s.attempt_count}次)` : "") + "</span>";
      }).join("") + (job.last_error
        ? `<div class="err">错误:${esc(job.last_error)}</div>` : "");
    }
    if (job.status === "succeeded" || job.status === "failed") {
      clearInterval(state.pollers[jobId]);
      delete state.pollers[jobId];
      if (job.status === "succeeded") await loadMinutes();
    }
  };
  tick();
  state.pollers[jobId] = setInterval(tick, 1500);
}

document.getElementById("btn-upload").addEventListener("click", async () => {
  const f = document.getElementById("audio").files[0];
  if (!f) { toast("请先选择 wav 录音文件", true); return; }
  const fd = new FormData();
  fd.append("file", f);
  try {
    const r = await api(`/meetings/${state.meetingId}/recordings`, { method: "POST", body: fd });
    toast(r.duplicate ? "内容重复:返回已有任务 " + r.job_id : "已受理,任务 " + r.job_id);
    const m = await api(`/meetings/${state.meetingId}`);
    renderRecordings(m.recordings || []);
  } catch (e) { toast(e.message, true); }
});

/* ---------- 人工转写导入 ---------- */
document.getElementById("btn-import").addEventListener("click", async () => {
  const raw = document.getElementById("transcript").value.trim();
  if (!raw) { toast("请粘贴转写文本", true); return; }
  const lines = raw.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const i = l.search(/[:：]/);
    return i > 0
      ? { speaker: l.slice(0, i).trim(), text: l.slice(i + 1).trim() }
      : { speaker: null, text: l };
  });
  const box = document.getElementById("import-result");
  box.innerHTML = '<span class="hint">导入中(将调用配置指定的模型)…</span>';
  try {
    const r = await api(`/meetings/${state.meetingId}/transcript`, {
      method: "POST",
      json: { lines, generate_minutes: document.getElementById("gen-minutes").checked },
    });
    const p = r.providers;
    box.innerHTML = `<div>转写版本 #${r.transcript_version_id}
      (${esc(r.source_type)})·片段 ${r.segments}·改动 ${r.edited}</div>
      <div>纪要条目 ${r.minutes ? r.minutes.items : "未生成"}·索引 ${r.index.chunks} 块</div>
      <div class="sub">实际模型: LLM ${esc(p.llm.model)} ${p.llm.is_mock ? "(mock)" : "(真实)"}
        / Embed ${esc(p.embed.model)} ${p.embed.is_mock ? "(mock)" : "(真实)"}</div>`;
    toast("转写已导入并发布");
    await loadMinutes();
  } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
});

/* ---------- 纪要 ---------- */
async function loadMinutes() {
  const box = document.getElementById("minutes");
  let m;
  try {
    m = await api(`/meetings/${state.meetingId}/minutes`);
  } catch (e) {
    box.innerHTML = '<span class="hint">尚无纪要(先完成转写与纪要阶段)。</span>';
    return;
  }
  box.innerHTML = `<div class="sub">版本 v${m.version_no} · ${esc(m.source_type)}
      · ${esc(m.status)} · 共 ${m.versions ? m.versions.length : 1} 个版本</div>`
    + m.content.items.map((it) => `
      <div class="item">
        <div><span class="pill">${esc(it.type)}</span>
          <span class="pill cert">${esc(it.certainty)}</span>
          ${it.needs_confirmation ? '<span class="pill warn">待人工确认</span>' : ""}</div>
        <div class="text">${esc(it.text)}</div>
        <div class="sub">负责人:${esc(it.owner || "-")} · 截止:${esc(it.due_date || "-")}</div>
        <details><summary>证据 ${it.evidence.length} 条</summary>
          ${it.evidence.map((e) => `<div class="ev">
            <b>SEG-${e.segment_id}</b> ${fmtTime(e.start_ms, e.end_ms)}
            ${esc(e.speaker || "未知")}:${esc(e.text)}</div>`).join("")}
        </details>
      </div>`).join("");
}

function fmtTime(a, b) {
  if (a === null || a === undefined) return "";
  return `[${Math.floor(a / 1000)}s–${Math.floor((b || 0) / 1000)}s]`;
}

/* ---------- 问答 ---------- */
document.getElementById("ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = document.getElementById("question").value.trim();
  const box = document.getElementById("answer");
  box.innerHTML = '<span class="hint">提问中…</span>';
  try {
    const r = await api(`/meetings/${state.meetingId}/ask`,
      { method: "POST", json: { question: q } });
    if (!r.has_sufficient_evidence) {
      box.innerHTML = `<div class="warn-box">${tag("回答模型 " + (r.is_mock ? "mock" : "真实"), !r.is_mock)}
        <div>证据不足,系统未生成回答。</div>
        <div class="sub">原始检索 ${r.raw_retrieved_count} 条,过阈值候选
          ${r.relevant_candidate_count} 条(0 表示无相关可用证据)。
          无回答、无引用,不拼接候选冒充答案。</div></div>`;
      return;
    }
    box.innerHTML = `<div class="ans">${tag("回答模型 " + (r.is_mock ? "mock" : "真实"), !r.is_mock)}
      <div class="text">${esc(r.answer)}</div>
      <div class="sub">候选 ${r.relevant_candidate_count} 条 · 引用 ${r.citations.length} 条</div>
      ${r.citations.map((c) => `<div class="ev"><b>SEG-${c.segment_id}</b>
        ${fmtTime(c.start_ms, c.end_ms)} ${esc(c.speaker || "未知")}:${esc(c.text)}
        <span class="sub">相似度 ${c.similarity}</span></div>`).join("")}</div>`;
  } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
});

