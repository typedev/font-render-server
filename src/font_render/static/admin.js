// Font Render Service web admin. Data is inserted with textContent only.
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// ---------------------------------------------------------------- formatting
const fmtBytes = (n) => {
  if (n === null || n === undefined) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i && n < 10 ? 1 : 0)} ${u[i]}`;
};
const fmtDuration = (s) => {
  s = Math.floor(s);
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${hh}h`;
  if (hh) return `${hh}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
};
const fmtAgo = (t) => (t ? `${fmtDuration(Date.now() / 1000 - t)} ago` : "never");
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const fmtDate = (t) => new Date(t * 1000).toLocaleString([], { hour12: false });

function bar(used, total) {
  const pct = total ? Math.min(100, (100 * used) / total) : 0;
  const fill = h("span");
  fill.style.width = `${pct.toFixed(1)}%`;  // CSSOM, not a style attribute (CSP)
  return h("div", { class: "bar" + (pct > 90 ? " err" : pct > 75 ? " warn" : "") }, fill);
}

function kv(dl, rows) {
  dl.replaceChildren(...rows.filter(Boolean).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)]));
}

function table(el, columns, rows, empty = "Nothing here yet") {
  const head = h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label))));
  const body = h("tbody");
  if (!rows.length) body.append(h("tr", {}, h("td", { class: "empty", colspan: columns.length }, empty)));
  for (const r of rows) {
    body.append(h("tr", {}, columns.map((c) => h("td", { class: c.num ? "num" : "" }, c.value(r)))));
  }
  el.replaceChildren(head, body);
}

function toast(msg, kind = "") {
  const t = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "err" ? 9000 : 4500);
}

// ---------------------------------------------------------------- API
class Unauthorized extends Error {}

async function api(path, { method = "GET", form } = {}) {
  const opts = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (form) {
    const fd = new FormData();
    for (const [k, v] of Object.entries(form)) fd.append(k, v);
    opts.body = fd;
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    setConn(false);
    throw new Error("server unreachable");
  }
  setConn(true);
  if (resp.status === 401) { showLogin(); throw new Unauthorized("unauthorized"); }
  let data = null;
  try { data = await resp.json(); } catch { /* non-JSON */ }
  if (!resp.ok) {
    const msg = (data && (data.error || data.detail)) || `HTTP ${resp.status}`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function setConn(ok) { $("#conn").className = "dot " + (ok ? "ok" : "err"); }

const report = (e) => { if (!(e instanceof Unauthorized)) toast(e.message, "err"); };

// Two-step confirm: first click arms the button for 4 s, second click runs.
function armed(button, fn) {
  button.addEventListener("click", async () => {
    if (!button.classList.contains("confirming")) {
      const label = button.textContent;
      button.dataset.label = label;
      button.textContent = button.dataset.confirm || "Sure?";
      button.classList.add("confirming");
      button._t = setTimeout(() => { button.textContent = label; button.classList.remove("confirming"); }, 4000);
      return;
    }
    clearTimeout(button._t);
    button.textContent = button.dataset.label;
    button.classList.remove("confirming");
    await busy(button, fn);
  });
}

async function busy(button, fn) {
  button.disabled = true;
  try { await fn(); } catch (e) { report(e); } finally { button.disabled = false; }
}

// ---------------------------------------------------------------- state
let status = null;
let tab = "overview";
const timers = {};

function showLogin() {
  for (const k in timers) { clearInterval(timers[k]); delete timers[k]; }
  $("#app").hidden = true;
  $("#login").hidden = false;
  ($("#login-client").value ? $("#login-token") : $("#login-client")).focus();
}

async function start() {
  try {
    await refreshStatus();
  } catch (e) {
    if (!(e instanceof Unauthorized)) { showLogin(); $("#login-error").textContent = e.message; }
    return;
  }
  $("#login").hidden = true;
  $("#app").hidden = false;
  try { tab = localStorage.getItem("frs-admin-tab") || tab; } catch { /* storage blocked */ }
  selectTab(tab);
  timers.status = setInterval(() => { if (!document.hidden) refreshStatus().catch(report); }, 5000);
  timers.tab = setInterval(() => { if (!document.hidden) refreshTab(false); }, 5000);
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  const client = $("#login-client").value.trim(), token = $("#login-token").value;
  try {
    await api("/admin/login", { method: "POST", form: { client, token } });
    // A password manager saves a username + password pair; PasswordCredential only in a secure context
    if (window.PasswordCredential && window.isSecureContext && client) {
      try { await navigator.credentials.store(new PasswordCredential({ id: client, password: token })); } catch { /* declined */ }
    }
    await start();
    // cleared only once the form is hidden, so Chrome sees a finished login with the values in it
    $("#login-token").value = "";
  } catch (e) {
    if (!(e instanceof Unauthorized)) $("#login-error").textContent = e.message;
    else $("#login-error").textContent = client ? "Not an admin token of this client" : "Not an admin token";
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("/admin/logout", { method: "POST" }); } catch { /* ignore */ }
  showLogin();
});

function selectTab(name) {
  if (!$(`[data-panel="${name}"]`)) name = "overview";
  tab = name;
  try { localStorage.setItem("frs-admin-tab", name); } catch { /* storage blocked */ }
  for (const b of $$(".tabs button")) b.classList.toggle("active", b.dataset.tab === name);
  for (const p of $$("[data-panel]")) p.hidden = p.dataset.panel !== name;
  refreshTab(true);
}
for (const b of $$(".tabs button")) b.addEventListener("click", () => selectTab(b.dataset.tab));

function refreshTab(first) {
  if (tab === "load" && (first || Date.now() - loadFetched > 15000)) refreshLoad().catch(report);
  if (tab === "traffic" && (first || Date.now() - trafficFetched > 30000)) refreshTraffic().catch(report);
  if (tab === "requests") refreshRequests().catch(report);
  if (tab === "logs" && (first || $("#lg-auto").checked)) refreshLogs(first).catch(report);
  if (tab === "maintenance" && first) { renderMaintenance(); refreshConfig().catch(report); }
}

// ---------------------------------------------------------------- overview
async function refreshStatus() {
  status = await api("/admin/api/status");
  renderOverview();
  renderClients();
  renderMaintenance();
  renderDevBanner();
  $("#who-version").textContent = status.server.version;
  $("#who-subject").textContent = status.subject;
}

function renderOverview() {
  const s = status, srv = s.server, p = s.pool, lat = s.render_latency, proc = s.process;
  kv($("#ov-server"), [
    ["Version", h("span", { class: "mono" }, `${srv.version} (${srv.branch})`)],
    ["Commit date", srv.commit_date],
    ["Uptime", `${fmtDuration(srv.uptime_s)} — since ${fmtDate(srv.started)}`],
    ["Clients connect to", h("span", { class: "mono" },
      [...(srv.addresses ? srv.addresses.by_name : []),
       ...(srv.addresses ? srv.addresses.by_ip.map((x) => `${x.url} (${x.interface})`) : [])].join("  ·  "))],
    ["Render version", String(srv.render_version)],
    ["Platform", srv.platform],
    ["Python", srv.python],
    ["Rollback target", srv.rollback_target || "none"],
  ]);
  const inflight = Object.entries(s.in_flight_per_client);
  kv($("#ov-load"), [
    ["Rendering", h("span", {}, `${p.busy} / ${p.concurrency}`, bar(p.busy, p.concurrency))],
    ["Queued", h("span", {}, `${p.queued} / ${p.queue_max}`, bar(p.queued, p.queue_max))],
    ["Per client", inflight.length ? inflight.map(([k, v]) => `${k}: ${v}`).join(", ") : "idle"],
    ["Render latency", lat.count ? `p50 ${lat.p50} ms · p95 ${lat.p95} ms · max ${lat.max} ms` : "no renders yet",],
    lat.count && ["Sample", `last ${lat.count} successful renders`],
    ["Cached pages", String(p.page_cache.pages)],
  ]);
  kv($("#ov-machine"), proc ? [
    ["Server process", fmtBytes(proc.rss)],
    ["Browsers", `${fmtBytes(proc.children_rss)} in ${proc.children} processes`],
    ["CPU", `${proc.cpu_percent.toFixed(0)} %`],
    ["Memory", h("span", {}, `${fmtBytes(proc.mem_total - proc.mem_available)} / ${fmtBytes(proc.mem_total)}`,
      bar(proc.mem_total - proc.mem_available, proc.mem_total))],
    ["Disk", h("span", {}, `${fmtBytes(s.disk.free)} free of ${fmtBytes(s.disk.total)}`,
      bar(s.disk.total - s.disk.free, s.disk.total))],
  ] : [
    ["Process stats", "psutil not installed"],
    ["Disk", `${fmtBytes(s.disk.free)} free of ${fmtBytes(s.disk.total)}`],
  ]);
  const fc = s.font_cache;
  const ns = Object.entries(fc.namespaces);
  kv($("#ov-fonts"), [
    ["Fonts", String(fc.fonts)],
    ["Memory", h("span", {}, `${fmtBytes(fc.bytes)} / ${fmtBytes(fc.max_bytes)}`, bar(fc.bytes, fc.max_bytes))],
    ["Idle TTL", fmtDuration(fc.ttl_s)],
    ["Per client", ns.length ? ns.map(([k, v]) => `${k}: ${v.fonts} (${fmtBytes(v.bytes)})`).join(", ") : "empty"],
  ]);

  table($("#ov-browsers"), [
    { label: "Engine", value: (r) => r.key },
    { label: "Version", value: (r) => r.version || "–" },
    { label: "State", value: (r) => h("span", { class: "badge " + (!r.alive ? "err" : r.retiring ? "warn" : "ok") },
        !r.alive ? "closed" : r.retiring ? "retiring" : "live") },
    { label: "Active", num: true, value: (r) => String(r.active) },
    { label: "Renders", num: true, value: (r) => `${r.renders} / ${s.config.RECYCLE_AFTER}` },
    { label: "Cached pages", num: true, value: (r) => String(r.pages) },
  ], s.browsers.slots, "No browsers running (they start on demand)");
  $("#ov-installed").textContent = "Installed: " + Object.entries(s.browsers.installed)
    .map(([k, v]) => `${k} ${v ? "✓" : "✗"}`).join(" · ");

  const activity = Object.entries(s.activity).map(([id, a]) => ({ id, ...a }))
    .sort((a, b) => b.last_seen - a.last_seen);
  table($("#ov-activity"), [
    { label: "Client", value: (r) => r.id },
    { label: "Requests", num: true, value: (r) => String(r.requests) },
    { label: "Renders", num: true, value: (r) => String(r.renders) },
    { label: "Errors", num: true, value: (r) => h("span", { class: r.errors ? "s4" : "" }, String(r.errors)) },
    { label: "Last seen", value: (r) => fmtAgo(r.last_seen) },
    { label: "Last IP", value: (r) => r.last_ip },
  ], activity, "No requests since restart");
}

// ---------------------------------------------------------------- clients
function renderClients() {
  const s = status;
  // Don't rebuild the table under an armed (awaiting confirm) button
  if ($("#cl-table .confirming")) return;
  table($("#cl-table"), [
    { label: "Client", value: (c) => h("b", {}, c.client_id) },
    { label: "Scopes", value: (c) => c.scopes.map((x) => h("span", { class: "badge" + (x === "admin" ? " warn" : "") }, x)) },
    { label: "Status", value: (c) => h("span", { class: "badge " + (c.disabled ? "err" : "ok") }, c.disabled ? "disabled" : "enabled") },
    { label: "Created", value: (c) => c.created || "–" },
    { label: "Requests", num: true, value: (c) => String(c.requests ?? 0) },
    { label: "Errors", num: true, value: (c) => String(c.errors ?? 0) },
    { label: "Last seen", value: (c) => fmtAgo(c.last_seen) },
    { label: "Last IP", value: (c) => c.last_ip || "–" },
    { label: "", value: (c) => clientActions(c) },
  ], s.clients, "No clients yet");
  $("#set-anon").checked = !!s.settings.allow_anonymous_render;
  $("#set-legacy").checked = !!s.settings.legacy_admin_token;
}

function clientActions(c) {
  const mk = (label, action, confirm, cls = "") => {
    const b = h("button", { class: `small ${cls}`, "data-confirm": confirm }, label);
    armed(b, async () => {
      const r = await api(`/admin/clients/${action}`, { method: "POST", form: { client_id: c.client_id } });
      if (r.token) revealToken(c.client_id, r.token);
      toast(`${c.client_id}: ${action} done`, "ok");
      await refreshStatus();
    });
    return b;
  };
  return h("div", { class: "row" },
    mk("Rotate", "rotate", "New token?"),
    c.disabled ? mk("Enable", "enable", "Enable?") : mk("Disable", "disable", "Disable?"),
    mk("Remove", "remove", "Remove?", "ghost"));
}

function revealToken(clientId, token) {
  $("#token-client").textContent = clientId;
  $("#token-value").value = token;
  $("#token-reveal").hidden = false;
  $("#token-value").select();
}
$("#token-copy").addEventListener("click", async () => {
  const v = $("#token-value").value;
  try { await navigator.clipboard.writeText(v); toast("Copied", "ok"); }
  catch { $("#token-value").select(); document.execCommand("copy"); toast("Copied", "ok"); }
});
$("#token-hide").addEventListener("click", () => { $("#token-value").value = ""; $("#token-reveal").hidden = true; });

$("#cl-add").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const id = $("#cl-id").value.trim();
  const scopes = $$('#cl-add input[name="scope"]:checked').map((x) => x.value).join(",");
  if (!scopes) { toast("Pick at least one scope", "err"); return; }
  const btn = ev.submitter || $("#cl-add button");
  await busy(btn, async () => {
    const r = await api("/admin/clients/add", { method: "POST", form: { client_id: id, scopes } });
    revealToken(id, r.token);
    $("#cl-id").value = "";
    await refreshStatus();
  });
});

for (const [el, field] of [["#set-anon", "allow_anonymous_render"], ["#set-legacy", "legacy_admin_token"]]) {
  $(el).addEventListener("change", async (ev) => {
    const box = ev.target;
    box.disabled = true;
    try {
      await api("/admin/settings", { method: "POST", form: { [field]: box.checked ? "true" : "false" } });
      toast("Settings saved", "ok");
      await refreshStatus();
    } catch (e) {
      box.checked = !box.checked;
      report(e);
    } finally {
      box.disabled = false;
    }
  });
}

// ---------------------------------------------------------------- charts (SVG, no library)
const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== undefined && v !== null) el.setAttribute(k, v);
  for (const c of children.flat()) if (c !== null && c !== undefined) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

const fmtNum = (v) => (v === null || v === undefined ? "–" : Math.abs(v) >= 100 || Number.isInteger(v) ? Math.round(v).toLocaleString() : v.toFixed(1));
const fmtMs = (v) => (v === null || v === undefined ? "–" : v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} s` : `${Math.round(v)} ms`);
const fmtPct = (v) => (v === null || v === undefined ? "–" : `${Math.round(v)} %`);

