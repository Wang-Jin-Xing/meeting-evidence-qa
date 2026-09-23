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
    // 把状态码与结构化 detail 一起带出来:调用方需要区分"真失败"与
    // "幂等拒绝"(如重复录音返回 409 + 已有任务号),不能一律当错误显示。
    const err = new Error(`${resp.status} ${detail}`);
    err.status = resp.status;
    err.detail = (data && typeof data.detail === "object") ? data.detail : null;
    throw err;
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
      tag(`${k.toUpperCase()} ${p[k].model}`, !p[k].is_mock)).join(" ")
      + tag(`证据阈值 ${m.retrieval.evidence_threshold}`,
            !p.embed.is_mock && m.retrieval.evidence_threshold > 0.3);
    el.title = (m.asr_note || "") + " | "
      + (m.retrieval.threshold_note || "");
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
  asr: "自动转写", human_transcription: "人工转写导入(非识别输出)",
  human_correction: "人工校对(在既有版本上追加,不覆盖原版本)",
  re_asr: "重新转写",
};

function transcriptLabel(r) {
  // 界面只陈述**能验证的事实**:转写版本的来源类型。
  // 音频是占位还是真实录音,数据库里没有这个事实,不在这里断言。
  if (!r.transcript_source) return '<span class="hint">尚无转写版本</span>';
  let label = `v${r.transcript_version_no} · ${esc(SOURCE_LABEL[r.transcript_source] || r.transcript_source)}`;
  if (r.transcript_source === "asr") {
    // 模型名来自 model_run(数据库事实),不由前端推断是否 mock
    label += ` <span class="tag mock">语音识别输出${
      r.transcript_model ? " · " + esc(r.transcript_model) : ""}(可能含识别错误)</span>`;
  }
  return label;
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
      // 重新拉详情:转写来源/版本等派生字段只有这里能刷新
      if (job.status === "succeeded" && state.meetingId) {
        const m = await api(`/meetings/${state.meetingId}`);
        renderRecordings(m.recordings || []);
        await loadMinutes();
      }
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
    toast("已受理,任务 " + r.job_id);
  } catch (e) {
    // 幂等拒绝不是故障:同一会谈内内容相同的录音不会重复建任务,复用已有任务即可。
    const dup = e.status === 409 && e.detail && e.detail.existing_job_id;
    if (dup) {
      toast(`内容重复:未新建任务,复用已有任务 #${e.detail.existing_job_id}`, false);
    } else {
      toast(e.message, true);
      return;
    }
  }
  try {
    const m = await api(`/meetings/${state.meetingId}`);
    renderRecordings(m.recordings || []);
  } catch (e) { toast("刷新会谈失败:" + e.message, true); }
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
            <b>SEG-${e.segment_id}</b> ${fmtTime(e.start_ms, e.end_ms, e.timestamp_precision)}
            ${playBtn(e)}
            ${esc(e.speaker || "未知")}:${esc(e.text)}</div>`).join("")}
        </details>
      </div>`).join("");
  renderMinutesActions(m);
}

