// Dashboard: plain JS, no build step. State comes from /api/state, messages from /api/messages,
// the Compute tab from /api/compute (or the queue itself). Live updates arrive as server-sent
// events from /api/stream. Every render goes through setHTML, which skips unchanged sections, so
// refreshes don't reset scroll positions, open posts or half-typed answers.
"use strict";

const $ = (sel) => document.querySelector(sel);
const TABS = ["overview", "messages", "tasks", "compute", "activity", "ask", "charter"];
const STATUSES = ["open", "claimed", "in_progress", "needs_input", "paused", "completed", "failed"];
const OWN_TAB_CHANNELS = ["ask", "activity"];   // channels with their own tab, kept out of Channels
const QUIET_CHANNELS = ["system"];               // not counted in the Channels tab's unread badge
const CHANNEL_DESC = {
  general: "coordination, steering and announcements",
  decisions: "proposals and decisions that bind the swarm",
  findings: "results, with their reviews threaded underneath",
  literature: "papers and mechanism hypotheses from the scouts",
  compute: "GPUs, endpoints, bookings and the job queue",
  system: "automatic notices from the board",
};
const TASK_FILTERS = {
  active: { label: "Active", match: (t) => ["claimed", "in_progress", "needs_input", "paused"].includes(t.status) },
  open: { label: "Open", match: (t) => t.status === "open" },
  done: { label: "Done", match: (t) => t.status === "completed" },
  failed: { label: "Failed", match: (t) => t.status === "failed" },
  all: { label: "All", match: () => true },
};
const AGENT_ORDER = ["working", "waiting_on_human", "blocked", "paused", "idle", "offline"];

let state = null;               // last /api/state snapshot
let serverOffsetMs = 0;         // server clock minus browser clock
let bootId = null;              // server process id; a new one means the server restarted
let tab = "overview";
let channel = "general";        // open channel in the Channels tab
let taskSel = null;             // selected task id in the Tasks tab
let taskFilter = "active";
let compute = null;             // {via, url, status, at} or {error}
let editingCharter = false;
const chan = {};                // channel -> {list, done}  (cached messages, oldest first)
const taskMsgs = {};            // task id -> messages, oldest first
const expanded = new Set();     // message ids whose full body is shown
const openThreads = new Set();  // root ids whose earlier replies are shown
const reply = { messages: null, tasks: null, ask: null };  // {id, author} being replied to

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

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { return null; }
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

const getJSON = async (path) => (await fetch(path)).json();

