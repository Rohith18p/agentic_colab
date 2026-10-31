// Dashboard: plain JS, no build step. State comes from /api/state; live
// updates arrive as server-sent events from /api/stream and trigger re-renders.
"use strict";

const STATUSES = ["open", "claimed", "in_progress", "needs_input", "paused", "completed", "failed"];
const $ = (sel) => document.querySelector(sel);

let state = null;              // last /api/state snapshot
let thread = { channel: "general" };  // {channel} or {task_id}
let messages = [];
let replyTo = null;
let editingCharter = false;
let serverOffsetMs = 0;        // server clock minus browser clock
let bootId = null;             // server process id; a new one means the server restarted

// ---------------------------------------------------------------- helpers

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

function setName(name) {
  document.cookie = `display_name=${encodeURIComponent(name)}; path=/; max-age=31536000; SameSite=Lax`;
  $("#me").textContent = name;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.message || (data.detail && JSON.stringify(data.detail)) || res.statusText;
    alert(msg);
    throw new Error(msg);
  }
  return data;
}

function ago(isoTime) {
  if (!isoTime) return "never";
  const s = Math.max(0, Math.round((Date.now() + serverOffsetMs - Date.parse(isoTime)) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function time(isoTime) {
  return isoTime ? new Date(isoTime).toLocaleTimeString() : "";
}

function taskTitle(id) {
  const t = state && state.tasks.find((t) => t.id === id);
  return t ? `#${t.id} ${t.title}` : `#${id}`;
}

function threadKey(t) {
  return t.task_id ? `task:${t.task_id}` : `channel:${t.channel}`;
}

function inThread(m) {
  return thread.task_id ? m.task_id === thread.task_id : (!m.task_id && m.channel === thread.channel);
}

// ---------------------------------------------------------------- rendering

function renderAll() {
  if (!state) return;
  renderNeedsHuman();
  renderAgents();
  renderBoard();
  renderThreads();
  renderThreadHeader();
  if (!editingCharter) renderCharter();
}

function renderNeedsHuman() {
  const qs = state.needs_human;
  $("#nh-count").textContent = qs.length || "";
  if (!qs.length) {
    $("#nh-list").innerHTML = '<p class="muted">No open questions.</p>';
    return;
  }
  // Keep half-typed answers across re-renders.
  const drafts = {};
  document.querySelectorAll("#nh-list input[data-q]").forEach((i) => (drafts[i.dataset.q] = i.value));
  $("#nh-list").innerHTML = qs.map((q) => `
    <div class="question-item">
      <div class="meta"><b>${esc(q.author_name)}</b> asked ${ago(q.created_at)}
        ${q.task_id ? `in <button class="link" data-open-task="${q.task_id}">${esc(taskTitle(q.task_id))}</button>` : "in #general"}</div>
      <div class="body">${esc(q.body)}</div>
      <form data-answer="${q.id}">
        <input data-q="${q.id}" placeholder="Answer…" required value="${esc(drafts[q.id] || "")}">
        <button type="submit">Answer</button>
        <button type="button" data-resolve="${q.id}">Dismiss</button>
      </form>
    </div>`).join("");
}

function renderAgents() {
  const rows = state.agents;
  if (!rows.length) {
    $("#agent-rows").innerHTML = '<tr><td colspan="7" class="muted">No agents yet. Start a runner to see one here.</td></tr>';
    return;
  }
  $("#agent-rows").innerHTML = rows.map((a) => `
    <tr>
      <td><b>${esc(a.name)}</b><br><small class="muted">${esc((a.skills || []).join(", "))}</small></td>
      <td>${esc(a.owner_name || "—")}<br><small class="muted">${esc(a.machine)}</small></td>
      <td class="status status-${a.status}">${esc(a.status)}${a.paused && a.status !== "paused" ? " (paused)" : ""}
        ${a.paused_by ? `<br><small class="muted">by ${esc(a.paused_by)}</small>` : ""}</td>
      <td>${a.current_task_id ? `<button class="link" data-open-task="${a.current_task_id}">${esc(taskTitle(a.current_task_id))}</button>` : "—"}</td>
      <td>${esc(a.approach || "")}</td>
      <td title="${esc(a.last_heartbeat || "")}">${ago(a.last_heartbeat)}</td>
      <td>${a.paused
        ? `<button data-resume="${esc(a.name)}">Resume</button>`
        : `<button data-pause="${esc(a.name)}">Pause</button>`}</td>
    </tr>`).join("");
}

function renderBoard() {
  $("#board").innerHTML = STATUSES.map((s) => {
    const cards = state.tasks.filter((t) => t.status === s).map((t) => `
      <div class="card ${thread.task_id === t.id ? "selected" : ""}" data-open-task="${t.id}">
        <b>#${t.id}</b> ${esc(t.title)}
        ${t.claimed_by ? `<small>${esc(t.claimed_by)}</small>` : ""}
        ${t.parent_id ? `<small>sub of #${t.parent_id}</small>` : ""}
      </div>`).join("");
    const n = state.tasks.filter((t) => t.status === s).length;
    return `<div class="column"><h3>${s.replace("_", " ")} (${n})</h3>${cards}</div>`;
  }).join("");
}

function renderThreads() {
  const channels = state.settings.channels || ["general", "decisions"];
  const items = channels.map((channel) => ({ channel }))
    .concat([...state.tasks].reverse().map((t) => ({ task_id: t.id })));
  const current = threadKey(thread);
  $("#threads").innerHTML = items.map((t) => {
    const label = t.task_id ? esc(taskTitle(t.task_id)) : `# ${t.channel}`;
    const attrs = t.task_id ? `data-open-task="${t.task_id}"` : `data-channel="${t.channel}"`;
    return `<button ${attrs} class="${threadKey(t) === current ? "active" : ""}" title="${label}">${label}</button>`;
  }).join("");
}

function renderThreadHeader() {
  if (!thread.task_id) {
    $("#thread-header").innerHTML = `<div class="muted"># ${esc(thread.channel)}</div>`;
    return;
  }
  const t = state.tasks.find((t) => t.id === thread.task_id);
  if (!t) return;
  const links = (t.output_links || []).map((l) => `<li>${esc(l)}</li>`).join("");
  $("#thread-header").innerHTML = `
    <div class="task-detail">
      <b>#${t.id} ${esc(t.title)}</b> — <span class="status status-${t.status}">${esc(t.status)}</span>
      ${t.claimed_by ? ` · held by <b>${esc(t.claimed_by)}</b>` : ""}
      ${t.lease_expires_at ? ` · lease until ${time(t.lease_expires_at)}` : ""}
      ${t.description ? `<div class="body">${esc(t.description)}</div>` : ""}
      ${t.approach_note ? `<div><small class="muted">Approach:</small> ${esc(t.approach_note)}</div>` : ""}
      ${links ? `<div><small class="muted">Outputs:</small><ul>${links}</ul></div>` : ""}
      <div class="row">
        <small class="muted">Created by ${esc(t.created_by)}${t.parent_id ? ` · subtask of #${t.parent_id}` : ""}</small>
        <span class="spacer"></span>
        ${t.claimed_by && !["completed", "failed"].includes(t.status) ? `<button data-release="${t.id}">Return to open</button>` : ""}
        <select data-status-for="${t.id}">${STATUSES.map((s) => `<option ${s === t.status ? "selected" : ""}>${s}</option>`).join("")}</select>
        <button data-set-status="${t.id}">Set status</button>
      </div>
    </div>`;
}

function renderMessages() {
  const list = $("#message-list");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  if (!messages.length) {
    list.innerHTML = '<p class="muted">No messages yet.</p>';
    return;
  }
  list.innerHTML = messages.map((m) => `
    <div class="msg ${m.author_type}" id="msg-${m.id}">
      <div class="meta"><b>${esc(m.author_name)}</b> (${m.author_type})
        <span class="tag ${m.type}">${m.type}</span>
        ${m.reply_to ? `<span class="tag">reply to #${m.reply_to}</span>` : ""}
        ${m.needs_human && !m.resolved_at ? '<span class="tag question">needs human</span>' : ""}
        · ${time(m.created_at)} · #${m.id}
        <button class="link" data-reply="${m.id}" data-reply-author="${esc(m.author_name)}">reply</button></div>
      <div class="body">${esc(m.body)}</div>
    </div>`).join("");
  if (atBottom) list.scrollTop = list.scrollHeight;
}

function renderCharter() {
  const c = state.charter;
  $("#charter-version").textContent = `v${c.version} · edited by ${c.edited_by} ${ago(c.created_at)}`;
  const fields = [["objective", "Objective"], ["constraints", "Constraints"], ["libraries", "Libraries"],
    ["compute", "Compute available"], ["notes", "Notes"]];
  $("#charter-view").innerHTML = fields.map(([k, label]) => `
    <div class="charter-field"><b>${label}</b><div>${c[k] ? esc(c[k]) : '<span class="muted">—</span>'}</div></div>`).join("");
}

// ---------------------------------------------------------------- data

async function refreshState() {
  const data = await (await fetch("/api/state")).json();
  serverOffsetMs = Date.parse(data.server_time) - Date.now();
  state = data;
  const end = data.settings.swarm_end;
  $("#ends").textContent = !end ? "" : (Date.parse(end) <= Date.now() + serverOffsetMs
    ? "swarm ended " : "swarm ends ") + new Date(end).toLocaleString();
  renderAll();
}

async function loadMessages() {
  const q = thread.task_id ? `task_id=${thread.task_id}` : `channel=${thread.channel}`;
  messages = await (await fetch(`/api/messages?${q}&limit=200`)).json();
  renderMessages();
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refreshState, 250);
}

function openThread(t) {
  thread = t;
  setReply(null);
  renderThreads();
  renderThreadHeader();
  renderBoard();
  loadMessages();
}

function setReply(id, author) {
  replyTo = id;
  const el = $("#reply-indicator");
  el.hidden = !id;
  el.innerHTML = id ? `Replying to #${id} (${esc(author)}) <button class="link" id="cancel-reply">cancel</button>` : "";
}

function connectStream() {
  const es = new EventSource("/api/stream");
  es.onopen = () => { $("#live").textContent = "live"; $("#live").className = "pill"; };
  es.onerror = () => { $("#live").textContent = "reconnecting"; $("#live").className = "pill off"; };
  // After a server restart (e.g. a hot reload), reload the page to pick up new JS/CSS/HTML.
  es.addEventListener("hello", (e) => {
    const id = JSON.parse(e.data).boot_id;
    if (bootId && id !== bootId) location.reload();
    bootId = id;
  });
  es.addEventListener("workspace", (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "message.posted" && inThread(ev.payload) && !messages.some((m) => m.id === ev.payload.id)) {
      messages.push(ev.payload);
      renderMessages();
    }
    scheduleRefresh();
  });
}

// ---------------------------------------------------------------- events

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-open-task],[data-channel],[data-pause],[data-resume],[data-reply],[data-release],[data-set-status],[data-resolve],#cancel-reply");
  if (!el) return;
  const d = el.dataset;
  if (el.id === "cancel-reply") return setReply(null);
  if (d.openTask) {
    openThread({ task_id: Number(d.openTask) });
    $("#messages").scrollIntoView({ behavior: "smooth" });
  } else if (d.channel) openThread({ channel: d.channel });
  else if (d.pause) {
    const reason = prompt(`Pause ${d.pause}? Optional reason:`, "");
    if (reason !== null) await api("POST", `/api/agents/${encodeURIComponent(d.pause)}/pause`, { reason });
  } else if (d.resume) await api("POST", `/api/agents/${encodeURIComponent(d.resume)}/resume`);
  else if (d.reply) { setReply(Number(d.reply), d.replyAuthor); $("#message-form textarea").focus(); }
  else if (d.release) await api("POST", `/api/tasks/${d.release}/release`);
  else if (d.setStatus) {
    const status = document.querySelector(`[data-status-for="${d.setStatus}"]`).value;
    await api("PATCH", `/api/tasks/${d.setStatus}`, { status });
  } else if (d.resolve) await api("POST", `/api/questions/${d.resolve}/resolve`);
  scheduleRefresh();
});

