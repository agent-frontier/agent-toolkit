"use strict";

const $ = id => document.getElementById(id);
let snapshot = null;
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
function serviceRows() {
  if (!snapshot) return;
  const query = $("service-search").value.toLowerCase();
  const state = $("service-state").value;
  const all = [snapshot.system_services, snapshot.user_services].flatMap(scope => scope?.items || []);
  const rank = {failed: 0, active: 1, activating: 2};
  all.sort((a, b) => (rank[a.active] ?? 3) - (rank[b.active] ?? 3)
    || a.unit.localeCompare(b.unit) || a.scope.localeCompare(b.scope));
  const rows = all.filter(item => `${item.unit} ${item.description} ${item.scope}`.toLowerCase().includes(query)
    && (state === "all" || (state === "failed" ? item.active === "failed" : ["active", "failed", "activating"].includes(item.active))));
  $("services").replaceChildren(...rows.map(item => row([item.unit, item.scope, `${item.active} / ${item.sub}`, item.description])));
  if (!rows.length) $("services").append(row(["No matching observed services", "", "", ""]));
  const running = all.filter(item => item.active === "active").length;
  const failed = all.filter(item => item.active === "failed").length;
  text("service-count", running);
  text("failed-count", `${failed} failed / ${all.length} listed`);
  const unavailable = [!snapshot.system_services && "system", !snapshot.user_services && "user"].filter(Boolean);
  text("services-note", unavailable.length ? `Unavailable scopes: ${unavailable.join(", ")}. See observation gaps.` :
    `${rows.length} shown. ${[snapshot.system_services, snapshot.user_services].some(scope => scope?.truncated) ? "Inventory capped at 250 per scope." : "System + user scopes; no start/stop controls."}`);
}
function render(value) {
  snapshot = value;
  $("content").hidden = false;
  $("login").hidden = true;
  text("host", `${value.host} / private, read-only monitoring`);
  badge("connection", value.status === "healthy" ? "Live" : "Partial data", value.status === "healthy");
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
    ["Daily Janitor", when(value.janitor?.last_run?.ended_at)],
  ];
  $("maintenance-summary").replaceChildren();
  summary.forEach(([key, data]) => {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = key; dd.textContent = data; $("maintenance-summary").append(dt, dd);
  });
  const actions = (maintenance?.actions || []).map(action => `${action.path || action.kind || "Cleanup"}: ${action.removed_bytes == null ? "completed; size unknown" : bytes(action.removed_bytes)}`);
  text("maintenance-actions", actions.length ? actions.join(" / ") : "No deletion in the latest maintenance check. Busy or unbacked work stays protected.");
  list("protected", (maintenance?.workspaces || []).filter(item => item.status !== "removed").map(item => `${item.path}: ${item.status}`), "No protected workspace records in the latest scan.");
  $("filesystems").replaceChildren(...(value.filesystems || []).map(disk => row([disk.mount, bytes(disk.available_bytes), pct(disk.used_percent), disk.inode_used_percent == null ? "Unknown" : pct(disk.inode_used_percent)])));
  if (!value.filesystems) $("filesystems").append(row(["Storage source unavailable", "", "", ""]));
  $("network").replaceChildren();
  (value.network || []).filter(item => item.received_bytes_per_second || item.sent_bytes_per_second || !item.name.startsWith("veth")).slice(0, 8).forEach(item => {
    const p = document.createElement("p"); p.className = "muted";
    p.textContent = `${item.name}: ${bytes(item.received_bytes_per_second)}/s in / ${bytes(item.sent_bytes_per_second)}/s out`; $("network").append(p);
  });
  if (!value.network) text("network", "Network source unavailable");
  $("processes").replaceChildren(...(value.processes?.items || []).map(item => row([item.name, item.pid, pct(item.cpu_percent), bytes(item.memory_bytes)])));
  text("process-note", value.processes ? `${value.processes.total_observed} processes observed; ${value.processes.unreadable} unreadable. CPU is per process and may exceed 100%. Arguments are never read.` : "Process source unavailable.");
  $("containers").replaceChildren();
  ["docker", "podman"].forEach(tool => {
    const inventory = value[tool];
    if (!inventory || inventory.status !== "available" || !inventory.items.length) {
      const p = document.createElement("p"); p.className = "muted";
      p.textContent = `${tool}: ${!inventory ? "observation failed" : inventory.status === "not_installed" ? "not installed" : inventory.status === "socket_unavailable" ? inventory.detail : "no running containers"}`;
      $("containers").append(p);
    } else inventory.items.forEach(item => {
      const div = document.createElement("div"); div.className = "workload";
      const name = document.createElement("strong"), detail = document.createElement("p");
      name.textContent = `${item.Names} / ${tool}`; detail.className = "muted"; detail.textContent = `${item.Image} / ${item.Status}`;
      div.append(name, detail); $("containers").append(div);
    });
    if (inventory?.truncated) {
      const note = document.createElement("p"); note.className = "muted";
      note.textContent = `${tool}: inventory capped at 100 containers; additional workloads are not shown.`;
      $("containers").append(note);
    }
  });
  $("errors-panel").hidden = !value.errors.length;
  list("errors", value.errors.map(error => `${error.source}: ${error.detail}`), "No observation errors.");
  $("chart").replaceChildren();
  const history = value.history || [];
  [["cpu", "#88e0b1"], ["memory", "#81b9ff"], ["disk", "#f0bf74"]].forEach(([key, color]) => {
    let points = [];
    const flush = () => {
      if (points.length) {
        const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
        line.setAttribute("points", points.join(" ")); line.setAttribute("fill", "none");
        line.setAttribute("stroke", color); line.setAttribute("stroke-width", "2");
        $("chart").append(line); points = [];
      }
    };
    history.forEach((point, index) => {
      if (point[key] == null) return flush();
      points.push(`${index * 1000 / Math.max(1, history.length - 1)},${128 - Math.max(0, Math.min(100, point[key])) * 1.25}`);
    });
    flush();
  });
  text("history-note", `${history.length} / 360 in-memory observations since ${when(history[0]?.ts)}. History resets on restart; sampling is best-effort.`);
  serviceRows();
}
let refreshTimer;
async function refresh() {
  clearTimeout(refreshTimer);
  try {
    const response = await fetch("/api/snapshot", {headers: {Authorization: `Bearer ${token}`}, cache: "no-store", signal: AbortSignal.timeout(10000)});
    if (response.status === 401) {
      sessionStorage.removeItem("dashboard-token"); $("login").hidden = false;
      badge("connection", "Access required", false); notice("Access token missing or invalid. Your previous observations, if any, are stale."); return;
    }
    if (!response.ok) throw new Error(`Snapshot unavailable (HTTP ${response.status})`);
    const value = await response.json();
    const age = (Date.now() - new Date(value.generated_at).getTime()) / 1000;
    render(value);
    if (age > (value.stale_after_seconds || 20)) { badge("connection", "Stale data", false); notice(`Last observation is ${Math.round(age)} seconds old; the collector may be delayed.`); }
    else notice("");
    sessionStorage.setItem("dashboard-token", token);
  } catch (error) {
    badge("connection", "Disconnected / stale", false);
    notice(`${error.message}. Retaining the last observation; retrying automatically.`);
  }
  refreshTimer = setTimeout(refresh, 5000);
}
$("login").addEventListener("submit", event => { event.preventDefault(); token = $("token").value.trim(); $("token").value = ""; refresh(); });
$("service-search").addEventListener("input", serviceRows);
$("service-state").addEventListener("change", serviceRows);
if (token) refresh();