// opts: {title, note, series: [{key, label, cls, type: "area"|"line"|"bar"}], fmt, ref, refLabel, yMin}
// data: {bins: [{t, ...}], step, bands: [[t0, t1]]}
function chart(opts, data) {
  const card = h("div", { class: "card chart" });
  card.append(h("h2", {}, opts.title, opts.note ? h("span", { class: "muted small" }, " " + opts.note) : null));
  card.append(h("div", { class: "legend" }, opts.series.map((s) => h("span", {}, h("i", { class: s.cls }), s.label)),
    opts.ref ? h("span", {}, h("i", { class: "c3" }), opts.refLabel || "limit") : null));
  const W = opts.width || 600, H = 180, L = 46, R = 8, T = 8, B = 20, PW = W - L - R, PH = H - T - B;
  const { bins, step, bands } = data;
  const n = bins.length;
  const t0 = n ? bins[0].t : 0, span = n * step;
  const xt = (t) => L + ((t - t0) / span) * PW;
  let vmax = opts.yMin || 0;
  for (const b of bins) for (const s of opts.series) if (b[s.key] !== null && b[s.key] !== undefined) vmax = Math.max(vmax, b[s.key]);
  if (opts.ref) vmax = Math.max(vmax, opts.ref);
  const ymax = opts.yMax || niceMax(vmax * 1.05);
  const y = (v) => T + (1 - v / ymax) * PH;
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.title });

  for (let i = 0; i <= 4; i++) {
    const v = (ymax * i) / 4, yy = y(v);
    svg.append(sv("line", { class: "grid", x1: L, x2: W - R, y1: yy, y2: yy }));
    svg.append(sv("text", { class: "axis", x: L - 6, y: yy + 3, "text-anchor": "end" }, (opts.fmt || fmtNum)(v)));
  }
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const t = t0 + (span * i) / ticks;
    const d = new Date(t * 1000);
    const label = span > 86400 * 1.5
      ? d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", hour12: false })
      : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    svg.append(sv("text", { class: "axis", x: xt(t), y: H - 5, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle" }, label));
  }
  for (const [a, b] of bands || []) {
    if (b < t0 || a > t0 + span) continue;
    const x1 = xt(Math.max(a, t0)), x2 = xt(Math.min(b + 60, t0 + span));
    svg.append(sv("rect", { class: "band", x: x1, y: T, width: Math.max(3, x2 - x1), height: PH }));
  }
  const bw = PW / Math.max(1, n);
  const xi = (i) => L + (i + 0.5) * bw;
  for (const s of opts.series) {
    if (s.type === "bar") {
      bins.forEach((b, i) => {
        const v = b[s.key];
        if (v) svg.append(sv("rect", { class: `bar ${s.cls}`, x: xi(i) - bw * 0.4, y: y(v), width: Math.max(1, bw * 0.8), height: y(0) - y(v) }));
      });
      continue;
    }
    // Split into runs without gaps (missing minutes = server down)
    let run = [];
    const flush = () => {
      if (!run.length) return;
      const pts = run.map(([i, v]) => `${xi(i).toFixed(1)},${y(v).toFixed(1)}`);
      if (s.type === "area") {
        const first = xi(run[0][0]).toFixed(1), last = xi(run[run.length - 1][0]).toFixed(1);
        svg.append(sv("path", { class: `area ${s.cls}`, d: `M${first},${y(0)} L${pts.join(" L")} L${last},${y(0)} Z` }));
      } else {
        svg.append(sv("path", { class: `line ${s.cls}`, d: `M${pts.join(" L")}` }));
      }
      if (run.length === 1) {  // an isolated minute: a line would be invisible
        svg.append(sv("circle", { class: `dot ${s.cls}`, cx: xi(run[0][0]), cy: y(run[0][1]), r: 2.5 }));
      }
      run = [];
    };
    bins.forEach((b, i) => {
      const v = b[s.key];
      if (v === null || v === undefined) flush(); else run.push([i, v]);
    });
    flush();
  }
  if (opts.ref) svg.append(sv("line", { class: "ref", x1: L, x2: W - R, y1: y(opts.ref), y2: y(opts.ref) }));

  // Hover: cursor line + tooltip
  const cursor = sv("line", { class: "cursor", y1: T, y2: T + PH, visibility: "hidden" });
  const hit = sv("rect", { x: L, y: T, width: PW, height: PH, fill: "transparent" });
  svg.append(cursor, hit);
  const tip = h("div", { class: "tip", hidden: true });
  hit.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect();
    const mx = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.min(n - 1, Math.max(0, Math.floor((mx - L) / bw)));
    const b = bins[i];
    if (!b) return;
    cursor.setAttribute("x1", xi(i)); cursor.setAttribute("x2", xi(i)); cursor.setAttribute("visibility", "visible");
    const when = new Date(b.t * 1000).toLocaleString([], { hour12: false, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    tip.replaceChildren(h("b", {}, when + (step > 60 ? ` (+${step / 60} min)` : "")),
      ...opts.series.map((s) => h("div", {}, `${s.label}: ${b[s.key] === null || b[s.key] === undefined ? "no data" : (opts.fmt || fmtNum)(b[s.key])}`)));
    tip.hidden = false;
    const px = ((xi(i) / W) * r.width);
    tip.style.left = `${Math.min(px + 12, r.width - 160)}px`;
    tip.style.top = "40px";
  });
  hit.addEventListener("mouseleave", () => { tip.hidden = true; cursor.setAttribute("visibility", "hidden"); });
  card.append(svg, tip);
  return card;
}

// ---------------------------------------------------------------- load
let loadRange = 60, loadFetched = 0;
try { loadRange = Number(localStorage.getItem("frs-admin-range")) || 60; } catch { /* storage blocked */ }

function binStep(rangeMin) { return rangeMin <= 360 ? 60 : rangeMin <= 1440 ? 300 : 1800; }

function aggregate(minutes, rangeMin) {
  const step = binStep(rangeMin);
  const nowMin = Math.floor(Date.now() / 60000) * 60;
  const t0 = Math.floor((nowMin - rangeMin * 60 + 60) / step) * step;
  const n = Math.floor((nowMin - t0) / step) + 1;
  const acc = Array.from({ length: n }, (_, i) => ({ t: t0 + i * step, m: [] }));
  for (const m of minutes) {
    const i = Math.floor((m.t - t0) / step);
    if (i >= 0 && i < n) acc[i].m.push(m);
  }
  const perMin = step / 60;
  return {
    step,
    bins: acc.map(({ t, m }) => {
      if (!m.length) return { t };
      const sum = (k) => m.reduce((a, x) => a + (x[k] || 0), 0);
      const max = (k) => Math.max(...m.map((x) => x[k] || 0));
      const avg = (k) => { const v = m.filter((x) => x[k] !== null && x[k] !== undefined); return v.length ? v.reduce((a, x) => a + x[k], 0) / v.length : null; };
      const pick = (k, f) => { const v = m.map((x) => x[k]).filter((x) => x !== null && x !== undefined); return v.length ? f(...v) : null; };
      const renders = sum("renders");
      const withLat = m.filter((x) => x.p50 !== undefined);
      return {
        t,
        req: sum("req") / perMin, renders: renders / perMin,
        bad: (sum("err")) / perMin, rejected: sum("rejected"), timeouts: sum("timeouts"),
        in: sum("in") / perMin, out: sum("out") / perMin,
        busy_avg: avg("busy_avg"), busy_max: max("busy_max"), queue_max: max("queue_max"), wait_max: max("wait_max"),
        p50: withLat.length ? withLat.reduce((a, x) => a + x.p50 * (x.renders || 1), 0) / withLat.reduce((a, x) => a + (x.renders || 1), 0) : null,
        p95: withLat.length ? Math.max(...withLat.map((x) => x.p95)) : null,
        cpu: avg("cpu"),
        mem_py: pick("mem_py", Math.max), mem_br: pick("mem_br", Math.max), mem_avail: pick("mem_avail", Math.min),
      };
    }),
  };
}

function tile(label, value, cls = "", title = "") {
  return h("div", { class: `tile ${cls}`, title }, h("div", { class: "v" }, value), h("div", { class: "k" }, label));
}

async function refreshLoad() {
  const d = await api(`/admin/api/load?minutes=${loadRange}`);
  loadFetched = Date.now();
  for (const b of $$("#ld-range button")) b.classList.toggle("active", Number(b.dataset.min) === loadRange);
  const mins = d.minutes, cap = d.concurrency;
  const sum = (k) => mins.reduce((a, x) => a + (x[k] || 0), 0);
  const max = (k) => mins.reduce((a, x) => Math.max(a, x[k] || 0), 0);
  const fullMin = mins.filter((x) => x.busy_max >= cap).length;
  const utilAvg = mins.length ? (100 * mins.reduce((a, x) => a + (x.busy_avg || 0), 0)) / mins.length / cap : 0;
  const rejected = sum("rejected"), timeouts = sum("timeouts"), errors = sum("err");
  const lat = mins.filter((x) => x.p95 !== undefined);
  let verdict, vcls;
  if (rejected || timeouts) { verdict = "Overloaded"; vcls = "err"; }
  else if (fullMin || max("queue_max") > 0) { verdict = "Busy at times"; vcls = "warn"; }
  else { verdict = "Headroom"; vcls = ""; }
  $("#ld-summary").replaceChildren(
    tile("verdict for this range", verdict, vcls, "Overloaded: requests were rejected (429) or timed out. Busy: all render slots were in use or renders queued."),
    tile(`avg utilization of ${cap} slots`, fmtPct(utilAvg)),
    tile("peak renders in flight", `${max("busy_max")} / ${cap}`, max("busy_max") >= cap ? "warn" : ""),
    tile("minutes at full capacity", String(fullMin), fullMin ? "warn" : ""),
    tile(`peak queue (limit ${d.queue_max})`, String(max("queue_max")), max("queue_max") ? "warn" : ""),
    tile("longest queue wait", fmtMs(max("wait_max")), max("wait_max") >= d.overload_wait_s * 1000 ? "err" : max("wait_max") > 500 ? "warn" : ""),
    tile("rejected, queue full (429)", String(rejected), rejected ? "err" : ""),
    tile("timeouts (504)", String(timeouts), timeouts ? "err" : ""),
    tile("renders / peak per min", `${sum("renders").toLocaleString()} / ${max("renders")}`),
    tile("worst-minute p95 latency", lat.length ? fmtMs(Math.max(...lat.map((x) => x.p95))) : "–"),
    tile("errors (4xx/5xx)", String(errors), errors ? "warn" : "", "Includes 401/409 from clients — see Requests for details"),
    tile("throttled per client (429)", String(sum("client_limit")), "", "A client exceeded RENDER_CLIENT_MAX renders in flight; the server itself was not necessarily full"),
  );

  table($("#ld-episodes"), [
    { label: "Start", value: (e) => fmtDate(e.start) },
    { label: "Duration", value: (e) => (e.ongoing ? "ongoing, " : "") + fmtDuration(Math.max(1, e.end - e.start)) },
    { label: "Severity", value: (e) => h("span", { class: `badge ${e.severity === "critical" ? "err" : "warn"}` }, e.severity) },
    { label: "Events", value: (e) => Object.entries(e.counts).map(([k, v]) => `${k.replace("_", " ")} ${v}`).join(", ") },
    { label: "Clients", value: (e) => Object.entries(e.clients).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${v}`).join(", ") },
    { label: "Max queue", num: true, value: (e) => String(e.max_queue) },
    { label: "Max busy", num: true, value: (e) => String(e.max_busy) },
    { label: "Max wait", num: true, value: (e) => (e.max_wait_ms ? fmtMs(e.max_wait_ms) : "–") },
  ], d.episodes.slice(0, 50), "No overload episodes recorded");

  const data = { ...aggregate(mins, loadRange), bands: d.episodes_in_range.map((e) => [e.start, e.end]) };
  lastLoad = d;
  drawLoadCharts(d, data);
}

let lastLoad = null;
// Chart width = its grid cell, so SVG text is never stretched (redrawn on resize)
function drawLoadCharts(d, data) {
  const cap = d.concurrency;
  const cw = $("#ld-charts").clientWidth || 900;
  const cols = window.matchMedia("(min-width: 900px)").matches ? 2 : 1;
  const width = Math.max(300, Math.floor((cw - (cols - 1) * 16) / cols) - 34);
  const per = data.step > 60 ? `avg per minute, ${data.step / 60}-min bins` : "per minute";
  $("#ld-charts").replaceChildren(
    chart({ width, title: "Renders in flight", note: "how many of the slots are busy", ref: cap, refLabel: `capacity ${cap}`,
      series: [{ key: "busy_avg", label: "average", cls: "c1", type: "area" }, { key: "busy_max", label: "peak", cls: "c2", type: "line" }] }, data),
    chart({ width, title: "Queue", note: "renders waiting for a slot", ref: d.queue_max, refLabel: `rejects above ${d.queue_max}`,
      series: [{ key: "queue_max", label: "peak queue", cls: "c5", type: "area" }] }, data),
    chart({ width, title: "Queue wait", note: "longest wait in the minute", fmt: fmtMs, ref: d.overload_wait_s * 1000, refLabel: "overload threshold",
      series: [{ key: "wait_max", label: "max wait", cls: "c2", type: "area" }] }, data),
    chart({ width, title: "Requests", note: per,
      series: [{ key: "renders", label: "renders", cls: "c4", type: "area" }, { key: "req", label: "all requests", cls: "c1", type: "line" }, { key: "bad", label: "errors", cls: "c3", type: "bar" }] }, data),
    chart({ width, title: "Render latency", note: "successful renders", fmt: fmtMs,
      series: [{ key: "p50", label: "p50", cls: "c1", type: "line" }, { key: "p95", label: "p95", cls: "c2", type: "line" }] }, data),
    chart({ width, title: "Traffic", note: per, fmt: fmtBytes,
      series: [{ key: "out", label: "out (to clients)", cls: "c6", type: "area" }, { key: "in", label: "in (uploads)", cls: "c5", type: "line" }] }, data),
    chart({ width, title: "CPU", note: "whole machine", fmt: fmtPct, yMax: 100,
      series: [{ key: "cpu", label: "CPU", cls: "c3", type: "area" }] }, data),
    chart({ width, title: "Memory", note: "browsers drop on recycle; free memory of the machine", fmt: fmtBytes,
      series: [{ key: "mem_br", label: "browsers (peak)", cls: "c5", type: "area" }, { key: "mem_avail", label: "free (lowest)", cls: "c4", type: "line" }] }, data),
    chart({ width, title: "Server process memory", note: "lives until restart: steady growth here is a leak", fmt: fmtBytes,
      series: [{ key: "mem_py", label: "Python (peak)", cls: "c1", type: "line" }] }, data),
  );
}
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (tab === "load" && lastLoad) drawLoadCharts(lastLoad, { ...aggregate(lastLoad.minutes, loadRange), bands: lastLoad.episodes_in_range.map((e) => [e.start, e.end]) });
    if (tab === "traffic") refreshTraffic().catch(report);
  }, 250);
});

for (const b of $$("#ld-range button")) {
  b.addEventListener("click", () => {
    loadRange = Number(b.dataset.min);
    try { localStorage.setItem("frs-admin-range", String(loadRange)); } catch { /* storage blocked */ }
    refreshLoad().catch(report);
  });
}

// ---------------------------------------------------------------- traffic
let trafficFetched = 0;
async function refreshTraffic() {
  const d = await api("/admin/api/traffic");
  trafficFetched = Date.now();
  const rows = Object.entries(d.clients).map(([id, t]) => ({ id, ...t }))
    .sort((a, b) => (b.month.in + b.month.out) - (a.month.in + a.month.out));
  const io = (x) => h("span", { title: `in ${x.in.toLocaleString()} B, out ${x.out.toLocaleString()} B` },
    `↑ ${fmtBytes(x.in)}  ↓ ${fmtBytes(x.out)}`);
  table($("#tr-table"), [
    { label: "Client", value: (r) => h("b", {}, r.id) },
    { label: "Today", value: (r) => io(r.today) },
    { label: "7 days", value: (r) => io(r.week) },
    { label: "30 days", value: (r) => io(r.month) },
    { label: "Total", value: (r) => io(r.total) },
    { label: "Requests", num: true, value: (r) => r.total.req.toLocaleString() },
    { label: "Renders", num: true, value: (r) => r.total.renders.toLocaleString() },
  ], rows, "No traffic recorded yet");

  // Stacked daily bars, last 30 days
  const days = [];
  for (let i = 29; i >= 0; i--) {
    const dt = new Date(Date.now() - i * 86400000);
    days.push(`${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`);
  }
  const W = Math.max(300, ($("#tr-chart").clientWidth || 900)), H = 220, L = 52, R = 8, T = 8, B = 20, PW = W - L - R, PH = H - T - B;
  const classes = ["c1", "c2", "c4", "c5", "c6", "c3"];
  const totals = days.map((day) => rows.reduce((a, r) => a + ((r.days[day] || {}).in || 0) + ((r.days[day] || {}).out || 0), 0));
  const ymax = niceMax(Math.max(1, ...totals) * 1.05);
  const y = (v) => T + (1 - v / ymax) * PH;
  const bw = PW / days.length;
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Daily traffic" });
  for (let i = 0; i <= 4; i++) {
    const v = (ymax * i) / 4;
    svg.append(sv("line", { class: "grid", x1: L, x2: W - R, y1: y(v), y2: y(v) }),
      sv("text", { class: "axis", x: L - 6, y: y(v) + 3, "text-anchor": "end" }, fmtBytes(v)));
  }
  days.forEach((day, i) => {
    let base = 0;
    rows.forEach((r, ci) => {
      const x = r.days[day];
      const v = x ? (x.in || 0) + (x.out || 0) : 0;
      if (!v) return;
      const rect = sv("rect", { class: `bar ${classes[ci % classes.length]}`, x: L + i * bw + bw * 0.1, y: y(base + v), width: bw * 0.8, height: y(base) - y(base + v) });
      rect.append(sv("title", {}, `${day} · ${r.id}: ${fmtBytes(v)} (${(x.renders || 0).toLocaleString()} renders)`));
      svg.append(rect);
      base += v;
    });
    if (i % 5 === 0 || i === days.length - 1) {
      svg.append(sv("text", { class: "axis", x: L + (i + 0.5) * bw, y: H - 5, "text-anchor": "middle" }, day.slice(5)));
    }
  });
  const legend = h("div", { class: "legend" }, rows.map((r, ci) => h("span", {}, h("i", { class: classes[ci % classes.length] }), r.id)));
  $("#tr-chart").replaceChildren(h("div", { class: "chart" }, legend, svg));
}

// ---------------------------------------------------------------- requests
async function refreshRequests() {
  const sel = $("#rq-client");
  const ids = status ? Object.keys(status.activity).sort() : [];
  const current = sel.value;
  const options = [h("option", { value: "" }, "all clients"), ...ids.map((x) => h("option", { value: x }, x))];
  if (sel.options.length !== options.length) { sel.replaceChildren(...options); sel.value = current; }
  const q = new URLSearchParams({ limit: "500" });
  if (sel.value) q.set("client", sel.value);
  const data = await api(`/admin/api/requests?${q}`);
  let rows = data.requests;
  if ($("#rq-errors").checked) rows = rows.filter((r) => r.status >= 400);
  table($("#rq-table"), [
    { label: "Time", value: (r) => fmtTime(r.t) },
    { label: "Client", value: (r) => r.client },
    { label: "IP", value: (r) => r.ip },
    { label: "Request", value: (r) => h("span", { class: "mono" }, `${r.method} ${r.path}`) },
    { label: "Status", value: (r) => h("b", { class: `s${String(r.status)[0]}` }, String(r.status)) },
    { label: "ms", num: true, value: (r) => r.ms.toFixed(0) },
    { label: "In", num: true, value: (r) => (r.in ? fmtBytes(r.in) : "–") },
    { label: "Out", num: true, value: (r) => (r.out ? fmtBytes(r.out) : "–") },
  ], rows, "No requests");
}
$("#rq-client").addEventListener("change", () => refreshRequests().catch(report));
$("#rq-errors").addEventListener("change", () => refreshRequests().catch(report));

// ---------------------------------------------------------------- logs
async function refreshLogs(scrollToEnd) {
  const pre = $("#lg-pre");
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  const q = new URLSearchParams({ lines: $("#lg-lines").value });
  const filter = $("#lg-filter").value.trim();
  if (filter) q.set("q", filter);
  const data = await api(`/admin/api/logs?${q}`);
  $("#lg-file").textContent = data.exists ? `${data.file} · ${fmtBytes(data.size)}` : `${data.file} (not found)`;
  const frag = document.createDocumentFragment();
  for (const line of data.lines) {
    const cls = /\bERROR\b|Traceback|Exception/.test(line) ? "lvl-error" : /\bWARNING\b/.test(line) ? "lvl-warning" : "";
    frag.append(h("span", { class: cls }, line + "\n"));
  }
  pre.replaceChildren(frag);
  if (scrollToEnd || atBottom) pre.scrollTop = pre.scrollHeight;
}
let filterTimer;
$("#lg-filter").addEventListener("input", () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => refreshLogs(true).catch(report), 300);
});
$("#lg-lines").addEventListener("change", () => refreshLogs(true).catch(report));
$("#lg-refresh").addEventListener("click", () => refreshLogs(true).catch(report));

// ---------------------------------------------------------------- configuration
const isBytes = (name) => name.endsWith("_BYTES") || name === "MAX_FONT_SIZE";
const toInput = (x, v) => (isBytes(x.name) ? String(Math.round((v / 1048576) * 100) / 100) : String(v));
const fromInput = (x, raw) => (isBytes(x.name) ? String(Math.round(Number(raw) * 1048576)) : raw);
const showValue = (x, v) => (isBytes(x.name) ? fmtBytes(v) : x.name.endsWith("_S") ? `${v} s` : String(v));

function renderConfig(list) {
  table($("#cf-table"), [
    { label: "Setting", value: (x) => h("div", {},
        h("b", { class: "mono" }, x.env || x.name), h("br"), h("span", { class: "muted small" }, x.description)) },
    { label: "Value", value: (x) => {
        const input = h("input", { type: "number", class: "mono", "data-name": x.name, step: "any",
          min: toInput(x, x.min), max: toInput(x, x.max), value: toInput(x, x.saved), "data-orig": toInput(x, x.saved) });
        input.style.width = "110px";
        input.addEventListener("input", () => {
          input.classList.toggle("changed", input.value !== input.dataset.orig);
          $("#cf-save").disabled = !$$("#cf-table input.changed").length;
        });
        return h("div", { class: "row" }, input, isBytes(x.name) ? h("span", { class: "muted small" }, "MB") : null);
      } },
    { label: "Range", value: (x) => h("span", { class: "muted small" }, `${showValue(x, x.min)} – ${showValue(x, x.max)}`) },
    { label: "Without override", value: (x) => h("span", { class: "muted small" },
        `${showValue(x, x.base)} (${x.base_source === "env" ? ".env" : "built-in default"})`) },
    { label: "Applies", value: (x) => x.pending_restart
        ? h("span", { class: "badge warn" }, `after restart (now ${showValue(x, x.value)})`)
        : h("span", { class: "badge" + (x.live ? " ok" : "") }, x.live ? "at once" : "on restart") },
    { label: "", value: (x) => {
        if (x.source !== "admin") return h("span", { class: "muted small" }, x.source === "env" ? "from .env" : "default");
        const b = h("button", { class: "small ghost", title: "Remove the admin override; use .env / default" }, "Reset");
        b.addEventListener("click", () => busy(b, () => saveConfig({ [x.name]: "" })));
        return b;
      } },
  ], list);
  $("#cf-save").disabled = true;
}

async function refreshConfig() {
  const d = await api("/admin/api/config");
  renderConfig(d.settings);
}

async function saveConfig(form) {
  const d = await api("/admin/api/config", { method: "POST", form });
  renderConfig(d.settings);
  toast(`Saved: ${Object.keys(form).join(", ")}`, "ok");
  await refreshStatus();
}

$("#cf-save").addEventListener("click", async (ev) => {
  const btn = ev.target;
  btn.disabled = true;
  try {
    const form = {};
    const list = (await api("/admin/api/config")).settings;
    for (const input of $$("#cf-table input.changed")) {
      const x = list.find((s) => s.name === input.dataset.name);
      form[x.name] = fromInput(x, input.value.trim());
    }
    if (Object.keys(form).length) await saveConfig(form);  // re-renders: no edits left
  } catch (e) {
    report(e);
    btn.disabled = !$$("#cf-table input.changed").length;
  }
});
$("#cf-reload").addEventListener("click", () => refreshConfig().catch(report));

// ---------------------------------------------------------------- developer mode
function renderDevBanner() {
  const d = status.server.dev_mode || { enabled: false };
  $("#devbanner").hidden = !d.enabled;
  if (!d.enabled) return;
  const until = d.until ? `until ${d.until}` : "with no expiry";
  const from = d.allow_from && d.allow_from.length ? ` · only from ${d.allow_from.join(", ")}` : "";
  $("#devbanner-text").textContent =
    `Since ${d.since} ${until} · switched on by ${d.by} · reason: “${d.reason}” · clients: ${(d.clients || []).join(", ")}${from} · running ${status.server.version}`;
}
armed($("#dev-off"), async () => {
  await api("/admin/api/dev-mode/off", { method: "POST" });
  toast("Developer mode switched off", "ok");
  await refreshStatus();
});

$("#mt-audit").addEventListener("click", (ev) => busy(ev.target, async () => {
  const r = await api("/admin/api/audit?lines=50");
  const rows = r.entries.map((e) => {
    const { time, event, ...rest } = e;
    const detail = Object.entries(rest).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`).join(" ");
    return h("li", {}, h("span", { class: "mono" }, time), " ", h("strong", {}, event), " ",
      h("span", { class: "muted small" }, detail));
  });
  $("#mt-audit-list").replaceChildren(rows.length ? h("ul", { class: "audit" }, rows)
    : h("p", { class: "muted" }, "No entries"));
}));