// Replaces an element's content only when it changed, then fills in its relative times.
function setHTML(el, html) {
  if (typeof el === "string") el = $(el);
  if (!el || el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  tickAgo(el);
  return true;
}

function now() { return Date.now() + serverOffsetMs; }

function span(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function agoText(iso) { return iso ? `${span((now() - Date.parse(iso)) / 1000)} ago` : "never"; }

// Relative times are rendered as <span data-ago="iso"></span> and filled here, so that
// the HTML itself doesn't change every second (setHTML would otherwise re-render).
const ago = (iso) => `<span data-ago="${esc(iso || "")}" title="${iso ? esc(new Date(iso).toLocaleString()) : ""}"></span>`;
function tickAgo(root = document) {
  root.querySelectorAll("[data-ago]").forEach((el) => (el.textContent = agoText(el.dataset.ago)));
  root.querySelectorAll("[data-since]").forEach((el) => (el.textContent = span((now() - Date.parse(el.dataset.since)) / 1000)));
}

function colorFor(name) {
  let h = 0;
  for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 55% 45%)`;
}

const avatar = (name) => `<span class="avatar" style="background:${colorFor(name)}">${esc(String(name)[0] || "?").toUpperCase()}</span>`;

function task(id) { return state && state.tasks.find((t) => t.id === id); }
function taskLink(id, withTitle = true) {
  const t = task(id);
  return `<button class="link" data-open-task="${id}" title="${esc(t ? t.title : "")}">#${id}${withTitle && t ? " " + esc(t.title) : ""}</button>`;
}

// ---------------------------------------------------------------- light markdown

// Inline pieces, matched on raw text so escaping can't break them.
const TOKEN = /(`[^`\n]+`)|(https?:\/\/[^\s<>"'`]+[^\s<>"'`.,;:!?)\]])|(\*\*[^*\n]+\*\*)|(@[A-Za-z0-9][\w.-]*[A-Za-z0-9])|(#\d{2,5}\b)|(\b[Tt]ask #?\d{1,4}\b)/g;

function inline(text) {
  let out = "";
  let last = 0;
  for (const m of text.matchAll(TOKEN)) {
    out += esc(text.slice(last, m.index));
    const [tok, code, url, bold, mention, ref, taskRef] = m;
    if (code) out += `<code>${esc(code.slice(1, -1))}</code>`;
    else if (url) out += `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`;
    else if (bold) out += `<b>${esc(bold.slice(2, -2))}</b>`;
    else if (mention) out += `<span class="mention">${esc(mention)}</span>`;
    else if (ref) out += `<a href="#" class="ref" data-msg="${ref.slice(1)}">${esc(ref)}</a>`;
    else if (taskRef) out += `<button class="link" data-open-task="${taskRef.match(/\d+/)[0]}">${esc(taskRef)}</button>`;
    else out += esc(tok);
    last = m.index + tok.length;
  }
  return out + esc(text.slice(last));
}

// "Label: value" lines (Value:, Setup:, Status: …) get a bold label; "# Heading" lines are bold.
function line(l) {
  const h = l.match(/^#{1,4}\s+(.*)$/);
  if (h) return `<span class="head">${inline(h[1])}</span>`;
  const lab = l.match(/^(\s*[-*]?\s*)([A-Z][A-Za-z0-9 /()+&'-]{1,32}):(\s|$)(.*)$/);
  if (lab) return `${esc(lab[1])}<span class="lbl">${esc(lab[2])}:</span>${lab[3]}${inline(lab[4])}`;
  return inline(l);
}

function md(text) {
  return String(text ?? "").split("```").map((part, i) => i % 2
    ? `<pre>${esc(part.replace(/^[\w-]*\n/, "").replace(/\n$/, ""))}</pre>`
    : part.split("\n").map(line).join("\n")).join("");
}

// ---------------------------------------------------------------- messages

const LONG = (m) => m.body.length > 700 || m.body.split("\n").length > 9;

// where: "ch" (channel thread), "task", "ask" — which composer the reply button targets; null hides it.
function msgHtml(m, { where = null, showTask = false } = {}) {
  const long = LONG(m);
  const open = expanded.has(m.id);
  const parentNotShown = m.reply_to ? `<a href="#" class="ref" data-msg="${m.reply_to}">↪ #${m.reply_to}</a>` : "";
  return `
    <div class="msg ${m.author_type}" data-mid="${m.id}">
      <div class="meta">${avatar(m.author_name)}<span class="who">${esc(m.author_name)}</span>
        ${m.author_type === "human" ? '<span class="chip">human</span>' : ""}
        ${m.type !== "status" ? `<span class="tag ${m.type}">${m.type}</span>` : ""}
        ${m.needs_human && !m.resolved_at ? '<span class="tag question">needs human</span>' : ""}
        ${m._orphan ? parentNotShown : ""}
        ${showTask && m.task_id ? taskLink(m.task_id) : ""}
        <span>${ago(m.created_at)}</span>
        <span class="actions">
          <a href="#" class="ref" data-msg="${m.id}" title="Link to this message">#${m.id}</a>
          ${where ? `<button class="link" data-reply="${where}" data-id="${m.id}" data-author="${esc(m.author_name)}">reply</button>` : ""}
        </span>
      </div>
      <div class="body ${long && !open ? "clamp" : ""}">${md(m.body)}</div>
      ${long ? `<button class="link more" data-expand="${m.id}">${open ? "Show less" : "Show more"}</button>` : ""}
    </div>`;
}

// Groups messages into threads: each reply goes under the oldest ancestor that is loaded.
function threads(list) {
  const byId = new Map(list.map((m) => [m.id, m]));
  const groups = new Map();
  for (const m of list) {
    let r = m;
    for (let i = 0; i < 50 && r.reply_to && byId.has(r.reply_to); i++) r = byId.get(r.reply_to);
    if (!groups.has(r.id)) groups.set(r.id, { root: r, replies: [], last: r.id });
    const g = groups.get(r.id);
    if (r !== m) g.replies.push(m);
    g.last = Math.max(g.last, m.id);
  }
  for (const g of groups.values()) {
    g.root._orphan = Boolean(g.root.reply_to);
    g.replies.sort((a, b) => a.id - b.id);
  }
  return [...groups.values()];
}

function threadHtml(g, where) {
  let replies = g.replies;
  let hidden = 0;
  if (replies.length > 3 && !openThreads.has(g.root.id)) {
    hidden = replies.length - 2;
    replies = replies.slice(-2);
  }
  return `<div class="thread" data-root="${g.root.id}">${msgHtml(g.root, { where })}
    ${g.replies.length ? `<div class="replies">
      ${hidden ? `<button class="link show-replies" data-open-thread="${g.root.id}">Show ${hidden} earlier ${hidden === 1 ? "reply" : "replies"}</button>` : ""}
      ${replies.map((m) => msgHtml(m, { where })).join("")}</div>` : ""}
  </div>`;
}

// Keeps the first visible thread where it is on screen while content above it changes.
function keepAnchor(container, render) {
  const items = [...container.querySelectorAll(":scope > [data-root]")];
  const anchor = window.scrollY > 0 && items.find((el) => el.getBoundingClientRect().bottom > 60);
  const top = anchor && anchor.getBoundingClientRect().top;
  const changed = render();
  if (anchor && changed) {
    const again = container.querySelector(`:scope > [data-root="${anchor.dataset.root}"]`);
    if (again) window.scrollBy(0, again.getBoundingClientRect().top - top);
  }
}

// ---------------------------------------------------------------- channel cache and unread

async function loadChannel(ch, limit = 150) {
  const list = await getJSON(`/api/messages?channel=${encodeURIComponent(ch)}&limit=${limit}`);
  chan[ch] = { list, done: list.length < limit };
  if (store(`seen:${ch}`) === null) markSeen(ch);  // first visit: start with nothing unread
}

async function loadOlder(ch) {
  const c = chan[ch];
  if (!c || c.done || !c.list.length) return;
  const older = await getJSON(`/api/messages?channel=${encodeURIComponent(ch)}&before_id=${c.list[0].id}&limit=150`);
  c.done = older.length < 150;
  c.list = older.concat(c.list);
}

function addMessage(m) {
  const list = m.task_id ? taskMsgs[m.task_id] : chan[m.channel] && chan[m.channel].list;
  if (!list || list.some((x) => x.id === m.id)) return false;
  list.push(m);
  return true;
}

function lastId(ch) { const l = chan[ch] && chan[ch].list; return l && l.length ? l[l.length - 1].id : 0; }
function markSeen(ch) { store(`seen:${ch}`, String(lastId(ch))); }
function unread(ch) {
  const seen = Number(store(`seen:${ch}`) || 0);
  const me = getCookie("display_name");
  return ((chan[ch] && chan[ch].list) || []).filter((m) => m.id > seen && m.author_name !== me).length;
}

function channelList() {
  return ((state && state.settings.channels) || ["general", "decisions"]).filter((c) => !OWN_TAB_CHANNELS.includes(c));
}

// ---------------------------------------------------------------- overview

function renderStats() {
  const a = state.agents;
  const n = (s) => a.filter((x) => x.status === s).length;
  const t = state.tasks;
  const active = t.filter(TASK_FILTERS.active.match).length;
  const nh = state.needs_human.length;
  const q = compute && compute.status;
  let queue = '<div class="big">—</div><div class="label">queue</div><div class="sub">loading…</div>';
  if (q) {
    const owners = Object.values(q.owners || {});
    const pend = owners.reduce((s, o) => s + (o.pending || 0), 0);
    const run = owners.reduce((s, o) => s + (o.running || 0), 0);
    const eps = Object.values(q.endpoints || {});
    const free = eps.filter((e) => e.ok !== false && !e.job && !(e.health && e.health.busy)).length;
    queue = `<div class="big">${pend}</div><div class="label">clips queued</div>
      <div class="sub">${run} running · ${free}/${eps.length} endpoints free</div>`;
  } else if (compute && compute.error) {
    queue = '<div class="big">?</div><div class="label">queue</div><div class="sub">unreachable</div>';
  }
  const end = state.settings.swarm_end;
  const left = end ? (Date.parse(end) - now()) / 1000 : null;
  setHTML("#stats", `
    <div class="stat" data-goto="agents"><div class="big">${n("working")}</div><div class="label">agents working</div>
      <div class="sub">${n("idle")} idle · ${n("waiting_on_human") + n("blocked") + n("paused")} waiting · ${n("offline")} offline</div></div>
    <div class="stat ${nh ? "alert" : ""}" data-goto="needs-human"><div class="big">${nh}</div><div class="label">need a human</div>
      <div class="sub">${nh ? "answer below" : "nothing waiting"}</div></div>
    <div class="stat" data-goto="tasks"><div class="big">${active}</div><div class="label">tasks in progress</div>
      <div class="sub">${t.filter(TASK_FILTERS.open.match).length} open · ${t.filter(TASK_FILTERS.done.match).length} done · ${t.filter(TASK_FILTERS.failed.match).length} failed</div></div>
    <div class="stat" data-goto="compute">${queue}</div>
    <div class="stat" data-goto="charter"><div class="big" id="freeze-big">${left === null ? "—" : left <= 0 ? "ended" : span(left)}</div>
      <div class="label">until freeze</div><div class="sub">${end ? new Date(end).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "no end set"}</div></div>`);
}

function renderNeedsHuman() {
  const qs = state.needs_human;
  $("#nh-badge").textContent = qs.length || "";
  $("#nh-count").textContent = qs.length || "";
  $("#needs-human").hidden = !qs.length;
  const drafts = {};
  document.querySelectorAll("#nh-list input[data-q]").forEach((i) => (drafts[i.dataset.q] = i.value));
  const changed = setHTML("#nh-list", qs.map((q) => `
    <div class="msg" data-mid="${q.id}">
      <div class="meta">${avatar(q.author_name)}<span class="who">${esc(q.author_name)}</span> asked ${ago(q.created_at)}
        ${q.task_id ? `in ${taskLink(q.task_id)}` : `in #${esc(q.channel || "general")}`}
        <span class="actions"><a href="#" class="ref" data-msg="${q.id}">#${q.id}</a></span></div>
      <div class="body">${md(q.body)}</div>
      <form class="row" data-answer="${q.id}" style="margin-top:6px">
        <input data-q="${q.id}" placeholder="Answer…" required style="flex:1">
        <button type="submit" class="primary">Answer</button>
        <button type="button" data-resolve="${q.id}">Dismiss</button>
      </form>
    </div>`).join(""));
  if (changed) document.querySelectorAll("#nh-list input[data-q]").forEach((i) => (i.value = drafts[i.dataset.q] || ""));
}

function model(a) {
  const s = (a.skills || []).find((x) => x.startsWith("model:"));
  return s ? s.slice(6) : "";
}

function renderAgents() {
  const showOffline = $("#show-offline").checked;
  const all = [...state.agents].sort((x, y) =>
    (AGENT_ORDER.indexOf(x.status) - AGENT_ORDER.indexOf(y.status)) || x.name.localeCompare(y.name));
  const offline = all.filter((a) => a.status === "offline").length;
  $("#offline-n").textContent = offline ? `(${offline})` : "";
  const rows = all.filter((a) => showOffline || a.status !== "offline");
  setHTML("#agent-rows", rows.length ? rows.map((a) => {
    const gpuText = String(a.machine || "").split(" · ")[1] || "";
    const gpu = (gpuText.match(/\b(RTX \w*\d{4}\w*|[A-Z]{1,2}\d{2,3}\w*|GB\d+)\b/) || [])[1] || "";
    return `
    <tr class="${a.status === "offline" ? "offline" : ""}">
      <td>${avatar(a.name)} <b>${esc(a.name)}</b>
        <div class="sub-line" title="${esc(a.machine || "")}">${model(a) ? `<span class="chip">${esc(model(a))}</span> ` : ""}${esc(a.owner_name || "")}${gpu ? " · " + esc(gpu) : ""}</div></td>
      <td><span class="st st-${a.status}">${esc(a.status.replace(/_/g, " "))}</span>
        ${a.paused_by ? `<div class="sub-line">paused by ${esc(a.paused_by)}</div>` : ""}</td>
      <td>${a.current_task_id ? taskLink(a.current_task_id) : ""}
        ${a.approach ? `<div class="approach" title="${esc(a.approach)}">${esc(a.approach)}</div>` : ""}</td>
      <td class="sub-line">${ago(a.last_heartbeat)}</td>
      <td>${a.paused ? `<button class="tiny" data-resume="${esc(a.name)}">Resume</button>`
        : a.status !== "offline" ? `<button class="tiny" data-pause="${esc(a.name)}">Pause</button>` : ""}
        ${a.status === "offline" ? `<button class="tiny" data-remove-agent="${esc(a.name)}" title="Remove this offline agent from the list">Remove</button>` : ""}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="5" class="muted">No agents online. Start a runner to see one here.</td></tr>');
}

function renderFindings() {
  const c = chan.findings;
  if (!c) return;
  const gs = threads(c.list).filter((g) => g.root.type === "finding").sort((a, b) => b.root.id - a.root.id).slice(0, 10);
  setHTML("#findings-list", gs.length ? gs.map((g) => {
    const first = g.root.body.split("\n")[0].replace(/^\**\s*FINDING\s*/i, "");
    const status = (g.root.body.match(/Status:\s*\**\s*([\w-]+)/i) || [])[1];
    const revs = g.replies.filter((r) => r.type === "review");
    return `<div class="finding-card" data-msg="${g.root.id}">
      <div class="meta">${avatar(g.root.author_name)}<span class="who">${esc(g.root.author_name)}</span>
        ${status ? `<span class="chip">${esc(status)}</span>` : ""}<span>${ago(g.root.created_at)}</span>
        <span class="actions">#${g.root.id}</span></div>
      <div class="headline">${inline(first.length > 260 ? first.slice(0, 260) + "…" : first)}</div>
      ${g.replies.length ? `<div class="reviews">${revs.length} review${revs.length === 1 ? "" : "s"} · ${g.replies.length} repl${g.replies.length === 1 ? "y" : "ies"} · latest by ${esc(g.replies[g.replies.length - 1].author_name)}</div>` : ""}
    </div>`;
  }).join("") : '<p class="muted">No findings yet.</p>');
}

// ---------------------------------------------------------------- channels

function renderChannelNav() {
  const total = channelList().filter((c) => !QUIET_CHANNELS.includes(c)).reduce((s, c) => s + unread(c), 0);
  $("#unread-badge").textContent = total || "";
  setHTML("#channels", channelList().map((c) => {
    const n = c === channel && tab === "messages" ? 0 : unread(c);
    return `<button data-channel="${c}" class="${c === channel ? "active" : ""}"><span># ${esc(c)}</span>${n ? `<span class="count soft">${n > 99 ? "99+" : n}</span>` : ""}</button>`;
  }).join(""));
}

function renderChannel() {
  $("#channel-title").textContent = `# ${channel}`;
  $("#channel-desc").textContent = CHANNEL_DESC[channel] || "";
  const c = chan[channel];
  const el = $("#message-list");
  if (!c) return setHTML(el, '<p class="muted">Loading…</p>');
  const q = $("#channel-search").value.trim().toLowerCase();
  let gs = threads(c.list).sort((a, b) => b.last - a.last);
  if (q) gs = gs.filter((g) => [g.root, ...g.replies].some((m) => (m.body + " " + m.author_name).toLowerCase().includes(q)));
  keepAnchor(el, () => setHTML(el, (gs.length ? gs.map((g) => threadHtml(g, "messages")).join("") : '<p class="muted">No messages yet.</p>')
    + (c.done ? "" : '<button class="load-older" data-older="channel">Load older messages</button>')));
  if (tab === "messages" && !document.hidden) markSeen(channel);
  renderChannelNav();
}

async function openChannel(ch) {
  channel = ch;
  setReply("messages", null);
  if (!chan[ch]) await loadChannel(ch);
  showTab("messages");
  renderChannel();
}

// ---------------------------------------------------------------- tasks

function renderTaskFilters() {
  setHTML("#task-filters", Object.entries(TASK_FILTERS).map(([k, f]) =>
    `<button data-task-filter="${k}" class="${k === taskFilter ? "active" : ""}">${f.label} ${state.tasks.filter(f.match).length}</button>`).join(""));
}

function renderTaskList() {
  renderTaskFilters();
  const q = $("#task-search").value.trim().toLowerCase();
  let ts = state.tasks.filter(TASK_FILTERS[taskFilter].match);
  if (q) ts = ts.filter((t) => `#${t.id} ${t.title} ${t.description} ${t.claimed_by || ""}`.toLowerCase().includes(q));
  ts.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const row = (t) => `
    <div class="task-row ${t.id === taskSel ? "selected" : ""}" data-select-task="${t.id}">
      <span class="tid">#${t.id}</span><span class="ttl">${esc(t.title)}</span>
      <div class="sub-line"><span class="st st-${t.status}">${t.status.replace(/_/g, " ")}</span>
        ${t.claimed_by ? `<span>${esc(t.claimed_by)}</span>` : ""}<span>${ago(t.updated_at)}</span>
        ${t.parent_id ? `<span>sub of #${t.parent_id}</span>` : ""}</div>
    </div>`;
  let html;
  if (taskFilter === "all" && !q) {
    html = STATUSES.map((s) => {
      const g = ts.filter((t) => t.status === s);
      return g.length ? `<div class="group-h">${s.replace(/_/g, " ")} · ${g.length}</div>${g.map(row).join("")}` : "";
    }).join("");
  } else html = ts.map(row).join("");
  setHTML("#task-list", html || '<p class="muted">No tasks here.</p>');
}

function renderTaskDetail() {
  const t = taskSel && task(taskSel);
  $("#task-message-form").hidden = !t;
  if (!t) return setHTML("#task-detail", '<p class="muted">Pick a task on the left.</p>');
  const kids = state.tasks.filter((k) => k.parent_id === t.id);
  const links = (t.output_links || []).map((l) => `<li>${inline(l)}</li>`).join("");
  const desc = { id: `d${t.id}`, body: t.description || "" };
  const long = LONG(desc);
  setHTML("#task-detail", `
    <div class="task-head">
      <h2>#${t.id} ${esc(t.title)}</h2>
      <div class="facts">
        <span><span class="st st-${t.status}">${t.status.replace(/_/g, " ")}</span></span>
        ${t.claimed_by ? `<span>held by <b>${esc(t.claimed_by)}</b></span>` : ""}
        ${t.lease_expires_at && t.claimed_by ? `<span>lease until ${esc(new Date(t.lease_expires_at).toLocaleTimeString())}</span>` : ""}
        <span>created by ${esc(t.created_by)} ${ago(t.created_at)}</span>
        <span>updated ${ago(t.updated_at)}</span>
        ${t.parent_id ? `<span>subtask of ${taskLink(t.parent_id, false)}</span>` : ""}
        ${kids.length ? `<span>subtasks ${kids.map((k) => taskLink(k.id, false)).join(" ")}</span>` : ""}
      </div>
    </div>
    ${t.description ? `<div class="task-desc"><div class="body ${long && !expanded.has(desc.id) ? "clamp" : ""}" data-body="${desc.id}">${md(t.description)}</div>
      ${long ? `<button class="link more" data-expand="${desc.id}">${expanded.has(desc.id) ? "Show less" : "Show more"}</button>` : ""}</div>` : ""}
    ${t.approach_note ? `<div class="body"><span class="lbl">Approach:</span> ${inline(t.approach_note)}</div>` : ""}
    ${links ? `<div><span class="lbl">Outputs:</span><ul>${links}</ul></div>` : ""}
    <div class="task-actions">
      <span class="spacer"></span>
      ${t.claimed_by && !["completed", "failed"].includes(t.status) ? `<button data-release="${t.id}">Return to open</button>` : ""}
      <select data-status-for="${t.id}">${STATUSES.map((s) => `<option ${s === t.status ? "selected" : ""}>${s}</option>`).join("")}</select>
      <button data-set-status="${t.id}">Set status</button>
    </div>`);
}

function renderTaskThread() {
  const list = taskSel && taskMsgs[taskSel];
  if (!taskSel) return setHTML("#task-thread", "");
  if (!list) return setHTML("#task-thread", '<p class="muted">Loading…</p>');
  const gs = threads(list).sort((a, b) => a.root.id - b.root.id);
  setHTML("#task-thread", gs.length ? gs.map((g) => threadHtml(g, "tasks")).join("") : '<p class="muted">No messages in this task yet.</p>');
}

async function openTask(id) {
  taskSel = id;
  setReply("tasks", null);
  const t = task(id);
  if (t && !TASK_FILTERS[taskFilter].match(t)) taskFilter = "all";
  showTab("tasks");
  renderTaskList();
  renderTaskDetail();
  renderTaskThread();
  if (!taskMsgs[id]) {
    taskMsgs[id] = await getJSON(`/api/messages?task_id=${id}&limit=500`);
    if (taskSel === id) renderTaskThread();
  }
}

// ---------------------------------------------------------------- compute

async function refreshCompute() {
  let r = null;
  try { r = await getJSON("/api/compute"); } catch { r = null; }
  if (r && r.ok) compute = { via: "board", url: r.url, status: r.status, at: new Date().toISOString() };
  else {
    // The board's host may not reach the queue's machine; this browser often can.
    const url = (r && r.url) || "http://board.tail0c526a.ts.net:8030";
    try {
      const ctl = new AbortController();
      setTimeout(() => ctl.abort(), 4000);
      const status = await (await fetch(`${url}/status`, { signal: ctl.signal })).json();
      compute = { via: "your browser", url, status, at: new Date().toISOString() };
    } catch (e) {
      compute = { url, error: `board: ${(r && r.error) || "no answer"}; browser: ${e.message || e}` };
    }
  }
  renderCompute();
  if (state) renderStats();
}

function renderCompute() {
  if (state) setHTML("#charter-compute", state.charter.compute ? md(state.charter.compute) : '<span class="muted">—</span>');
  const err = $("#compute-error");
  if (!compute) return;
  err.hidden = !compute.error;
  if (compute.error) {
    err.textContent = `Can't reach the job queue at ${compute.url} (${compute.error}). It runs in tmux session cosmos-queue on the laptop.`;
    $("#compute-meta").textContent = "";
    return;
  }
  const s = compute.status;
  $("#compute-meta").innerHTML = `queue ${esc(compute.url)} · via ${compute.via} · ${ago(compute.at)}`;
  tickAgo($("#compute-meta"));
  setHTML("#endpoints", Object.entries(s.endpoints || {}).map(([name, e]) => {
    const h = e.health || {};
    const st = e.ok === false ? "down" : e.job ? "busy" : h.busy ? "busy" : "free";
    const what = e.ok === false ? "not answering"
      : e.job ? `<b>${esc(e.job_owner || "queue")}</b> · ${esc(e.job_name || e.job)}${e.job_started ? ` · <span data-since="${esc(e.job_started)}"></span>` : ""}`
      : h.busy ? "busy with a direct (non-queue) request" : "idle";
    return `<div class="endpoint ${st}">
      <div class="row"><span class="name">${esc(name)}</span><span class="spacer"></span><span class="st st-${st}">${st}</span></div>
      <div class="job">${what}${h.waiting ? ` · ${h.waiting} waiting` : ""}</div>
      <div class="nums"><span>queue done ${e.done}</span><span>failed ${e.failed}</span>${h.served != null ? `<span>served ${h.served}</span>` : ""}</div>
      <div class="url">${esc(e.url)}</div>
    </div>`;
  }).join("") || '<p class="muted">No endpoints configured.</p>');
  const owners = Object.entries(s.owners || {}).sort((a, b) =>
    ((b[1].pending || 0) + (b[1].running || 0)) - ((a[1].pending || 0) + (a[1].running || 0)) || a[0].localeCompare(b[0]));
  setHTML("#owner-rows", owners.map(([o, c]) => `<tr><td>${avatar(o)} ${esc(o)}</td>
    ${["pending", "running", "done", "failed"].map((k) => `<td class="num">${c[k] || ""}</td>`).join("")}</tr>`).join("")
    || '<tr><td colspan="5" class="muted">No jobs yet.</td></tr>');
  setHTML("#recent-rows", (s.recent || []).map((j) => {
    const took = j.started && j.finished ? span((Date.parse(j.finished) - Date.parse(j.started)) / 1000) : "";
    return `<tr><td title="${esc(j.error || "")}">${j.status === "failed" ? '<span class="st st-failed"></span>' : ""}${esc(j.name)}</td>
      <td>${esc(j.owner)}</td><td>${esc(j.endpoint || "")}</td><td class="num">${took}</td><td class="sub-line">${ago(j.finished)}</td></tr>`;
  }).join("") || '<tr><td colspan="5" class="muted">Nothing finished yet (needs the updated queue server).</td></tr>');
}

// ---------------------------------------------------------------- activity, ask, charter

function renderActivityFilter() {
  const sel = $("#activity-agent");
  const names = state.agents.map((a) => a.name).sort();
  const have = [...sel.options].map((o) => o.value).filter(Boolean);
  if (names.join() === have.join()) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">All agents</option>' + names.map((n) => `<option>${esc(n)}</option>`).join("");
  sel.value = names.includes(current) ? current : "";
}

function renderActivity() {
  const c = chan.activity;
  if (!c) return;
  const who = $("#activity-agent").value;
  const shown = c.list.filter((m) => !who || m.author_name === who).slice().reverse();
  setHTML("#activity-list", (shown.length ? `<div class="threads">${shown.map((m) => `<div class="thread" data-root="${m.id}">${msgHtml(m)}</div>`).join("")}</div>`
    : '<p class="muted">No shift reports yet. Each agent posts one when a shift ends.</p>')
    + (c.done ? "" : '<button class="load-older" data-older="activity">Load older reports</button>'));
}

function explainers() {
  return (state ? state.agents : []).filter((a) => (a.skills || []).includes("explainer"));
}

function renderAsk() {
  const online = explainers().filter((a) => a.status !== "offline");
  const warn = $("#ask-offline");
  warn.hidden = online.length > 0;
  warn.innerHTML = "No explainer is online. Start one with <code>swarm/start_explainer.sh &lt;board-url&gt;</code>, "
    + "or chat in a terminal with <code>swarm/explain.sh &lt;board-url&gt; \"&lt;your name&gt;\"</code>.";
  const c = chan.ask;
  if (!c) return;
  const el = $("#ask-list");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  setHTML(el, c.list.length ? c.list.map((m) => msgHtml(m, { where: "ask" })).join("") : '<p class="muted">Nothing asked yet.</p>');
  if (atBottom || c.list.length < 3) el.scrollTop = el.scrollHeight;
}

const CHARTER_FIELDS = [["objective", "Objective"], ["constraints", "Constraints"], ["libraries", "Libraries"],
  ["compute", "Compute available"], ["notes", "Notes"]];

function renderCharter() {
  const c = state.charter;
  $("#charter-version").innerHTML = `v${c.version} · edited by ${esc(c.edited_by)} ${ago(c.created_at)}`;
  tickAgo($("#charter-version"));
  setHTML("#charter-view", CHARTER_FIELDS.map(([k, label]) => `
    <div class="charter-field"><b>${label}</b><div class="md">${c[k] ? md(c[k]) : '<span class="muted">—</span>'}</div></div>`).join(""));
}

// ---------------------------------------------------------------- state, tabs, live updates

function renderState() {
  if (!state) return;
  const end = state.settings.swarm_end;
  $("#ends").textContent = !end ? "" : (Date.parse(end) <= now() ? "swarm ended " : "freeze ") + new Date(end).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  renderStats();
  renderNeedsHuman();
  renderAgents();
  renderTaskList();
  renderTaskDetail();
  renderActivityFilter();
  renderAsk();
  renderChannelNav();
  if (!editingCharter) renderCharter();
  if (tab === "compute") renderCompute();
}

async function refreshState() {
  const data = await getJSON("/api/state");
  serverOffsetMs = Date.parse(data.server_time) - Date.now();
  state = data;
  renderState();
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refreshState, 400);
}

function showTab(name, arg) {
  tab = TABS.includes(name) ? name : "overview";
  document.querySelectorAll("main > section[data-tab]").forEach((el) => (el.hidden = el.dataset.tab !== tab));
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const hash = tab === "messages" ? `#messages/${channel}` : tab === "tasks" && taskSel ? `#tasks/${taskSel}` : `#${tab}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  if (tab === "messages") renderChannel();
  if (tab === "compute") { renderCompute(); refreshCompute(); }
  if (tab === "activity") renderActivity();
  if (tab === "ask") renderAsk();
}

function route() {
  const [name, arg] = location.hash.slice(1).split("/");
  if (name === "messages" && arg) return openChannel(decodeURIComponent(arg));
  if (name === "tasks" && arg) return openTask(Number(arg));
  showTab(name);
}

function onMessage(m) {
  if (!addMessage(m)) return;
  if (m.task_id) {
    if (m.task_id === taskSel) renderTaskThread();
    return;
  }
  if (m.channel === channel && tab === "messages") renderChannel();
  else renderChannelNav();
  if (m.channel === "findings") renderFindings();
  if (m.channel === "activity" && tab === "activity") renderActivity();
  if (m.channel === "ask") renderAsk();
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
    if (ev.type === "message.posted") onMessage(ev.payload);
    scheduleRefresh();
  });
}

// Opens the channel or task that holds a message, loading older pages if needed, and highlights it.
async function jumpTo(id) {
  let m = Object.values(chan).flatMap((c) => c.list).find((x) => x.id === id)
    || Object.values(taskMsgs).flat().find((x) => x.id === id);
  if (!m) m = await api("GET", `/api/messages/${id}`);
  if (m.task_id) {
    await openTask(m.task_id);
  } else {
    if (!chan[m.channel]) await loadChannel(m.channel);
    for (let i = 0; i < 10 && !chan[m.channel].list.some((x) => x.id === id) && !chan[m.channel].done; i++) await loadOlder(m.channel);
    const list = chan[m.channel].list;
    // Open the post's thread and the post itself.
    const byId = new Map(list.map((x) => [x.id, x]));
    let r = byId.get(id) || m;
    for (let i = 0; i < 50 && r.reply_to && byId.has(r.reply_to); i++) r = byId.get(r.reply_to);
    openThreads.add(r.id);
    expanded.add(id);
    $("#channel-search").value = "";
    await openChannel(m.channel);
  }
  const el = document.querySelector(`main > section:not([hidden]) [data-mid="${id}"]`);
  if (el) {
    el.scrollIntoView({ block: "center" });
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
  }
}

function setReply(where, id, author) {
  reply[where] = id ? { id, author } : null;
  const el = { messages: $("#reply-indicator"), tasks: $("#task-reply-indicator"), ask: $("#ask-reply") }[where];
  el.hidden = !id;
  el.innerHTML = id ? `Replying to #${id} (${esc(author)}) <button class="link" data-cancel-reply="${where}">cancel</button>` : "";
}

// ---------------------------------------------------------------- events

document.addEventListener("click", async (e) => {
  const el = e.target.closest("#tabs [data-tab],[data-open-task],[data-select-task],[data-channel],[data-msg],[data-pause],[data-resume],[data-remove-agent],[data-reply],[data-cancel-reply],[data-release],[data-set-status],[data-resolve],[data-expand],[data-open-thread],[data-older],[data-task-filter],[data-goto]");
  if (!el || el.closest("dialog")) return;
  const d = el.dataset;
  if (el.tagName === "A") e.preventDefault();
  if (d.tab) return showTab(d.tab);
  if (d.msg) return jumpTo(Number(d.msg));
  if (d.openTask) return openTask(Number(d.openTask));
  if (d.selectTask) return openTask(Number(d.selectTask));
  if (d.channel) return openChannel(d.channel);
  if (d.taskFilter) { taskFilter = d.taskFilter; return renderTaskList(); }
  if (d.goto) {
    if (["tasks", "compute", "charter"].includes(d.goto)) return showTab(d.goto);
    const target = d.goto === "agents" ? $("#agent-rows").closest(".panel") : $("#needs-human");
    return target && !target.hidden && target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  if (d.expand) {
    const key = /^\d+$/.test(d.expand) ? Number(d.expand) : d.expand;
    const open = !expanded.has(key);
    if (open) expanded.add(key); else expanded.delete(key);
    const body = el.previousElementSibling;
    body.classList.toggle("clamp", !open);
    el.textContent = open ? "Show less" : "Show more";
    // Keep cached HTML in sync so the next diff doesn't undo the toggle.
    document.querySelectorAll("#message-list, #task-thread, #task-detail, #activity-list, #ask-list").forEach((c) => (c._html = null));
    return;
  }
  if (d.openThread) {
    openThreads.add(Number(d.openThread));
    return tab === "tasks" ? renderTaskThread() : renderChannel();
  }
  if (d.older) {
    el.disabled = true;
    if (d.older === "channel") { await loadOlder(channel); renderChannel(); }
    else { await loadOlder("activity"); renderActivity(); }
    return;
  }
  if (d.reply) {
    setReply(d.reply, Number(d.id), d.author);
    const form = { messages: "#message-form", tasks: "#task-message-form", ask: "#ask-form" }[d.reply];
    $(form).scrollIntoView({ block: "nearest" });
    $(`${form} textarea`).focus();
    return;
  }
  if (d.cancelReply) return setReply(d.cancelReply, null);
  if (d.pause) {
    const reason = prompt(`Pause ${d.pause}? Optional reason:`, "");
    if (reason !== null) await api("POST", `/api/agents/${encodeURIComponent(d.pause)}/pause`, { reason });
  } else if (d.resume) await api("POST", `/api/agents/${encodeURIComponent(d.resume)}/resume`);
  else if (d.removeAgent) {
    if (confirm(`Remove ${d.removeAgent} from the agent list? Tasks it still holds go back to open. If it comes back online, it reappears.`)) {
      await api("DELETE", `/api/agents/${encodeURIComponent(d.removeAgent)}`);
    }
  } else if (d.release) await api("POST", `/api/tasks/${d.release}/release`);
  else if (d.setStatus) {
    const status = document.querySelector(`[data-status-for="${d.setStatus}"]`).value;
    await api("PATCH", `/api/tasks/${d.setStatus}`, { status });
  } else if (d.resolve) await api("POST", `/api/questions/${d.resolve}/resolve`);
  scheduleRefresh();
});

document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (!form.dataset.answer) return;
  e.preventDefault();
  const input = form.querySelector("input");
  await api("POST", "/api/messages", { body: input.value, type: "human_input", reply_to: Number(form.dataset.answer) });
  input.value = "";
  scheduleRefresh();
});

function ctrlEnter(form) {
  $(`${form} textarea`).addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $(form).requestSubmit();
  });
}
["#message-form", "#task-message-form", "#ask-form"].forEach(ctrlEnter);

$("#message-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const m = await api("POST", "/api/messages", {
    body: f.elements.body.value, type: f.elements.type.value, channel, reply_to: reply.messages && reply.messages.id,
  });
  f.elements.body.value = "";
  setReply("messages", null);
  onMessage(m);
});

