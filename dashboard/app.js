"use strict";

const $ = id => document.getElementById(id);
let snapshot = null;
let paused = false;
let refreshing = false;
let token = new URLSearchParams(location.hash.slice(1)).get("token") || sessionStorage.getItem("dashboard-token") || "";
if (location.hash) history.replaceState(null, "", location.pathname);
const bytes = value => {
  if (!Number.isFinite(value)) return "Unknown";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let n = 0;
  while (Math.abs(value) >= 1024 && n < units.length - 1) { value /= 1024; n++; }
  return `${value.toFixed(n ? 1 : 0)} ${units[n]}`;
};
const pct = value => value == null ? "Sampling..." : Number.isFinite(value) ? `${value.toFixed(1)}%` : "Unknown";
const when = value => value ? new Date(value).toLocaleString() : "No record yet";
const text = (id, value) => { $(id).textContent = value; };
function badge(id, value, good) {
  text(id, value);
  $(id).className = `badge ${good ? "good" : "warn"}`;
}
function row(values) {
  const tr = document.createElement("tr");
  values.forEach(value => { const td = document.createElement("td"); td.textContent = value; tr.append(td); });
  return tr;
}
function list(id, values, empty) {
  $(id).replaceChildren();
  (values.length ? values : [empty]).forEach(value => {
    const li = document.createElement("li"); li.textContent = value; $(id).append(li);
  });
}
function notice(message) { text("notice", message); $("notice").hidden = !message; }
function detailFields(fields) {
  $("detail-fields").replaceChildren();
  fields.forEach(([key, value]) => {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = key; dd.textContent = value ?? "Unknown"; $("detail-fields").append(dt, dd);
  });
}
function showDetails(kind, item) {
  const fields = kind === "Service" ? [["Unit", item.unit], ["Scope", item.scope], ["State", item.active], ["Substate", item.sub], ["Description", item.description]] :
    kind === "Process" ? [["Name", item.name], ["PID", item.pid], ["CPU", pct(item.cpu_percent)], ["RAM", bytes(item.memory_bytes)]] :
    kind === "Container" ? [["Name", item.Names], ["Engine", item.engine], ["ID", item.ID], ["Image", item.Image], ["State", item.State || item.Status]] :
    [["Run", item.run_id], ["Started", when(item.started_at)], ["Finished", when(item.ended_at)], ["Reclaimed", bytes(Number.isFinite(item.freed_kb) ? item.freed_kb * 1024 : null)], ["Safety integrity", item.safety_integrity]];
  text("detail-title", `${kind} details`);
  text("detail-observed", `Snapshot observed ${when(snapshot?.generated_at)}; this detail view is fixed until reopened.`);
  detailFields(fields);
  $("detail-dialog").showModal();
}
function inspectRow(values, kind, item) {
  const tr = row(values), button = document.createElement("button");
  button.type = "button"; button.className = "row-button"; button.textContent = values[0];
  button.addEventListener("click", () => showDetails(kind, item));
  tr.children[0].replaceChildren(button);
  return tr;
}
function liveControls() {
  text("live-toggle", paused ? "Resume updates" : "Pause updates");
  $("live-toggle").setAttribute("aria-pressed", String(paused));
  text("live-status", paused ? "Paused on this device. Refresh now fetches one snapshot." : "Live updates every 5 seconds");
}
function toggleLive() {
  paused = !paused;
  clearTimeout(refreshTimer);
  liveControls();
  if (paused && snapshot) badge("connection", "Paused", false);
  if (!paused) refresh(true);
}
function serviceRows() {
  if (!snapshot) return;
  const query = $("service-search").value.toLowerCase();
  const state = $("service-state").value;
  const all = [snapshot.system_services, snapshot.user_services].flatMap(scope => scope?.items || []);
  const rank = {failed: 0, active: 1, activating: 2};
  const sort = $("service-sort").value;
  all.sort((a, b) => (sort === "name" ? 0 : sort === "scope" ? a.scope.localeCompare(b.scope) : (rank[a.active] ?? 3) - (rank[b.active] ?? 3))
    || a.unit.localeCompare(b.unit) || a.scope.localeCompare(b.scope));
  const rows = all.filter(item => `${item.unit} ${item.description} ${item.scope}`.toLowerCase().includes(query)
    && (state === "all" || (state === "failed" ? item.active === "failed" : ["active", "failed", "activating"].includes(item.active))));
  $("services").replaceChildren(...rows.map(item => inspectRow([item.unit, item.scope, `${item.active} / ${item.sub}`, item.description], "Service", item)));
  if (!rows.length) $("services").append(row(["No matching observed services", "", "", ""]));
  const running = all.filter(item => item.active === "active").length;
  const failed = all.filter(item => item.active === "failed").length;
  text("service-count", running);
  text("failed-count", `${failed} failed / ${all.length} listed`);
  const unavailable = [!snapshot.system_services && "system", !snapshot.user_services && "user"].filter(Boolean);
  text("services-note", unavailable.length ? `Unavailable scopes: ${unavailable.join(", ")}. See observation gaps.` :
    `${rows.length} shown. ${[snapshot.system_services, snapshot.user_services].some(scope => scope?.truncated) ? "Inventory capped at 250 per scope." : "System + user scopes; no start/stop controls."}`);
}
function processRows() {
  const query = $("process-search").value.toLowerCase(), sort = $("process-sort").value;
  const rows = [...(snapshot?.processes?.items || [])].filter(item => `${item.name} ${item.pid}`.toLowerCase().includes(query));
  rows.sort((a, b) => sort === "name" ? a.name.localeCompare(b.name) :
    (sort === "memory" ? b.memory_bytes - a.memory_bytes : (b.cpu_percent ?? -1) - (a.cpu_percent ?? -1)) || a.pid - b.pid);
  $("processes").replaceChildren(...rows.map(item => inspectRow([item.name, item.pid, pct(item.cpu_percent), bytes(item.memory_bytes)], "Process", item)));
  if (!rows.length) $("processes").append(row([snapshot?.processes ? "No matching displayed processes" : "Process source unavailable", "", "", ""]));
}
function containerRows() {
  $("containers").replaceChildren();
  const query = $("container-search").value.toLowerCase(), engine = $("container-engine").value || "all", state = $("container-state").value || "all";
  ["docker", "podman"].filter(tool => engine === "all" || tool === engine).forEach(tool => {
    const inventory = snapshot?.[tool];
    const heading = document.createElement("h3"); heading.textContent = tool === "docker" ? "Docker" : "Podman";
    $("containers").append(heading);
    if (!inventory || inventory.status !== "available") {
      const p = document.createElement("p"); p.className = "muted";
      p.textContent = !inventory ? "Observation failed" : inventory.status === "not_installed" ? "Not installed" : inventory.detail || inventory.status;
      $("containers").append(p); return;
    }
    const rows = inventory.items.filter(item => {
      const running = item.State === "running" || (!item.State && item.Status.startsWith("Up"));
      return `${item.Names} ${item.Image}`.toLowerCase().includes(query)
        && (state === "all" || (state === "running" ? running : !running));
    });
    rows.forEach(item => {
      const div = document.createElement("div"); div.className = "workload";
      const name = document.createElement("button"), detail = document.createElement("p");
      name.type = "button"; name.className = "row-button"; name.textContent = item.Names;
      name.addEventListener("click", () => showDetails("Container", {...item, engine: tool}));
      detail.className = "muted"; detail.textContent = `${item.State || item.Status} / ${item.Image}`;
      div.append(name, detail); $("containers").append(div);
    });
    if (!rows.length) {
      const p = document.createElement("p"); p.className = "muted";
      p.textContent = inventory.items.length ? "No containers match these filters." : "No containers.";
      $("containers").append(p);
    }
    if (inventory.truncated) {
      const p = document.createElement("p"); p.className = "muted";
      p.textContent = "Inventory capped at 100 containers; additional containers are not shown."; $("containers").append(p);
    }
  });
}
function historyRows() {
  const points = snapshot?.history || [], minutes = Number($("history-window").value);
  if (!minutes || !points.length) return points;
  const cutoff = Date.parse(points[points.length - 1].ts) - minutes * 60000;
  return points.filter(point => Date.parse(point.ts) >= cutoff);
}
function drawHistory() {
  $("chart").replaceChildren();
  const points = historyRows();
  [["cpu", "#88e0b1"], ["memory", "#81b9ff"], ["disk", "#f0bf74"]].forEach(([key, color]) => {
    let segment = [];
    const flush = () => {
      if (segment.length) {
        const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
        line.setAttribute("points", segment.join(" ")); line.setAttribute("fill", "none");
        line.setAttribute("stroke", color); line.setAttribute("stroke-width", "2");
        $("chart").append(line); segment = [];
      }
    };
    points.forEach((point, index) => {
      if (point[key] == null) return flush();
      segment.push(`${index * 1000 / Math.max(1, points.length - 1)},${128 - Math.max(0, Math.min(100, point[key])) * 1.25}`);
    });
    flush();
  });
  text("history-note", `${points.length} observations shown since ${when(points[0]?.ts)}. ${(snapshot?.history || []).length} / 360 held in memory; resets on restart.`);
}
function render(value) {
  snapshot = value;
  $("content").hidden = false;
  $("login").hidden = true;
  text("host", `${value.host} / private, read-only monitoring`);
  badge("connection", paused ? "Paused" : value.status === "healthy" ? "Live" : "Partial data", !paused && value.status === "healthy");
  liveControls();
  text("updated", `Observed ${new Date(value.generated_at).toLocaleTimeString()}`);
  const system = value.system || {};
  const root = (value.filesystems || [])[0];
  text("disk-free", root ? bytes(root.available_bytes) : "Unknown");
  text("disk-detail", root ? `${pct(root.used_percent)} used / ${bytes(root.total_bytes)} total` : "Storage source unavailable");
  $("disk-meter").value = root?.used_percent || 0;
  text("cpu", pct(system.cpu_percent));
  text("load", system.load_average ? `${system.cpu_count} cores / load ${system.load_average.map(n => n.toFixed(2)).join(" / ")}` : "CPU source unavailable");
  $("cpu-meter").value = system.cpu_percent || 0;
  text("memory", system.memory_used_percent == null ? "Unknown" : pct(system.memory_used_percent));
  text("memory-detail", `${bytes(system.memory_available_bytes)} available / ${bytes(system.memory_total_bytes)}`);
  $("memory-meter").value = system.memory_used_percent || 0;
  const uptime = system.uptime_seconds;
  text("uptime", uptime == null ? "Unknown" : `${Math.floor(uptime / 86400)}d ${Math.floor(uptime / 3600) % 24}h`);
  text("swap", `Swap ${bytes(system.swap_used_bytes)} / ${bytes(system.swap_total_bytes)}`);
  const maintenance = value.janitor?.maintenance;
  text("cache", bytes(maintenance?.cache_bytes));
  text("cache-detail", `${bytes(maintenance?.cache_budget_bytes)} soft budget / ${when(maintenance?.generated_at)}`);
  badge("maintenance-status", maintenance?.status || "Unknown", !!maintenance && !["error"].includes(maintenance.status));
  const timer = value.janitor?.timer || {};
  const summary = [
    ["Monitor timer", timer.ActiveState || "Unknown"],
    ["Next check", timer.NextElapseUSecRealtime || "Unknown"],
    ["Last cleanup", when(maintenance?.last_cleanup_at)],
    ["Cooldown remaining", maintenance ? `${maintenance.cooldown_remaining_sec}s` : "Unknown"],
    ["New builds queued", maintenance?.build_admission?.waiting?.length ?? "Unknown"],
    ["Last run reclaimed", maintenance ? bytes(maintenance.observed_reclaimed_bytes) : "Unknown"],
    ["Native Janitor", when(value.janitor?.last_run?.ended_at)],
    ["Native run reclaimed", bytes(Number.isFinite(value.janitor?.last_run?.freed_kb) ? value.janitor.last_run.freed_kb * 1024 : null)],
  ];
  $("maintenance-summary").replaceChildren();
  summary.forEach(([key, data]) => {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = key; dd.textContent = data; $("maintenance-summary").append(dt, dd);
  });
  const actions = (maintenance?.actions || []).map(action => `${action.path || action.kind || "Cleanup"}: ${action.removed_bytes == null ? "completed; size unknown" : bytes(action.removed_bytes)}`);
  text("maintenance-actions", actions.length ? actions.join(" / ") : "No deletion in the latest maintenance check. Busy or unbacked work stays protected.");
  $("cleanup-details").disabled = !value.janitor?.last_run;
  list("protected", (maintenance?.workspaces || []).filter(item => item.status !== "removed").map(item => `${item.path}: ${item.status}`), "No protected workspace records in the latest scan.");
  $("filesystems").replaceChildren(...(value.filesystems || []).map(disk => row([disk.mount, bytes(disk.available_bytes), pct(disk.used_percent), disk.inode_used_percent == null ? "Unknown" : pct(disk.inode_used_percent)])));
  if (!value.filesystems) $("filesystems").append(row(["Storage source unavailable", "", "", ""]));
  $("network").replaceChildren();
  (value.network || []).filter(item => item.received_bytes_per_second || item.sent_bytes_per_second || !item.name.startsWith("veth")).slice(0, 8).forEach(item => {
    const p = document.createElement("p"); p.className = "muted";
    p.textContent = `${item.name}: ${bytes(item.received_bytes_per_second)}/s in / ${bytes(item.sent_bytes_per_second)}/s out`; $("network").append(p);
  });
  if (!value.network) text("network", "Network source unavailable");
  processRows();
  text("process-note", value.processes ? `${value.processes.total_observed} processes observed; ${value.processes.unreadable} unreadable. CPU is per process and may exceed 100%. Arguments are never read.` : "Process source unavailable.");
  containerRows();
  $("errors-panel").hidden = !value.errors.length;
  list("errors", value.errors.map(error => `${error.source}: ${error.detail}`), "No observation errors.");
  drawHistory();
  serviceRows();
}
let refreshTimer;
async function refresh(manual = false) {
  if (refreshing || (paused && !manual)) return;
  refreshing = true;
  $("refresh-now").disabled = true;
  let retry = true;
  clearTimeout(refreshTimer);
  try {
    const response = await fetch("/api/snapshot", {headers: {Authorization: `Bearer ${token}`}, cache: "no-store", signal: AbortSignal.timeout(10000)});
    if (response.status === 401) {
      sessionStorage.removeItem("dashboard-token"); $("login").hidden = false;
      retry = false;
      badge("connection", "Access required", false); notice("Access token missing or invalid. Your previous observations, if any, are stale."); return;
    }
    if (!response.ok) throw new Error(`Snapshot unavailable (HTTP ${response.status})`);
    const value = await response.json();
    if (paused && !manual) return;
    const age = (Date.now() - new Date(value.generated_at).getTime()) / 1000;
    render(value);
    if (age > (value.stale_after_seconds || 20)) { badge("connection", "Stale data", false); notice(`Last observation is ${Math.round(age)} seconds old; the collector may be delayed.`); }
    else notice("");
    sessionStorage.setItem("dashboard-token", token);
  } catch (error) {
    badge("connection", "Disconnected / stale", false);
    notice(`${error.message}. Retaining the last observation; retrying automatically.`);
  } finally {
    refreshing = false;
    $("refresh-now").disabled = false;
    if (retry && !paused) refreshTimer = setTimeout(() => refresh(), 5000);
  }
}
$("login").addEventListener("submit", event => { event.preventDefault(); token = $("token").value.trim(); $("token").value = ""; refresh(true); });
$("refresh-now").addEventListener("click", () => refresh(true));
$("live-toggle").addEventListener("click", toggleLive);
$("service-search").addEventListener("input", serviceRows);
$("service-state").addEventListener("change", serviceRows);
$("service-sort").addEventListener("change", serviceRows);
$("process-search").addEventListener("input", processRows);
$("process-sort").addEventListener("change", processRows);
$("container-search").addEventListener("input", containerRows);
$("container-engine").addEventListener("change", containerRows);
$("container-state").addEventListener("change", containerRows);
$("history-window").addEventListener("change", drawHistory);
$("detail-close").addEventListener("click", () => $("detail-dialog").close());
$("cleanup-details").addEventListener("click", () => { if (snapshot?.janitor?.last_run) showDetails("Cleanup", snapshot.janitor.last_run); });
refresh();