// ---------------------------------------------------------------- maintenance
function renderMaintenance() {
  if (!status) return;
  const srv = status.server;
  const installed = srv.layout === "installed";
  kv($("#mt-code"), installed ? [
    ["Running", h("span", { class: "mono" }, srv.version)],
    ["Roll back to", srv.rollback_target ? h("span", { class: "mono" }, srv.rollback_target) : "none"],
  ] : [
    ["Running", h("span", { class: "mono" }, `${srv.version} (${srv.branch})`)],
    ["Commit date", srv.commit_date],
    ["Rollback target", srv.rollback_target ? h("span", { class: "mono" }, srv.rollback_target) : "none"],
  ]);
  $("#mt-channel-row").hidden = !installed;
  if (installed && !releaseCheck) $("#mt-update").textContent = "Update to the newest release";
  if (!installed) $("#mt-update").textContent = "Update to origin/main";
  $("#mt-rollback").disabled = !srv.rollback_target;
  const pending = status.pending_restart || [];
  $("#cf-pending").hidden = !pending.length;
  $("#cf-pending").textContent = pending.length ? `Saved, takes effect after a restart: ${pending.join(", ")}` : "";
}

function commitList(title, rows) {
  return [
    h("p", { class: "commits-title" }, title),
    h("ul", { class: "commits" }, rows.map((r) =>
      h("li", { title: `${r.date} · ${r.author}` }, h("span", { class: "mono" }, r.commit), r.subject))),
  ];
}