$("#task-message-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const m = await api("POST", "/api/messages", {
    body: f.elements.body.value, type: "human_input", task_id: taskSel, reply_to: reply.tasks && reply.tasks.id,
  });
  f.elements.body.value = "";
  setReply("tasks", null);
  onMessage(m);
});

$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  let body = f.elements.body.value.trim();
  const names = explainers().map((a) => a.name);
  const target = names[0] || "explainer";
  if (!(names.length ? names : [target]).some((n) => body.toLowerCase().includes(`@${n.toLowerCase()}`))) {
    body = `@${target} ${body}`;
  }
  const m = await api("POST", "/api/messages", { body, type: "question", channel: "ask", reply_to: reply.ask && reply.ask.id });
  f.elements.body.value = "";
  setReply("ask", null);
  onMessage(m);
});

$("#channel-search").addEventListener("input", renderChannel);
$("#task-search").addEventListener("input", renderTaskList);
$("#show-offline").addEventListener("change", renderAgents);
$("#activity-agent").addEventListener("change", renderActivity);
window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (!document.hidden && tab === "messages") renderChannel(); });

$("#new-task-btn").addEventListener("click", () => { $("#task-form").hidden = false; $("#task-form").elements.title.focus(); });
$("#task-cancel").addEventListener("click", () => { $("#task-form").reset(); $("#task-form").hidden = true; });
$("#task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const payload = { title: f.elements.title.value, description: f.elements.description.value };
  if (f.elements.parent_id.value) payload.parent_id = Number(f.elements.parent_id.value);
  const t = await api("POST", "/api/tasks", payload);
  f.reset();
  f.hidden = true;
  await refreshState();
  openTask(t.id);
});