document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (form.dataset.answer) {
    e.preventDefault();
    const input = form.querySelector("input");
    await api("POST", "/api/messages", { body: input.value, type: "human_input", reply_to: Number(form.dataset.answer) });
    input.value = "";
    scheduleRefresh();
  }
});

$("#message-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const payload = { body: f.elements.body.value, type: f.elements.type.value, reply_to: replyTo };
  if (thread.task_id) payload.task_id = thread.task_id; else payload.channel = thread.channel;
  await api("POST", "/api/messages", payload);
  f.body.value = "";
  setReply(null);
});

$("#message-form textarea").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#message-form").requestSubmit();
});

$("#task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const payload = { title: f.elements.title.value, description: f.elements.description.value };
  if (f.parent_id.value) payload.parent_id = Number(f.parent_id.value);
  const task = await api("POST", "/api/tasks", payload);
  f.reset();
  await refreshState();
  openThread({ task_id: task.id });
});

$("#charter-edit").addEventListener("click", () => {
  const f = $("#charter-form");
  for (const k of ["objective", "constraints", "libraries", "compute", "notes"]) f[k].value = state.charter[k];
  f.dataset.baseVersion = state.charter.version;
  editingCharter = true;
  f.hidden = false;
  $("#charter-view").hidden = true;
});