/* 纪要的人工流转:确认只作用于"当前这一版",编辑追加新版本并回到待确认 */
function renderMinutesActions(m) {
  const el = document.getElementById("minutes-actions");
  const confirmed = m.status === "confirmed";
  const at = m.confirmed_at ? String(m.confirmed_at).replace("T", " ").slice(0, 19) : "";
  el.innerHTML = `
    <div class="sub">本版(v${m.version_no})确认状态:${confirmed
      ? `已确认 · 确认人 #${esc(String(m.confirmed_by))} · ${esc(at)}`
      : "未确认(等待人工确认)"}
    </div>
    <button id="btn-confirm-minutes" ${confirmed ? "disabled" : ""}>
      ${confirmed ? "此版本已确认" : "确认此版本"}</button>
    <button id="btn-edit-minutes">编辑为新版</button>
    <details><summary>历史版本 ${m.versions.length} 个</summary>
      <ul>${m.versions.map((v) => `<li>v${v.version_no} · ${esc(v.source_type)} ·
        ${esc(v.status)}${v.confirmed_by ? " · 确认人 #" + esc(String(v.confirmed_by)) : ""}
      </li>`).join("")}</ul>
    </details>`;

  document.getElementById("btn-confirm-minutes").onclick = async () => {
    try {
      await api(`/meetings/${state.meetingId}/minutes/confirm`,
        { method: "POST", json: { version_id: m.id } });
      toast(`已确认 v${m.version_no}`);
      await loadMinutes();
    } catch (e) { toast(e.message, true); }
  };

  const panel = document.getElementById("minutes-edit");
  document.getElementById("btn-edit-minutes").onclick = () => {
    panel.classList.toggle("hidden");
    if (!panel.classList.contains("hidden")) {
      // 基版本记在 data-base 上:提交时若已不是最新版,服务端会拒绝而非覆盖
      panel.dataset.base = m.id;
      document.getElementById("minutes-json").value =
        JSON.stringify(m.content, null, 2);
    }
  };
}

document.getElementById("btn-minutes-save").addEventListener("click", async () => {
  const out = document.getElementById("minutes-edit-result");
  let parsed;
  try {
    parsed = JSON.parse(document.getElementById("minutes-json").value);
  } catch (e) { out.innerHTML = `<div class="err">JSON 不合法:${esc(e.message)}</div>`; return; }
  const base = Number(document.getElementById("minutes-edit").dataset.base);
  out.innerHTML = '<span class="hint">提交中…</span>';
  try {
    const v = await api(`/meetings/${state.meetingId}/minutes/versions`, {
      method: "POST", json: { base_version_id: base, content: parsed },
    });
    out.innerHTML = `<div class="hint">已追加 v${v.version_no}(${esc(v.source_type)},`
      + `${esc(v.status)})。AI 原版与既往版本均未改写;这一版需要重新确认。</div>`;
    await loadMinutes();
  } catch (e) {
    out.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
});

const PRECISION_LABEL = {
  segment_level_from_slice: "切片范围,非精确语句定位",
};

function fmtTime(a, b, precision) {
  if (a === null || a === undefined) return "";
  const range = `[${Math.floor(a / 1000)}s–${Math.floor((b || 0) / 1000)}s]`;
  return precision
    ? `${range} <span class="sub">(精度:${esc(PRECISION_LABEL[precision] || precision)})</span>`
    : range;
}

/* ---------- 证据回放 ----------
 * 音频接口要求 Bearer 头,而 <audio>/<video> 发不出自定义头,
 * 也不能把令牌塞进 URL(会进浏览器历史与访问日志)。
 * 因此用带鉴权的 fetch 取字节 → blob URL 播放;时间轴仍按引用的切片范围定位。
 */
let _replayUrl = null;
let _stopAt = null;

function playBtn(c) {
  if (!c.recording_id || c.start_ms === null || c.start_ms === undefined) return "";
  return `<button class="play-seg" data-rec="${c.recording_id}" data-s="${c.start_ms}"`
    + ` data-e="${c.end_ms ?? ""}">▶ 播放本片段</button>`;
}

async function playEvidence(rec, startMs, endMs) {
  const el = document.getElementById("player");
  const label = document.getElementById("replay-label");
  label.textContent = "正在取回该录音(经鉴权接口)…";
  try {
    const resp = await fetch(`/recordings/${rec}/audio`, {
      headers: { Authorization: "Bearer " + state.token },
    });
    if (!resp.ok) {
      throw new Error(`${resp.status} ${(await resp.text()).slice(0, 160)}`);
    }
    const served = resp.headers.get("X-Audio-Source") || "unknown";
    const width = resp.headers.get("X-Audio-Original-Bits") || "?";
    if (_replayUrl) URL.revokeObjectURL(_replayUrl);
    _replayUrl = URL.createObjectURL(await resp.blob());
    _stopAt = endMs ? endMs / 1000 : null;
    el.addEventListener("loadedmetadata", () => {
      el.currentTime = startMs / 1000;
      el.play();
    }, { once: true });
    el.src = _replayUrl;
    label.innerHTML = `SEG 切片 ${Math.floor(startMs / 1000)}s–`
      + `${endMs ? Math.floor(endMs / 1000) + "s" : "?"} · `
      + `<span class="sub">切片范围,非精确语句定位</span> · 播放来源:${esc(served)}`
      + ` (原件位宽 ${esc(width)};不兼容时服务端另存 16-bit 副本,原件未改)`;
  } catch (e) {
    _stopAt = null;
    label.textContent = "回放失败:" + e.message;
  }
}

// 只播被引用的那一段:到末尾自动停,不越界播放整条会议录音
document.getElementById("player").addEventListener("timeupdate", (ev) => {
  const el = ev.target;
  if (_stopAt !== null && el.currentTime >= _stopAt) {
    el.pause();
    _stopAt = null;
  }
});

document.getElementById("view-detail").addEventListener("click", (ev) => {
  const b = ev.target.closest("button.play-seg");
  if (!b) return;
  playEvidence(Number(b.dataset.rec), Number(b.dataset.s),
    b.dataset.e ? Number(b.dataset.e) : null);
});

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
        ${fmtTime(c.start_ms, c.end_ms, c.timestamp_precision)} ${playBtn(c)}
        ${esc(c.speaker || "未知")}:${esc(c.text)}
        <span class="sub">相似度 ${c.similarity}</span></div>`).join("")}</div>`;
  } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
});