$("#charter-edit").addEventListener("click", () => {
  const f = $("#charter-form");
  for (const [k] of CHARTER_FIELDS) f.elements[k].value = state.charter[k];
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
  for (const [k] of CHARTER_FIELDS) payload[k] = f.elements[k].value;
  state.charter = await api("PUT", "/api/charter", payload);
  closeCharterEditor();
  if (!$("#charter-history").hidden) showHistory();
});

async function showHistory() {
  const versions = await getJSON("/api/charter/history");
  $("#charter-history").innerHTML = versions.map((v) => `
    <details class="history-item"><summary>v${v.version} · ${esc(v.edited_by)} · ${new Date(v.created_at).toLocaleString()}</summary>
      ${CHARTER_FIELDS.map(([k, label]) => `<div class="charter-field"><b>${label}</b><div class="md">${v[k] ? md(v[k]) : '<span class="muted">—</span>'}</div></div>`).join("")}
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
  if (name) setName(name);
  await refreshState();
  showTab(location.hash.slice(1).split("/")[0]);  // show the page now; channels fill in as they load
  if (!name) $("#name-dialog").showModal();
  connectStream();
  refreshCompute();
  await Promise.all(channelList().concat(OWN_TAB_CHANNELS).map(async (c) => {
    await loadChannel(c, c === "activity" ? 100 : 150);
    if (c === "findings") renderFindings();
    if (c === channel && tab === "messages") renderChannel();
    if (c === "activity" && tab === "activity") renderActivity();
    if (c === "ask") renderAsk();
    renderChannelNav();
  }));
  route();
  setInterval(refreshState, 20000);                                  // offline status, leases
  setInterval(() => refreshCompute(), 15000);                        // queue and endpoints
  setInterval(() => { tickAgo(); if (state) renderStats(); }, 10000); // relative times, countdown
})();