// Installed server: the last answer of check-updates (mode "release")
let releaseCheck = null;

function renderReleases(r) {
  releaseCheck = r;
  $("#mt-channel").value = r.channel;
  const newest = r.available[0];
  $("#mt-update").textContent = newest ? `Update to ${newest.version}` : "Update to the newest release";
  $("#mt-update").disabled = !newest;
  const rows = r.available.map((x) => h("li", {},
    h("span", { class: "mono" }, x.version), x.prerelease ? " (pre-release) " : " ",
    h("span", { class: "muted small" }, (x.published || "").slice(0, 10)), " ",
    x.url ? h("a", { href: x.url, target: "_blank", rel: "noopener" }, "release page") : "",
    x.notes ? h("pre", { class: "notes" }, x.notes) : ""));
  $("#mt-pending").replaceChildren(
    ...(r.error ? [h("p", { class: "error" }, `Could not check ${r.repo}: ${r.error}`)] : []),
    newest ? h("p", { class: "commits-title" }, `${r.available.length} newer release(s) on the ${r.channel} channel`)
      : h("p", { class: "commits-title" }, `Up to date: ${r.current} is the newest ${r.channel} release`),
    ...(rows.length ? [h("ul", { class: "audit" }, rows)] : []),
    ...(r.job && r.job.state === "failed"
      ? [h("p", { class: "error" }, `Last update to ${r.job.version} failed: ${r.job.error}`)] : []),
  );
}

