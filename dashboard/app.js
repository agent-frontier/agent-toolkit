"use strict";

const $ = id => document.getElementById(id);
let snapshot = null;
let snapshotReceipt = null;
let paused = false;
let refreshing = false;
let detailTarget = null;
let pendingAction = null;
let actionBusy = false;
const CLEANUP_UNIT = "system-janitor-catchup.service";
const cleanupRunning = new Set(["active", "activating", "deactivating", "reloading", "refreshing"]);
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
function cleanupFeature() {
  const actions = snapshot?.actions, control = actions?.cleanup;
  return actions?.enabled === true && Array.isArray(actions.capabilities) &&
    ["guarded-cleanup", "cleanup-job-state"].every(name => actions.capabilities.includes(name)) &&
    control?.kind === "cleanup" && control.action === "run" && control.unit === CLEANUP_UNIT &&
    control.scope === "user" && control.dispatch_only === true;
}
function receiptFor(response, requestedAt) {
  const receivedAt = performance.now(), date = response.headers?.get?.("Date");
  const serverTime = typeof date === "string" ? Date.parse(date) : NaN;
  if (!Number.isFinite(serverTime) || new Date(serverTime).toUTCString() !== date ||
      !Number.isFinite(requestedAt) || !Number.isFinite(receivedAt) ||
      requestedAt < 0 || receivedAt < requestedAt) return null;
  return {serverTime, requestedAt, receivedAt};
}
function observationAge(timestamp) {
  const receipt = snapshotReceipt, source = typeof timestamp === "string" ? Date.parse(timestamp) : NaN;
  if (!receipt || receipt.snapshot !== snapshot || !Number.isFinite(source) ||
      ![receipt.serverTime, receipt.requestedAt, receipt.receivedAt].every(Number.isFinite)) return NaN;
  const elapsed = performance.now() - receipt.receivedAt;
  const transit = receipt.receivedAt - receipt.requestedAt, initial = receipt.serverTime - source;
  if (!Number.isFinite(elapsed) || elapsed < 0 || transit < 0 || initial < -1000) return NaN;
  // HTTP Date has second precision. Include that uncertainty and the full request
  // transit conservatively; phone wall-clock offsets/jumps never grant freshness.
  return (initial + 1000 + transit + elapsed) / 1000;
}
function freshCleanup(service = snapshot?.janitor?.service) {
  const age = observationAge(service?.read_at), cacheAge = observationAge(snapshot?.generated_at);
  const limit = snapshot?.stale_after_seconds || 20;
  return service?.status === "available" && service.unit === CLEANUP_UNIT &&
    service.id === CLEANUP_UNIT && service.scope === "user" && service.load_state === "loaded" &&
    Number.isFinite(age) && age >= 0 && age <= limit &&
    Number.isFinite(cacheAge) && cacheAge >= 0 && cacheAge <= limit;
}
function cleanupAllowed() {
  const service = snapshot?.janitor?.service;
  return cleanupFeature() && freshCleanup(service) && service.can_start === true &&
    service.refuse_manual_start === false && ["inactive", "failed"].includes(service.active);
}
function cleanupPolicy() {
  if (!snapshot?.actions?.enabled) return "Read-only mode. Homelab controls are not enabled.";
  if (!cleanupFeature()) return "Guarded cleanup is not supported by this backend's control metadata.";
  const service = snapshot?.janitor?.service;
  if (service?.status === "unavailable") return `${CLEANUP_UNIT} is unavailable (missing, masked or unusable). No fallback cleanup.`;
  if (!Number.isFinite(observationAge(snapshot?.generated_at)) || !Number.isFinite(observationAge(service?.read_at))) return "Server-relative snapshot/unit age evidence is unavailable. Refresh before scheduling; no fallback cleanup.";
  if (!freshCleanup(service)) return "Guarded runner state is unknown or stale. Refresh before scheduling; no fallback cleanup.";
  if (cleanupRunning.has(service.active)) return "Guarded runner is busy or changing state. Observe it; no duplicate run will be scheduled.";
  if (!cleanupAllowed()) return "The guarded user unit cannot be started under its current policy.";
  return "Schedules only the existing guarded user unit. Leases, budgets, cooldown and active-work protections stay unchanged; work may defer or reclaim zero bytes. No force cleanup.";
}
function unitObservation(service) {
  if (!service) return `${CLEANUP_UNIT}: unknown; no unit observation.`;
  if (service.status !== "available") return `${CLEANUP_UNIT}: ${service.status || "unknown"}; ${service.detail || "state unavailable"}; read ${when(service.read_at)}.`;
  return `${CLEANUP_UNIT}: ${service.load_state} / ${service.active} / ${service.sub}; result ${service.result ?? "Unknown"}; exit ${service.exit_status ?? "Unknown"}; invocation ${service.invocation_id || "unverified"}; read ${when(service.read_at)}.`;
}
function maintenanceFields(item) {
  const service = item.service, saved = item.maintenance, native = item.last_run, timer = item.timer || {};
  const linked = item.maintenance_phase_linked === true && /^[0-9a-f]{32}$/.test(service?.invocation_id || "") &&
    service.invocation_id === saved?.service_invocation_id;
  return [
    ["Guarded user unit", CLEANUP_UNIT],
    ["Current unit observation", unitObservation(service)],
    ["Monitor timer", timer.ActiveState || "Unknown"],
    ["Next check", timer.NextElapseUSecRealtime || "Unknown"],
    ["Saved budget phase", `${saved?.status || "Unknown"} / ${when(saved?.generated_at)}`],
    ["Budget phase association", linked ? "Linked to the observed invocation: budget phase only, not whole-wrapper or native Janitor completion." : "Dated/unlinked last result; no verified current-invocation association."],
    ["Last cleanup", when(saved?.last_cleanup_at)],
    ["Cooldown remaining", saved?.cooldown_remaining_sec == null ? "Unknown" : `${saved.cooldown_remaining_sec}s`],
    ["New builds queued", saved?.build_admission?.waiting?.length ?? "Unknown"],
    ["Saved removed artifact bytes", bytes(saved?.removed_bytes)],
    ["Saved observed free-space change", bytes(saved?.observed_reclaimed_bytes)],
    ["Native Janitor result (separate)", when(native?.ended_at)],
    ["Native safety integrity", native?.safety_integrity ?? "Unknown"],
    ["Native run reclaimed", bytes(Number.isFinite(native?.freed_kb) ? native.freed_kb * 1024 : null)],
  ];
}
function cleanupReceipt(result) {
  const dispatch = result.dispatch;
  const receipt = dispatch?.status === "accepted" ? "Scheduling accepted (dispatch only, not cleanup completion)." :
    dispatch?.status === "already_running" ? "Existing runner observed; no duplicate scheduled." :
    `Scheduling ${dispatch?.status || "unknown"}; execution is not confirmed complete.`;
  return `${result.message || result.error || ""} ${receipt} Fresh unit observation: ${unitObservation(result.observation)}`;
}
function openMaintenance(confirm = false) {
  if (actionBusy) return;
  if (snapshot?.janitor?.service || cleanupFeature()) {
    showDetails("Maintenance", snapshot?.janitor || {});
    if (confirm && cleanupAllowed()) confirmAction("run", "Run guarded cleanup");
  } else if (snapshot?.janitor?.last_run) showDetails("Cleanup", snapshot.janitor.last_run);
}
function showDetails(kind, item) {
  if (actionBusy) return;
  detailTarget = {kind, item};
  pendingAction = null;
  $("action-confirm").hidden = true;
  text("action-result", "");
  const fields = kind === "Maintenance" ? maintenanceFields(item) :
    kind === "Service" ? [["Unit", item.unit], ["Scope", item.scope], ["State", item.active], ["Substate", item.sub], ["Description", item.description]] :
    kind === "Process" ? [["Name", item.name], ["PID", item.pid], ["CPU", pct(item.cpu_percent)], ["RAM", bytes(item.memory_bytes)]] :
    kind === "Container" ? [["Name", item.Names], ["Engine", item.engine], ["ID", item.ID], ["Image", item.Image], ["State", item.State || item.Status]] :
    [["Run", item.run_id], ["Started", when(item.started_at)], ["Finished", when(item.ended_at)], ["Reclaimed", bytes(Number.isFinite(item.freed_kb) ? item.freed_kb * 1024 : null)], ["Safety integrity", item.safety_integrity]];
  text("detail-title", `${kind} details`);
  text("detail-observed", `Snapshot observed ${when(snapshot?.generated_at)}; this detail view is fixed until reopened.`);
  detailFields(fields);
  drawActions();
  $("detail-dialog").showModal();
}
function drawActions() {
  const controls = $("action-controls");
  controls.replaceChildren(); controls.hidden = true;
  if (!snapshot?.actions?.enabled || !detailTarget) {
    text("detail-policy", "Read-only mode. Homelab controls are not enabled.");
    return;
  }
  const {kind, item} = detailTarget;
  let choices = [];
  if (kind === "Maintenance") {
    if (cleanupAllowed()) choices = [["run", "Run guarded cleanup"]];
    text("detail-policy", cleanupPolicy());
  } else if (kind === "Container" && !item.protected && /^[a-f0-9]{64}$/.test(item.id || "")) {
    choices = item.State === "running" || item.State === "paused" || item.State === "restarting" ?
      [["stop", "Stop gracefully"], ["kill", "Kill now"]] : [["remove", "Remove container"]];
    text("detail-policy", "Container actions keep all volumes. Running containers must be stopped before removal.");
  } else if (kind === "Process" && item.controllable) {
    choices = [["terminate", "Terminate"], ["kill", "Force kill"]];
    text("detail-policy", "Signals target this exact process identity. A service manager may restart it; stop its user service instead if needed.");
  } else if (kind === "Service" && item.scope === "user" && !(snapshot.actions.protected_services || []).includes(item.unit)) {
    choices = [["stop", "Stop service"]];
    text("detail-policy", "Stops this user service. Core connectivity, the dashboard and protected services stay guarded.");
  } else {
    text("detail-policy", kind === "Service" ? "System/protected service: view-only. No root privileges are granted." :
      kind === "Process" ? "System, protected or unavailable process: view-only." : "No control action for this item.");
  }
  choices.forEach(([action, label]) => {
    const button = document.createElement("button");
    button.type = "button"; button.className = "danger"; button.textContent = label;
    button.addEventListener("click", () => confirmAction(action, label));
    controls.append(button);
  });
  controls.hidden = !choices.length;
}
function confirmAction(action, label) {
  if (actionBusy || !detailTarget) return;
  const {kind, item} = detailTarget;
  if (kind === "Maintenance" && (!cleanupAllowed() || action !== "run")) {
    text("detail-policy", cleanupPolicy()); return;
  }
  const payload = kind === "Maintenance" ? {kind: "cleanup", action: "run"} :
    kind === "Container" ? {kind: "container", engine: item.engine, id: item.id, action} :
    kind === "Process" ? {kind: "process", pid: item.pid, start_time_ticks: item.start_time_ticks, action} :
    {kind: "service", scope: item.scope, unit: item.unit, action};
  const target = kind === "Maintenance" ? `${CLEANUP_UNIT} (current user)` :
    kind === "Container" ? `${item.Names} (${item.engine}, ${item.ID})` :
    kind === "Process" ? `${item.name} (PID ${item.pid})` : `${item.unit} (${item.scope})`;
  pendingAction = {payload, target, label};
  const effect = kind === "Maintenance" ? " Scheduling only, not cleanup completion. The existing guard may defer busy work or reclaim zero bytes; no force mode." :
    action === "remove" ? " Volumes will be kept." :
    kind === "Container" && action === "stop" ? " The daemon waits up to 10 seconds, then may force-kill if it has not exited." : "";
  text("action-question", `${label}: ${target}?${effect}`);
  text("action-run", label);
  $("action-confirm").hidden = false;
  text("action-result", "");
}
async function runAction() {
  if (actionBusy || !pendingAction || !snapshot?.actions?.nonce) return;
  const request = pendingAction;
  if (request.payload.kind === "cleanup" && !cleanupAllowed()) {
    pendingAction = null; $("action-confirm").hidden = true;
    text("action-result", cleanupPolicy()); return;
  }
  actionBusy = true;
  $("cleanup-run").disabled = true;
  $("action-run").disabled = true; $("action-cancel").disabled = true; $("detail-close").disabled = true;
  text("action-result", `${request.label} in progress for ${request.target}...`);
  try {
    const response = await fetch("/api/action", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Action-Token": snapshot.actions.nonce,
        ...(token ? {Authorization: `Bearer ${token}`} : {})},
      body: JSON.stringify(request.payload), signal: AbortSignal.timeout(20000),
    });
    const result = await response.json();
    if (request.payload.kind === "cleanup") {
      if (!response.ok || !result.ok) throw new Error(cleanupReceipt(result));
      text("action-result", cleanupReceipt(result));
    } else {
      if (!response.ok || !result.ok) throw new Error(result.error || `Action failed (HTTP ${response.status})`);
      text("action-result", result.message);
    }
  } catch (error) {
    text("action-result", `${error.message}. Refresh the observation before retrying; an interrupted request may still complete.`);
  } finally {
    actionBusy = false; pendingAction = null;
    $("action-run").disabled = false; $("action-cancel").disabled = false; $("detail-close").disabled = false;
    $("action-confirm").hidden = true;
    $("action-controls").replaceChildren(); $("action-controls").hidden = true;
    refresh(true);
  }
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
    `${rows.length} shown. ${[snapshot.system_services, snapshot.user_services].some(scope => scope?.truncated) ? "Inventory capped at 250 per scope." : snapshot.actions?.enabled ? "Tap a user service for Stop; system units stay view-only." : "System + user scopes; observer mode."}`);
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
function renderMaintenance() {
  const item = snapshot?.janitor || {}, saved = item.maintenance, service = item.service;
  text("cache", bytes(saved?.cache_bytes));
  text("cache-detail", `${bytes(saved?.cache_budget_bytes)} soft budget / ${when(saved?.generated_at)}`);
  if (service || cleanupFeature()) {
    let label = "Runner unknown", good = false;
    if (service?.status === "unavailable") label = "Runner unavailable";
    else if (freshCleanup(service)) {
      if (cleanupRunning.has(service.active)) label = `Runner ${service.active === "active" ? "running" : service.active}`;
      else if (service.active === "failed" || service.result !== "success" || service.exit_status !== 0) label = "Runner failed";
      else if (service.active === "inactive" && service.invocation_id) { label = "Runner finished (unit only)"; good = true; }
      else label = "Runner inactive / no verified execution";
    } else if (service?.status === "available") label = "Runner stale / unknown";
    badge("maintenance-status", label, good);
  } else badge("maintenance-status", saved?.status || "Unknown", !!saved && saved.status !== "error");
  $("maintenance-summary").replaceChildren();
  maintenanceFields(item).forEach(([key, data]) => {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = key; dd.textContent = data; $("maintenance-summary").append(dt, dd);
  });
  const actions = (saved?.actions || []).map(action => `${action.path || action.kind || "Cleanup"}: ${action.removed_bytes == null ? "removed; size unknown" : bytes(action.removed_bytes)}`);
  text("maintenance-actions", !saved ? "Saved budget phase unknown; no result yet." :
    saved.status === "deferred" ? `Saved budget phase deferred. Busy or unbacked work stays protected. ${actions.join(" / ")}` :
    actions.length ? `Saved budget-phase removals: ${actions.join(" / ")}. Removed bytes and concurrent free-space change are separate, not a guaranteed reclaim total.` :
    Array.isArray(saved.actions) ? "No deletion recorded in the saved budget phase; zero work is valid. This is not whole-runner or native Janitor completion." :
    "Saved budget phase has no action records; deletion outcome unknown.");
  $("cleanup-run").hidden = !cleanupFeature();
  $("cleanup-run").disabled = actionBusy || !cleanupAllowed();
  text("cleanup-run", "Run guarded cleanup");
  text("cleanup-policy", cleanupPolicy());
  const details = !!service || cleanupFeature();
  text("cleanup-details", details ? "Maintenance details" : "View last cleanup");
  $("cleanup-details").disabled = !details && !item.last_run;
  list("protected", (saved?.workspaces || []).filter(work => work.status !== "removed").map(work => `${work.path}: ${work.status}`), "No protected workspace records in the latest scan.");
}
function render(value, receipt = null) {
  snapshot = value;
  snapshotReceipt = receipt ? {...receipt, snapshot: value} : null;
  $("content").hidden = false;
  $("login").hidden = true;
  text("host", `${value.host} / ${value.actions?.enabled ? "homelab controls enabled" : "private, read-only monitoring"}`);
  badge("controls-status", value.actions?.enabled ? "Controls on - trusted access" : "Read-only", !value.actions?.enabled);
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
  renderMaintenance();
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
  const requestedAt = performance.now();
  try {
    const response = await fetch("/api/snapshot", {headers: {Authorization: `Bearer ${token}`}, cache: "no-store", signal: AbortSignal.timeout(10000)});
    if (response.status === 401) {
      sessionStorage.removeItem("dashboard-token"); $("login").hidden = false;
      retry = false;
      badge("connection", "Access required", false); notice("Access token missing or invalid. Your previous observations, if any, are stale."); return;
    }
    if (!response.ok) throw new Error(`Snapshot unavailable (HTTP ${response.status})`);
    const receipt = receiptFor(response, requestedAt);
    const value = await response.json();
    if (paused && !manual) return;
    render(value, receipt);
    const age = observationAge(value.generated_at);
    if (!Number.isFinite(age)) { badge("connection", "Age unknown / stale", false); notice("Server-relative snapshot age evidence is unavailable. Refresh for authoritative time evidence; guarded cleanup stays disabled."); }
    else if (age > (value.stale_after_seconds || 20)) { badge("connection", "Stale data", false); notice(`Last observation is ${Math.round(age)} seconds old; the collector may be delayed.`); }
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
$("detail-dialog").addEventListener("cancel", event => { if (actionBusy) event.preventDefault(); });
$("action-run").addEventListener("click", runAction);
$("action-cancel").addEventListener("click", () => { if (!actionBusy) { pendingAction = null; $("action-confirm").hidden = true; } });
$("cleanup-details").addEventListener("click", () => openMaintenance());
$("cleanup-run").addEventListener("click", () => openMaintenance(true));
refresh();
