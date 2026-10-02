"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const r = await fetch(path, init);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(typeof data === "string" ? data : JSON.stringify(data.detail ?? data, null, 1));
  return data;
}

const fmtBytes = (n) => {
  if (n == null) return "-";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i ? 2 : 0)} ${u[i]}`;
};
const fmtRate = (b) => (b == null ? "-" : `${((b * 8) / 1e6).toFixed(1)} Mbps`);
const fmtTs = (ns) => (ns ? new Date(ns / 1e6).toLocaleTimeString() : "-");
const dot = (s) => `<span class="dot s-${esc(s)}" title="${esc(s)}"></span>${esc(s)}`;
const linkCell = (d) => {
  const v = d?.link_speed_mbps;
  if (!v) return '<span class="muted">-</span>';
  const exp = d.expected_link_mbps || 1000;
  return `<span class="${v < exp ? "bad" : "good"}">${v} Mbps</span>`;
};

// ------------------------------------------------------------------ tabs
let currentTab = "overview";
$$("nav button").forEach((b) => b.addEventListener("click", () => {
  $$("nav button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
  currentTab = b.dataset.tab;
  refresh();
}));

// ------------------------------------------------------------------ overview
let lastOverview = { nodes: [], streams: [] };

function renderOverview(ov) {
  lastOverview = ov;
  $("#prefix").textContent = ov.prefix;
  const nodes = ov.nodes;
  const online = nodes.filter((n) => n.alive).length;
  const bw = ov.streams.reduce((a, s) => a + (s.bytes_per_s || 0), 0);
  const bad = nodes.filter((n) => ["error", "degraded", "stale"].includes(n.state)).length;
  const slow = nodes.filter((n) => n.device?.link_speed_mbps && n.device.link_speed_mbps < (n.device.expected_link_mbps || 1000)).length;
  $("#summary").innerHTML = [
    ["在线实例", `${online} / ${nodes.length}`], ["数据通道", ov.streams.length],
    ["总带宽", fmtRate(bw)], ["异常", bad], ["链路降速", slow],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  $("#nodes tbody").innerHTML = nodes.map((n) => `<tr>
    <td>${dot(n.state)}${n.reason ? ` <span class="muted">${esc(n.reason)}</span>` : ""}</td>
    <td><span class="editable" data-rel="${esc(n.rel)}">${esc(n.alias) || '<span class="muted">设置别名</span>'}</span></td>
    <td><span class="editable" data-rel="${esc(n.rel)}">${esc(n.note) || '<span class="muted">-</span>'}</span>${n.location ? ` <span class="pill">${esc(n.location)}</span>` : ""}</td>
    <td>${esc(n.kind)}</td><td>${esc(n.name)}</td><td>${esc(n.host || "-")}</td>
    <td>${linkCell(n.device)}</td>
    <td>${n.shm_active == null ? "-" : n.shm_active ? '<span class="good">是</span>' : '<span class="warn">否</span>'}</td>
    <td>${n.last_status_age_s == null ? "-" : `${n.last_status_age_s}s 前`}</td>
    <td>${n.alive ? `<button class="small" data-ctrl="${esc(n.rel)}">控制</button>` : ""}</td></tr>`).join("");

  $("#streams tbody").innerHTML = ov.streams.map((s) => {
    const lowRate = s.target_hz && s.rate_hz < s.target_hz * 0.9;
    const lat = s.latency_ms ? `${s.latency_ms.p50} / ${s.latency_ms.max} ms` : "-";
    const sync = s.sync_err_ms ? `${s.sync_err_ms.p95} ms` : "-";
    return `<tr><td>${esc(s.alias || s.name)}</td><td><code>${esc(s.key)}</code></td><td>${esc(s.encoding)}</td>
      <td class="${lowRate ? "warn" : ""}">${s.rate_hz} / ${s.target_hz ?? "-"} Hz</td>
      <td>${fmtBytes(s.msg_bytes)}</td><td>${fmtRate(s.bytes_per_s)}</td><td>${lat}</td><td>${sync}</td>
      <td class="${s.gaps ? "warn" : ""}">${s.gaps ?? 0}</td><td>${s.count}</td>
      <td><button class="small" data-probe="${esc(s.key)}">接收端测速</button></td></tr>`;
  }).join("");
  fillCtrlSelect(nodes);
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-rel],[data-ctrl],[data-probe]");
  if (!t) return;
  if (t.dataset.ctrl) { $('nav button[data-tab="control"]').click(); $("#ctrl-node").value = t.dataset.ctrl; loadCtrl(); return; }
  if (t.dataset.probe) {
    const box = $("#probe-result"); box.classList.remove("hidden");
    box.textContent = `正在从控制台订阅 ${t.dataset.probe} 3 秒（只读帧头）…`;
    try { box.textContent = JSON.stringify(await api(`/api/probe?key=${encodeURIComponent(t.dataset.probe)}&seconds=3`), null, 2); }
    catch (e) { box.textContent = e.message; }
    return;
  }
  if (t.classList.contains("editable")) openAlias(t.dataset.rel);
});

function openAlias(rel) {
  const n = lastOverview.nodes.find((x) => x.rel === rel) || {};
  $("#alias-title").textContent = rel;
  $("#alias-in").value = n.alias || ""; $("#note-in").value = n.note || ""; $("#loc-in").value = n.location || "";
  const dlg = $("#alias-dlg");
  dlg.returnValue = "";
  dlg.showModal();
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    await api(`/api/aliases/${rel}`, { method: "POST", body: { alias: $("#alias-in").value, note: $("#note-in").value, location: $("#loc-in").value } });
    refresh();
  };
}

// ------------------------------------------------------------------ control
let ctrlSpecs = [];
function fillCtrlSelect(nodes) {
  const sel = $("#ctrl-node"); const cur = sel.value;
  const opts = nodes.filter((n) => n.alive).map((n) => `<option value="${esc(n.rel)}">${esc(n.alias ? `${n.alias} (${n.rel})` : n.rel)}</option>`).join("");
  if (sel.dataset.sig !== opts) { sel.innerHTML = opts; sel.dataset.sig = opts; if (cur) sel.value = cur; }
}

async function ctrl(op, params) {
  const [kind, name] = $("#ctrl-node").value.split("/");
  return api(`/api/ctrl/${kind}/${name}`, { method: "POST", body: { op, params } });
}

async function loadCtrl() {
  const out = $("#ctrl-result");
  try {
    const r = await ctrl("describe");
    if (!r.ok) throw new Error(r.error);
    ctrlSpecs = r.result.params;
    let group = null; let html = "";
    for (const p of ctrlSpecs) {
      if ((p.group || "") !== group) { group = p.group || ""; if (group) html += `<div class="group-title">${esc(group)}</div>`; }
      const id = `p-${p.name}`; const dis = p.readonly ? "disabled" : "";
      let input;
      if (p.type === "enum") input = `<select id="${id}" ${dis}>${p.choices.map((c) => `<option ${String(c) === String(p.value) ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
      else if (p.type === "bool") input = `<select id="${id}" ${dis}><option ${p.value ? "selected" : ""}>true</option><option ${!p.value ? "selected" : ""}>false</option></select>`;
      else input = `<input id="${id}" value="${esc(p.value)}" ${dis} ${p.min != null ? `min="${p.min}"` : ""} ${p.max != null ? `max="${p.max}"` : ""} type="${["int", "float"].includes(p.type) ? "number" : "text"}" step="${p.step ?? "any"}"/>`;
      html += `<label data-name="${esc(p.name)}">${esc(p.label || p.name)} ${p.unit ? `<span class="muted">(${esc(p.unit)})</span>` : ""}${p.live ? "" : " ⟳"}
        ${input}<span class="help">${esc(p.help || "")}${p.min != null || p.max != null ? ` 范围 ${p.min ?? ""}~${p.max ?? ""}` : ""}</span></label>`;
    }
    $("#ctrl-form").innerHTML = html || '<span class="muted">该实例没有可调参数</span>';
    $$("#ctrl-form input, #ctrl-form select").forEach((el) => el.addEventListener("change", () => el.closest("label").classList.add("changed")));
    $("#ctrl-ops").innerHTML = (r.result.ops || []).map((o) => `<button class="small" data-op="${esc(o)}">${esc(o)}</button>`).join(" ");
    $$("#ctrl-ops [data-op]").forEach((b) => b.addEventListener("click", async () => { out.textContent = JSON.stringify(await ctrl(b.dataset.op, {}), null, 2); }));
    out.textContent = "已读取参数";
  } catch (e) { out.textContent = `读取失败: ${e.message}`; }
}

$("#ctrl-load").addEventListener("click", loadCtrl);
$("#ctrl-node").addEventListener("change", loadCtrl);
$("#ctrl-apply").addEventListener("click", async (ev) => {
  ev.preventDefault();
  const changes = {};
  $$("#ctrl-form label.changed").forEach((l) => { changes[l.dataset.name] = $("input,select", l).value; });
  if (!Object.keys(changes).length) { $("#ctrl-result").textContent = "没有修改"; return; }
  const r = await ctrl("set_params", changes);
  $("#ctrl-result").textContent = JSON.stringify(r, null, 2);
  if (r.ok) loadCtrl();
});

// ------------------------------------------------------------------ config
async function loadConfig() {
  const c = await api("/api/config");
  $("#cfg-text").value = c.content;
  $("#cfg-ver").textContent = c.version ? `v${c.version}` : "未入库";
  $("#cfg-text").dataset.sha = c.sha256;
  await loadHistory();
}
async function loadHistory() {
  const h = await api("/api/config/history");
  $("#cfg-hist tbody").innerHTML = h.map((v) => `<tr>
    <td>v${v.version}</td><td>${new Date(v.ts * 1000).toLocaleString()}</td><td>${esc(v.comment)}</td>
    <td>${v.tags.map((t) => `<span class="pill">${esc(t)}</span>`).join(" ")}</td>
    <td><button class="small" data-v="${v.version}" data-a="view">查看</button>
        <button class="small" data-v="${v.version}" data-a="diff">对比当前</button>
        <button class="small" data-v="${v.version}" data-a="tag">${v.tags.includes("known_good") ? "取消正确标记" : "标记正确"}</button>
        <button class="small" data-v="${v.version}" data-a="rollback">回滚</button></td></tr>`).join("");
}
$("#cfg-hist").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-a]"); if (!b) return;
  const v = b.dataset.v; const box = $("#cfg-diff");
  try {
    if (b.dataset.a === "view") box.textContent = await api(`/api/config/history/${v}`);
    if (b.dataset.a === "diff") box.textContent = (await api(`/api/config/diff?a=${v}`)) || "与当前一致";
    if (b.dataset.a === "tag") { const remove = b.textContent.includes("取消"); await api(`/api/config/tag/${v}`, { method: "POST", body: { tag: "known_good", remove } }); loadHistory(); }
    if (b.dataset.a === "rollback" && confirm(`确认回滚到 v${v}？当前内容会先保存为新版本。`)) {
      const r = await api(`/api/config/rollback/${v}`, { method: "POST", body: {} });
      $("#cfg-msg").textContent = `已回滚：${JSON.stringify(r)}`; loadConfig();
    }
  } catch (e) { box.textContent = e.message; }
});
$("#cfg-reload").addEventListener("click", loadConfig);
$("#cfg-validate").addEventListener("click", async () => {
  const r = await api("/api/config/validate", { method: "POST", body: { content: $("#cfg-text").value } });
  $("#cfg-msg").textContent = r.ok ? "✔ 校验通过" : `✘ ${r.errors.join("\n✘ ")}`;
});
$("#cfg-save").addEventListener("click", async () => {
  const comment = $("#cfg-comment").value.trim();
  if (!comment) { $("#cfg-msg").textContent = "请填写修改说明"; return; }
  try {
    const r = await api("/api/config", { method: "POST", body: { content: $("#cfg-text").value, comment } });
    $("#cfg-msg").textContent = r.changed ? `✔ 已保存为 v${r.version}，并已通知各节点（cfg/changed）` : "内容未变化";
    $("#cfg-comment").value = ""; loadConfig();
  } catch (e) { $("#cfg-msg").textContent = `✘ 保存失败：${e.message}`; }
});

// ------------------------------------------------------------------ network / events / system
async function loadNet() {
  const r = await api("/api/net");
  const rows = [...r.nodes];
  for (const d of r.netprobe?.data?.devices || []) rows.push({ ...d, rel: d.name || d.ip, source: d.source || "netprobe" });
  $("#net tbody").innerHTML = rows.map((d) => `<tr><td>${esc(d.rel)}</td><td>${esc(d.alias || "")}</td><td>${esc(d.ip || "-")}</td>
    <td>${esc(d.mac || "-")}</td><td>${linkCell(d)}</td><td>${d.rtt_ms != null ? `${d.rtt_ms} ms` : "-"}</td>
    <td>${esc(d.source)}</td><td>${d.state ? dot(d.state) : "-"}</td></tr>`).join("");
}
async function loadEvents() {
  const ev = await api("/api/events?limit=300");
  $("#events tbody").innerHTML = ev.map((e) => `<tr><td>${fmtTs(e.ts_ns)}</td>
    <td class="${e.level === "error" ? "bad" : e.level === "warn" ? "warn" : ""}">${esc(e.level)}</td>
    <td>${esc(e.key)}</td><td>${esc(e.msg)} ${e.reason ? `<span class="muted">${esc(e.reason)}</span>` : ""}</td></tr>`).join("");
}
async function loadSystem() {
  const [info, router] = await Promise.all([api("/api/info"), api("/api/router")]);
  $("#router").textContent = JSON.stringify({ console: info, routers: router }, null, 2);
}

// ------------------------------------------------------------------ loop
let configLoaded = false;
async function refresh() {
  try {
    if (currentTab === "overview" || currentTab === "control") renderOverview(await api("/api/overview"));
    if (currentTab === "config" && !configLoaded) { configLoaded = true; await loadConfig(); }
    if (currentTab === "network") await loadNet();
    if (currentTab === "events") await loadEvents();
    if (currentTab === "system") await loadSystem();
    $("#conn").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
  } catch (e) { $("#conn").textContent = `连接失败: ${e.message}`; }
}
setInterval(() => { if (currentTab !== "config" && currentTab !== "control") refresh(); }, 1000);
refresh();