function closeCharterEditor() {
  editingCharter = false;
  $("#charter-form").hidden = true;
  $("#charter-view").hidden = false;
  renderCharter();
}

$("#charter-cancel").addEventListener("click", closeCharterEditor);

$("#charter-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const payload = { base_version: Number(f.dataset.baseVersion) };
  for (const k of ["objective", "constraints", "libraries", "compute", "notes"]) payload[k] = f[k].value;
  state.charter = await api("PUT", "/api/charter", payload);
  closeCharterEditor();
  if (!$("#charter-history").hidden) showHistory();
});

async function showHistory() {
  const versions = await (await fetch("/api/charter/history")).json();
  $("#charter-history").innerHTML = versions.map((v) => `
    <details class="history-item"><summary>v${v.version} · ${esc(v.edited_by)} · ${new Date(v.created_at).toLocaleString()}</summary>
      ${["objective", "constraints", "libraries", "compute", "notes"].map((k) =>
        `<div class="charter-field"><b>${k}</b><div>${esc(v[k]) || '<span class="muted">—</span>'}</div></div>`).join("")}
    </details>`).join("");
}

$("#charter-history-btn").addEventListener("click", async () => {
  const el = $("#charter-history");
  el.hidden = !el.hidden;
  if (!el.hidden) await showHistory();
});

$("#change-name").addEventListener("click", () => {
  $("#name-input").value = getCookie("display_name");
  $("#name-dialog").showModal();
});

// The dialog keeps coming back until there is a name (e.g. after Escape).
$("#name-dialog").addEventListener("close", () => {
  if (!getCookie("display_name")) $("#name-dialog").showModal();
});

$("#name-form").addEventListener("submit", () => {
  const name = $("#name-input").value.trim();
  if (name) setName(name);
});

// ---------------------------------------------------------------- boot

(async function boot() {
  const name = getCookie("display_name");
  if (name) setName(name); else $("#name-dialog").showModal();
  await refreshState();
  await loadMessages();
  connectStream();
  setInterval(refreshState, 5000);  // keeps "x s ago" and offline status current
})();