$("#mt-channel").addEventListener("change", async (ev) => {
  try {
    await api("/admin/api/update-channel", { method: "POST", form: { channel: ev.target.value } });
    toast(`Update channel: ${ev.target.value}`, "ok");
    renderReleases(await api("/admin/api/check-updates", { method: "POST" }));
  } catch (e) { report(e); }
});

$("#mt-check").addEventListener("click", (ev) => busy(ev.target, async () => {
  const r = await api("/admin/api/check-updates", { method: "POST" });
  if (r.mode === "release") { renderReleases(r); return; }
  const box = $("#mt-pending");
  box.replaceChildren(
    ...(r.dirty.length ? [h("p", { class: "error" }, `Working tree has local changes: ${r.dirty.join(", ")}`)] : []),
    ...(r.pending.length ? commitList(`${r.pending.length} new on origin/main`, r.pending)
      : [h("p", { class: "commits-title" }, "Up to date with origin/main")]),
    ...commitList("Recent history", r.recent),
  );
}));

// Wait until the server answers with a different start time (it restarted).
async function waitForRestart(message) {
  const before = status && status.server.started;
  $("#overlay-text").textContent = message;
  $("#overlay").hidden = false;
  const t0 = Date.now();
  try {
    while (Date.now() - t0 < 120000) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const s = await api("/admin/api/status");
        if (s.server.started !== before) {
          toast(`Server is back: ${s.server.version}`, "ok");
          await refreshStatus();
          return;
        }
      } catch (e) {
        if (e instanceof Unauthorized) return;
        $("#overlay-text").textContent = `${message} (waiting for the server…)`;
      }
    }
    toast("The server did not come back within 2 minutes — check the service log", "err");
  } finally {
    $("#overlay").hidden = true;
  }
}

// Installed server: follow the update job until it restarts the server (or fails).
async function followUpdate(version) {
  $("#overlay-text").textContent = `Updating to ${version}…`;
  $("#overlay").hidden = false;
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      let job;
      try { job = await api("/admin/api/update-status"); } catch (e) { break; }  // restarting
      $("#overlay-text").textContent = `Updating to ${version}: ${job.step}`;
      if (job.state === "failed") { toast(`Update failed: ${job.error}`, "err"); return false; }
      if (job.state === "restarting") break;
    }
  } finally {
    $("#overlay").hidden = true;
  }
  return true;
}

armed($("#mt-update"), async () => {
  const r = await api("/update", { method: "POST" });
  if (r.status === "up-to-date") { toast(`Already up to date (${r.commit})`, "ok"); return; }
  if (r.status === "started") {
    if (await followUpdate(r.new_version)) await waitForRestart(`Restarting into ${r.new_version}…`);
    return;
  }
  toast(`Updating ${r.old_commit} → ${r.new_commit}`, "ok");
  await waitForRestart(`Updating to ${r.new_commit}, restarting…`);
});
armed($("#mt-rollback"), async () => {
  const r = await api("/rollback", { method: "POST" });
  await waitForRestart(`Rolling back to ${r.version || r.commit}, restarting…`);
});
armed($("#mt-restart"), async () => {
  await api("/admin/restart", { method: "POST" });
  await waitForRestart("Restarting…");
});
armed($("#mt-recycle"), async () => {
  const r = await api("/admin/browsers/recycle", { method: "POST" });
  toast(`Recycled ${r.recycled} browser(s)`, "ok");
  await refreshStatus();
});
armed($("#mt-clear"), async () => {
  const r = await api("/admin/fonts/clear", { method: "POST" });
  toast(`Dropped ${r.deleted} cached font(s)`, "ok");
  await refreshStatus();
});
$("#mt-install").addEventListener("click", (ev) => busy(ev.target, async () => {
  const browser = $("#mt-browser").value;
  $("#overlay-text").textContent = `Installing ${browser}…`;
  $("#overlay").hidden = false;
  try {
    await api("/browsers/install", { method: "POST", form: { browser } });
    toast(`${browser} installed`, "ok");
    await refreshStatus();
  } finally {
    $("#overlay").hidden = true;
  }
}));

start();
