"use strict";

const $ = id => document.getElementById(id);
let snapshot = null;
let snapshotReceipt = null;
let paused = false;
let refreshing = false;
let detailTarget = null;
let detailOpener = null;
let pendingAction = null;
let actionBusy = false;
let actionHistory = null;
let actionHistoryBusy = false;
let actionHistoryManualPending = false;
let actionHistoryError = "";
let unlinkedAttempt = "";
let storageView = null;
let storageViewSource = "none";
let storageReadEpoch = 0;
let storageReadBusy = false;
let storageManualPending = false;
let storagePostBusy = false;
let storageReadError = "";
let storageRequestError = "";
let storagePresetPending = true;
let storagePresetRevision = 0;
const storageRoots = ["home", "root", "tmp"];
const storageActive = new Set(["starting", "running", "cancelling", "finalization_failed"]);
const storageStates = new Set([...storageActive, "complete", "partial", "failed", "cancelled", "timed_out"]);
const CLEANUP_UNIT = "system-janitor-catchup.service";
const cleanupRunning = new Set(["active", "activating", "deactivating", "reloading", "refreshing"]);
const detailHeadings = {Service: "services-heading", Process: "processes-heading",
  Container: "containers-heading", Maintenance: "maintenance-heading", Cleanup: "maintenance-heading"};
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
function row(values, labels = []) {
  const tr = document.createElement("tr");
  values.forEach((value, index) => {
    const td = document.createElement("td"); td.textContent = value;
    if (labels[index]) td.setAttribute("data-label", labels[index]);
    tr.append(td);
  });
  return tr;
}
function list(id, values, empty) {
  $(id).replaceChildren();
  (values.length ? values : [empty]).forEach(value => {
    const li = document.createElement("li"); li.textContent = value; $(id).append(li);
  });
}
function notice(message) { text("notice", message); $("notice").hidden = !message; }
function detailFields(fields, id = "detail-fields") {
  $(id).replaceChildren();
  fields.forEach(([key, value]) => {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = key; dd.textContent = value ?? "Unknown"; $(id).append(dt, dd);
  });
}
function targetKey(kind, item) {
  if (kind === "Container" && ["docker", "podman"].includes(item.engine) && /^[a-f0-9]{64}$/.test(item.id || "")) return `container:${item.engine}:${item.id}`;
  if (kind === "Service" && ["user", "system"].includes(item.scope) && typeof item.unit === "string" && item.unit) return `service:${item.scope}:${item.unit}`;
  if (kind === "Process" && Number.isSafeInteger(item.pid) && item.pid > 0 &&
      Number.isSafeInteger(item.start_time_ticks) && item.start_time_ticks >= 0) return `process:${item.pid}:${item.start_time_ticks}`;
  return null; // Never substitute a name, short ID or reused PID for an exact identity.
}
function canFocus(node) {
  const dialog = node?.closest("dialog");
  return node?.isConnected && !node.disabled && !node.closest("[hidden]") && (!dialog || dialog.open);
}
function focusContext(node, kind) {
  const opener = ["BUTTON", "A", "INPUT", "SELECT", "SUMMARY"].includes(node?.tagName) ? node : null;
  return {node: opener, key: opener?.getAttribute("data-target-key"), heading: detailHeadings[kind]};
}
function restoreFocus(context) {
  if (!context) return;
  const matching = canFocus(context.node) ? context.node : context.key &&
    [...document.querySelectorAll("button[data-target-key]")].find(node =>
      node.getAttribute("data-target-key") === context.key && canFocus(node));
  const target = matching || $(context.heading);
  if (canFocus(target)) target.focus();
}
function rowFocus(id, kind) {
  return $(id).contains(document.activeElement) ? focusContext(document.activeElement, kind) : null;
}
function inspectControl(button, kind, item) {
  const key = targetKey(kind, item);
  if (key) button.setAttribute("data-target-key", key);
  const identity = kind === "Service" ? `${item.unit} (${item.scope} scope)` :
    kind === "Container" ? `${item.Names} (${item.engine}, ${item.id || "full ID unknown"})` :
    `${item.name} (PID ${item.pid}, start ticks ${item.start_time_ticks ?? "unknown"})`;
  button.setAttribute("aria-label", `Inspect ${kind.toLowerCase()}: ${identity}`);
  button.addEventListener("click", event => showDetails(kind, item, event.currentTarget));
}
function cleanupFeature() {
  const actions = snapshot?.actions, control = actions?.cleanup;
  return actions?.enabled === true && Array.isArray(actions.capabilities) &&
    ["guarded-cleanup", "cleanup-job-state"].every(name => actions.capabilities.includes(name)) &&
    control?.kind === "cleanup" && control.action === "run" && control.unit === CLEANUP_UNIT &&
    control.scope === "user" && control.dispatch_only === true;
}
function advertisedActions(kind) {
  const legacy = kind === "Container" ? ["stop", "kill", "remove"] : ["stop"];
  const known = [...legacy, "start", "restart"];
  const values = snapshot?.actions?.[kind === "Container" ? "container_actions" : "service_actions"];
  if (values === undefined) return legacy;
  if (!Array.isArray(values) || values.some(value => typeof value !== "string" || !known.includes(value)) ||
      new Set(values).size !== values.length) return [];
  const recovery = Array.isArray(snapshot.actions.capabilities) &&
    snapshot.actions.capabilities.includes("workload-recovery");
  return values.filter(value => legacy.includes(value) || recovery);
}
function observedActionsFeature() {
  return snapshot?.actions?.enabled === true && Array.isArray(snapshot.actions.capabilities) &&
    snapshot.actions.capabilities.includes("observed-action-state");
}
function actionHistoryFeature() {
  const actions = snapshot?.actions;
  return observedActionsFeature() && actions.capabilities.includes("action-history") &&
    actions.capabilities.includes("workload-recovery") &&
    Array.isArray(actions.container_actions) && Array.isArray(actions.service_actions) &&
    advertisedActions("Container").length > 0 && advertisedActions("Service").length > 0;
}
function workloadChoices(kind, item) {
  if (snapshot?.actions?.enabled !== true || detailTarget?.unverified) return [];
  let choices = [];
  if (kind === "Container" && !item.protected && ["docker", "podman"].includes(item.engine) &&
      /^[a-f0-9]{64}$/.test(item.id || "")) {
    const modern = Array.isArray(snapshot.actions.container_actions);
    if (["running", "paused", "restarting"].includes(item.State)) {
      choices = [["stop", "Stop gracefully"], ["kill", "Kill now"]];
      if (item.State === "running" && item.protected === false) choices.push(["restart", "Restart container"]);
    } else if (["created", "exited", "stopped"].includes(item.State)) {
      if (item.protected === false) choices.push(["start", "Start container"]);
      choices.push(["remove", "Remove container"]);
    } else if (!modern) choices = [["remove", "Remove container"]];
    choices = choices.filter(([action]) => advertisedActions(kind).includes(action));
  } else if (kind === "Process" && item.controllable) {
    choices = [["terminate", "Terminate"], ["kill", "Force kill"]];
  } else if (kind === "Service" && item.scope === "user" && !item.protected &&
      !(snapshot.actions.protected_services || []).some(unit => unit === item.unit || unit === item.id)) {
    choices = [["stop", "Stop service"]];
    if (item.can_start !== false && item.refuse_manual_start !== true &&
        (!item.load_state || item.load_state === "loaded") && (!item.id || item.id === item.unit)) {
      if (["inactive", "failed"].includes(item.active)) choices.push(["start", "Start service"]);
      if (["active", "inactive", "failed"].includes(item.active)) choices.push(["restart", "Restart service"]);
    }
    choices = choices.filter(([action]) => advertisedActions(kind).includes(action));
  }
  return choices;
}
function applyActionObservation(request, result) {
  if (!detailTarget) return;
  if (request.kind === "cleanup") {
    detailTarget.afterAttempt = true;
    $("detail-current").hidden = false;
    text("detail-current", `Fresh unit observation after scheduling attempt: ${unitObservation(result.observation)} Scheduling is not cleanup completion; saved fields remain historical.`);
    return;
  }
  if (!observedActionsFeature()) return;
  const value = result.observation, item = detailTarget.item;
  detailTarget.unverified = true;
  $("detail-current").hidden = false;
  text("detail-current", "Exact post-attempt state: unknown; no verified server read received. Historical fields retained; not application health.");
  if (!value || typeof value.read_at !== "string" || !Number.isFinite(Date.parse(value.read_at))) return;
  if (request.kind === "container" && value.id === request.id && value.engine === request.engine &&
      value.status === "available" && typeof value.protected === "boolean" &&
      ["created", "exited", "stopped", "running", "paused", "restarting", "removing", "dead"].includes(value.state)) {
    item.State = value.state; item.protected = value.protected; detailTarget.unverified = false;
  } else if (request.kind === "service" && value.unit === request.unit && value.id === request.unit &&
      value.scope === "user" && value.status === "available" && value.load_state === "loaded" &&
      value.state === value.active && typeof value.can_start === "boolean" &&
      typeof value.refuse_manual_start === "boolean" && typeof value.protected === "boolean" &&
      [...cleanupRunning, "inactive", "failed"].includes(value.state)) {
    Object.assign(item, {active: value.active, sub: value.sub, id: value.id, load_state: value.load_state,
      can_start: value.can_start, refuse_manual_start: value.refuse_manual_start, protected: value.protected});
    detailTarget.unverified = false;
  } else if (request.kind === "process" && value.pid === request.pid &&
      value.start_time_ticks === request.start_time_ticks && value.status === "available" && value.state === "present") {
    detailTarget.unverified = false;
  }
  text("detail-current", `Exact post-attempt state: ${value.state || "unknown"}; server read ${when(value.read_at)}. ${detailTarget.unverified ? "No further control authority granted. " : ""}Historical fields retained; not application health.`);
}
function actionReceipt(result) {
  const id = typeof result.action_id === "string" && /^[a-f0-9]{32}$/.test(result.action_id) ? result.action_id : null;
  if (!observedActionsFeature()) return "";
  if (!id) {
    unlinkedAttempt = "UNLINKED browser attempt: no action ID received. Not attached to history by target or time; never automatically replayed.";
    renderActionHistory();
    return `${unlinkedAttempt} `;
  }
  return `Request receipt ${id}: `;
}
function workloadObservation(value) {
  if (!value) return "Exact target observation: unknown; no read received. Not application health.";
  return `Exact target observation: ${value.state || "unknown"}; server read ${when(value.read_at)}. ${value.detail || ""} Not application health.`;
}
function renderActionHistory() {
  $("actions-panel").hidden = !actionHistoryFeature();
  if (!actionHistoryFeature()) return;
  const value = actionHistory;
  text("actions-quality", actionHistoryError || (!value ? "History not read yet." :
    `${value.status}; server read ${when(value.read_at)}. ${value.detail || ""} Truncated: ${value.truncated}. ` +
    `Tail truncated: ${value.quality.tail_truncated}; events capped: ${value.quality.events_truncated}; ` +
    `invalid lines: ${value.quality.invalid_lines}; partial first line: ${value.quality.incomplete_first_line}; ` +
    `incomplete tail: ${value.quality.incomplete_tail}; changed during read: ${value.quality.changed_during_read}.`));
  text("unlinked-attempt", unlinkedAttempt); $("unlinked-attempt").hidden = !unlinkedAttempt;
  const events = value?.events || [], terminal = new Set(events.filter(event => event.event !== "started").map(event => event.action_id));
  list("recent-actions", events.map(event => {
    const meaning = event.event === "started" && !terminal.has(event.action_id) ? "started; unresolved in this bounded history" :
      event.kind === "cleanup" && event.event === "accepted" ? "accepted scheduling only, not cleanup completion" :
      event.kind === "cleanup" && event.event === "observed" ? "existing runner observed; no duplicate scheduled" : event.event;
    return `${event.action_id} / ${when(event.time)} / ${event.kind} ${event.action}: ${meaning}. ${event.message || ""}`;
  }), value?.status === "no_history_yet" ? "NoHistoryYet: no actual journal events yet." : "No curated events in the current bounded read; check history quality.");
}
async function refreshActionHistory(manual = false) {
  if (!actionHistoryFeature() || (paused && !manual)) return;
  if (actionHistoryBusy) {
    if (manual) actionHistoryManualPending = true;
    return;
  }
  actionHistoryBusy = true;
  let publish = false;
  try {
    const response = await fetch("/api/actions", {method: "GET", cache: "no-store",
      headers: token ? {Authorization: "Bearer " + token} : {}, signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(`History unavailable (HTTP ${response.status})`);
    const value = await response.json();
    if (!value || !Array.isArray(value.events) || value.events.length > 20 || !value.quality ||
        typeof value.truncated !== "boolean" || typeof value.read_at !== "string" ||
        !Number.isFinite(Date.parse(value.read_at)) ||
        !Number.isSafeInteger(value.quality.invalid_lines) || value.quality.invalid_lines < 0 ||
        ["tail_truncated", "events_truncated", "incomplete_first_line", "incomplete_tail", "changed_during_read"]
          .some(key => typeof value.quality[key] !== "boolean") ||
        !["available", "partial", "no_history_yet", "unavailable"].includes(value.status) ||
        value.events.some(event => !event || typeof event.action_id !== "string" ||
          !/^[a-f0-9]{32}$/.test(event.action_id))) throw new Error("Malformed history response");
    if (!actionHistoryFeature() || (paused && !manual)) return;
    actionHistory = value; actionHistoryError = "";
    publish = true;
  } catch (error) {
    if (actionHistoryFeature() && (!paused || manual)) {
      actionHistoryError = `Unavailable: ${error.message}. Previous dated receipts, if any, are retained; no replay.`;
      publish = true;
    }
  } finally {
    actionHistoryBusy = false;
    if (publish && (!paused || manual)) renderActionHistory();
    // A manual request waiting behind an automatic read needs its own GET;
    // never relabel the old response as manual or leave the read slot occupied.
    const manualPending = actionHistoryManualPending;
    actionHistoryManualPending = false;
    if (manualPending) refreshActionHistory(true);
  }
}
function storageManifest(value) {
  return value?.supported === true && value.api_version === 1 &&
    typeof value.enabled === "boolean" && typeof value.available === "boolean" &&
    Array.isArray(value.capabilities) && value.capabilities.includes("storage-scan") &&
    value.limits?.max_directories === 128 && Number.isFinite(value.limits.wall_seconds) && value.limits.wall_seconds > 0 &&
    Array.isArray(value.roots) && value.roots.length === 3 &&
    storageRoots.every(id => value.roots.filter(root => root?.id === id).length === 1);
}
function storageFeature() { return storageManifest(snapshot?.storage_scan); }
function storageAllowed() {
  return storageFeature() && snapshot.storage_scan.enabled && snapshot.storage_scan.available &&
    snapshot.actions?.enabled === true && typeof snapshot.actions.nonce === "string" && !!snapshot.actions.nonce;
}
function validStorageView(value) {
  if (!storageManifest(value) || typeof value.read_at !== "string" || !Number.isFinite(Date.parse(value.read_at))) return false;
  const job = value.job;
  if (job === null) return true;
  const id = value => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
  const size = value => Number.isSafeInteger(value) && value >= 0;
  const name = value => typeof value === "string" && value.length > 0 && value.length <= 4096;
  if (!job || !id(job.id) || !id(job.directory_id) || !storageRoots.includes(job.root) || !storageStates.has(job.state) ||
      !Number.isFinite(job.elapsed_seconds) || job.elapsed_seconds < 0 ||
      typeof job.started_at !== "string" || !Number.isFinite(Date.parse(job.started_at)) ||
      (job.finished_at !== null && (typeof job.finished_at !== "string" || !Number.isFinite(Date.parse(job.finished_at)))) ||
      !["complete", "lower_bound", "unknown"].includes(job.coverage) ||
      (job.root_bytes !== null && !size(job.root_bytes)) || (job.direct_bytes !== null && !size(job.direct_bytes)) ||
      !Array.isArray(job.directories) || job.directories.length > 128 ||
      !Array.isArray(job.breadcrumbs) || !job.breadcrumbs.length || job.breadcrumbs.length > 64 ||
      job.breadcrumbs.some(crumb => !id(crumb?.id) || !name(crumb.name)) ||
      job.breadcrumbs.at(-1).id !== job.directory_id ||
      job.directories.some(row => !row || !name(row.name) || !size(row.bytes) ||
        typeof row.navigable !== "boolean" || (row.navigable ? !id(row.id) : row.id !== null)) ||
      !size(job.omitted_directories?.count) || !size(job.omitted_directories.bytes) ||
      !size(job.warning_count) || typeof job.warning_sample !== "string" || job.warning_sample.length > 16384 ||
      typeof job.permission_denied !== "boolean") return false;
  const subtotal = job.directories.reduce((total, row) => total + row.bytes, job.omitted_directories.bytes);
  return size(subtotal) && (job.root_bytes === null ||
    (job.direct_bytes !== null && size(subtotal + job.direct_bytes) && subtotal + job.direct_bytes === job.root_bytes));
}
function acceptStorageReceipt(value, route) {
  // A validated acknowledgement preserves its exact ID, not completion or measured results.
  // Keep Cancel usable for a known accepted job even if its separate cached GET fails.
  storageView = {...value, job: {...value.job,
    state: route === "/api/storage-scan/cancel" ? "cancelling" : "starting",
    finished_at: null, root_bytes: null, direct_bytes: null, directories: [],
    omitted_directories: {count: 0, bytes: 0}, coverage: "unknown", display_truncated: false,
    exit_code: null, error: null, error_code: null}};
  storageViewSource = "accepted_receipt";
  storageReadEpoch++; // Reads begun before this accepted pointer cannot restore an older job.
  storageReadError = "";
}
function drawStorageMap(job, rootBytes) {
  $("storage-map").replaceChildren();
  const omitted = job?.omitted_directories;
  const tiles = (job?.directories || []).filter(row => row.bytes > 0).map(row => ({name: row.name, bytes: row.bytes}));
  if (omitted?.bytes > 0) tiles.push({name: `Other directories (${omitted.count})`, bytes: omitted.bytes});
  if (rootBytes !== null && job?.direct_bytes > 0) tiles.push({name: "Direct files, symlinks & directory metadata", bytes: job.direct_bytes});
  const total = tiles.reduce((sum, tile) => sum + tile.bytes, 0);
  text("storage-map-note", !total ? "No measured nonzero allocations; root coverage may be unknown." :
    rootBytes === null ? `Root total unknown. Map compares only ${bytes(total)} of reported directory allocations; direct bytes are unknown.` :
    `Map proportions: ${bytes(total)} measured allocations, not filesystem capacity or reclaimable bytes. Ranked buttons below are the navigation route.`);
  if (!total) return;
  const colors = ["#81b9ff", "#88e0b1", "#f0bf74", "#b9a7e8", "#78c5cc", "#c8d889"];
  let color = 0;
  const draw = (items, x, y, width, height) => {
    if (items.length === 1) {
      const tile = items[0], group = document.createElementNS("http://www.w3.org/2000/svg", "g");
      group.setAttribute("aria-hidden", "true");
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      for (const [key, value] of Object.entries({x, y, width, height, "data-bytes": tile.bytes, fill: colors[color++ % colors.length]})) rect.setAttribute(key, String(value));
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${tile.name}: ${bytes(tile.bytes)} allocated`;
      group.append(title, rect);
      if (width >= 90 && height >= 30) {
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", String(x + 8)); label.setAttribute("y", String(y + 24));
        label.textContent = [...tile.name].slice(0, Math.min(45, Math.floor(width / 12))).join("");
        group.append(label);
      }
      $("storage-map").append(group); return;
    }
    const sum = items.reduce((value, tile) => value + tile.bytes, 0);
    let split = 1, left = items[0].bytes;
    while (split < items.length - 1 && Math.abs(left + items[split].bytes - sum / 2) < Math.abs(left - sum / 2)) left += items[split++].bytes;
    const ratio = left / sum;
    if (width >= height) {
      draw(items.slice(0, split), x, y, width * ratio, height);
      draw(items.slice(split), x + width * ratio, y, width * (1 - ratio), height);
    } else {
      draw(items.slice(0, split), x, y, width, height * ratio);
      draw(items.slice(split), x, y + height * ratio, width, height * (1 - ratio));
    }
  };
  draw(tiles, 0, 0, 1000, 500);
}
function renderStorage() {
  const feature = storageFeature(), allowed = storageAllowed(), job = storageView?.job;
  const accepted = storageViewSource === "accepted_receipt";
  const focused = document.activeElement, focusedId = focused?.getAttribute("data-directory-id");
  const focus = focusedId ? {id: focusedId, job: focused.getAttribute("data-job-id")} : null;
  $("storage-explorer").hidden = !feature;
  text("storage-support", !feature ? "On-demand storage scans are not advertised by this backend; unsupported requests are disabled." :
    !snapshot.storage_scan.enabled || !snapshot.actions?.enabled ? "Observer mode: starting storage scans is disabled. Cached results remain view-only." :
    !snapshot.storage_scan.available ? snapshot.storage_scan.detail || "GNU du unavailable; no fallback scan." :
    "Explicit read-only scans only, up to 120s. Files are never opened for content. Selecting a preset alone does not scan.");
  if (!$("storage-root").value) $("storage-root").value = "home";
  if (!storagePresetPending && job) $("storage-root").value = job.root;
  const busy = storageActive.has(job?.state);
  $("storage-start").disabled = !allowed || storagePostBusy || busy;
  $("storage-cancel").disabled = !allowed || storagePostBusy || !["starting", "running", "finalization_failed"].includes(job?.state);
  text("storage-cancel", job?.state === "finalization_failed" ? "Retry cancellation" : "Cancel scan");
  $("storage-back").disabled = !allowed || storagePostBusy || busy || (job?.breadcrumbs.length || 0) < 2;
  $("storage-scan").setAttribute("data-state", job?.state || "idle");
  $("storage-scan").setAttribute("data-job-id", job?.id || "");
  $("storage-scan").setAttribute("data-state-source", storageViewSource);
  $("storage-state").setAttribute("aria-busy", String(busy));
  text("storage-state", job ? accepted ? `${job.state.toUpperCase()} / accepted request; current state unverified.` :
    `${job.state.toUpperCase()} / ${job.elapsed_seconds.toFixed(1)}s elapsed${job.state === "finalization_failed" ? "; ownership retained, another scan is blocked" : ""}` : "Not scanned");
  text("storage-date", job ? accepted ? `Request accepted ${when(storageView.read_at)}. Cached job state and completion are not yet verified.` :
    `Started ${when(job.started_at)} / ${job.finished_at ? `finished ${when(job.finished_at)}` : "not finalized"}. Server read ${when(storageView.read_at)}.` : "No scan result yet.");
  const rootBytes = !job || (job.root_bytes === 0 && job.state !== "complete") ? null : job.root_bytes;
  text("storage-total", `Measured root allocations: ${bytes(rootBytes)}`);
  text("storage-coverage", !job ? "Coverage unknown; refresh reads cached state only. Scan is a separate explicit operation." :
    `Coverage: ${job.coverage === "complete" ? "complete traversal of this pinned directory on one filesystem, at scan time" :
      job.coverage === "lower_bound" ? "partial / lower-bound traversal" : "unknown"}. ` +
    `${job.warning_count} diagnostic lines${job.permission_denied ? "; permission denied under the current user, no elevation" : ""}. ` +
    `${job.directories.length} displayed directories; ${job.omitted_directories.count} omitted and summed separately. ` +
    "Proportions describe measured allocated bytes, not reclaimable space or the entire physical filesystem. Changing trees are not an atomic inventory.");
  text("storage-error", [storageRequestError, storageReadError, job?.error].filter(Boolean).join(" "));
  $("storage-error").hidden = !$("storage-error").textContent;
  $("storage-breadcrumbs").replaceChildren();
  (job?.breadcrumbs || []).forEach(crumb => {
    const button = document.createElement("button");
    button.type = "button"; button.className = "secondary"; button.textContent = crumb.name;
    button.setAttribute("data-directory-id", crumb.id); button.setAttribute("data-job-id", job.id);
    button.setAttribute("aria-label", `Scan breadcrumb directory: ${crumb.name} (read-only)`);
    if (crumb.id === job.directory_id) button.setAttribute("aria-current", "location");
    button.disabled = !allowed || storagePostBusy || busy || crumb.id === job.directory_id;
    button.addEventListener("click", () => storageNavigate(crumb.id, job.id));
    $("storage-breadcrumbs").append(button);
  });
  $("storage-directories").replaceChildren();
  [...(job?.directories || [])].sort((a, b) => b.bytes - a.bytes || a.name.localeCompare(b.name)).forEach(item => {
    const li = document.createElement("li"), button = document.createElement("button");
    button.type = "button"; button.className = "storage-directory";
    const name = document.createElement("span"), size = document.createElement("span");
    name.textContent = item.name; size.textContent = bytes(item.bytes); size.className = "storage-size";
    button.append(name, size);
    button.setAttribute("aria-label", `Scan directory: ${item.name}, ${bytes(item.bytes)} allocated (read-only)`);
    if (item.id) {
      button.setAttribute("data-directory-id", item.id); button.setAttribute("data-job-id", job.id);
      button.addEventListener("click", () => storageNavigate(item.id, job.id));
    }
    button.disabled = !allowed || storagePostBusy || busy || !item.navigable;
    li.append(button);
    if (item.detail) {
      const detail = document.createElement("p"); detail.className = "muted"; detail.textContent = item.detail; li.append(detail);
    }
    $("storage-directories").append(li);
  });
  list("storage-buckets", job ? [
    `Other directories (${job.omitted_directories.count} omitted): ${bytes(job.omitted_directories.bytes)} allocated; not a drill-down target.`,
    `Direct files, symlinks & directory metadata: ${bytes(rootBytes === null ? null : job.direct_bytes)}. Separate from directory subtotals; not file-content inspection.`,
  ] : [], "No measured buckets yet.");
  text("storage-warnings", job ? `Native exit: ${job.exit_code ?? "not finalized"}. ${job.warning_sample || "No native diagnostic output."}${job.warning_truncated ? "\nDiagnostic sample capped; further lines were drained, not stored." : ""}` : "No scan diagnostics yet.");
  drawStorageMap(job, rootBytes);
  if (focus) {
    const matching = [...document.querySelectorAll("button[data-directory-id]")].find(button =>
      button.getAttribute("data-directory-id") === focus.id && button.getAttribute("data-job-id") === focus.job && canFocus(button));
    (matching || $("storage-heading")).focus();
  }
}
async function refreshStorage(manual = false) {
  if (!storageFeature() || (paused && !manual)) return;
  if (storageReadBusy) { if (manual) storageManualPending = true; return; }
  storageReadBusy = true;
  const readEpoch = storageReadEpoch;
  let publish = false;
  try {
    const response = await fetch("/api/storage-scan", {method: "GET", cache: "no-store",
      headers: token ? {Authorization: "Bearer " + token} : {}, signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(`Storage state unavailable (HTTP ${response.status})`);
    const value = await response.json();
    if (!validStorageView(value)) throw new Error("Malformed storage state");
    if (readEpoch !== storageReadEpoch || !storageFeature() || (paused && !manual)) return;
    storageView = value; storageViewSource = "cached_read"; storageReadError = ""; publish = true;
  } catch (error) {
    if (readEpoch === storageReadEpoch && storageFeature() && (!paused || manual)) {
      storageReadError = storageViewSource === "accepted_receipt" ?
        `${error.message}. Accepted job ${storageView.job.id} retained; state/completion unverified. No automatic replay.` :
        `${error.message}. Previous dated scan, if any, retained; refresh reads state only, never starts or replays a scan.`;
      publish = true;
    }
  } finally {
    storageReadBusy = false;
    if (publish && (!paused || manual)) renderStorage();
    const queued = storageManualPending; storageManualPending = false;
    if (queued) refreshStorage(true); // A new manual GET, not promotion of a delayed automatic response.
  }
}
async function storageRequest(route, payload) {
  if (!storageAllowed() || storagePostBusy) return;
  const presetRevision = storagePresetRevision;
  storagePostBusy = true; storageRequestError = ""; renderStorage();
  try {
    const headers = {"Content-Type": "application/json", "X-Action-Token": snapshot.actions.nonce};
    if (token) headers.Authorization = "Bearer " + token;
    const response = await fetch(route, {method: "POST", headers, body: JSON.stringify(payload),
      cache: "no-store", signal: AbortSignal.timeout(10000)});
    const value = await response.json();
    if (!response.ok) {
      storageRequestError = `Storage request rejected (HTTP ${response.status}): ${value.error || "unavailable"}. No automatic replay.`;
    } else {
      if (!validStorageView(value) || !value.job) throw new Error("Storage request receipt unavailable");
      acceptStorageReceipt(value, route);
      if (payload.root && presetRevision === storagePresetRevision) storagePresetPending = false;
    }
  } catch (error) {
    storageRequestError = `${error.message}. Request outcome may be unknown; no automatic replay. Refresh state before a new explicit intent.`;
  } finally {
    storagePostBusy = false; renderStorage();
    refreshStorage(true); // Explicit Scan/Cancel reads once and never changes Pause.
  }
}
function storageNavigate(identifier, jobId) {
  const job = storageView?.job;
  if (!storageAllowed() || storagePostBusy || storageActive.has(job?.state)) return;
  if (!job || jobId !== job.id || ![...job.breadcrumbs, ...job.directories.filter(row => row.navigable)].some(row => row.id === identifier)) {
    storageRequestError = "Directory result changed; refresh for current server-issued IDs. No scan started."; renderStorage(); return;
  }
  storagePresetPending = false;
  storageRequest("/api/storage-scan/start", {job_id: job.id, directory_id: identifier});
}
function startStorage() {
  if (!storageAllowed() || storagePostBusy || storageActive.has(storageView?.job?.state)) return;
  if (!storagePresetPending && storageView?.job) return storageNavigate(storageView.job.directory_id, storageView.job.id);
  const root = $("storage-root").value;
  if (storageRoots.includes(root)) storageRequest("/api/storage-scan/start", {root});
}
function cancelStorage() {
  const job = storageView?.job;
  if (["starting", "running", "finalization_failed"].includes(job?.state)) storageRequest("/api/storage-scan/cancel", {job_id: job.id});
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
function storageFields(saved) {
  const age = observationAge(saved?.generated_at);
  const caches = Array.isArray(saved?.caches), workspaces = Array.isArray(saved?.workspaces);
  const coverage = !caches && !workspaces ? "Unknown; no saved lists." :
    `${caches ? `${saved.caches.length} cache records` : "Cache records unknown"} / ` +
    `${workspaces ? `${saved.workspaces.length} workspace records` : "workspace records unknown"}; saved lists only, not complete disk attribution.`;
  return [
    ["Saved source", saved ? `maintenance-last.json (cached budget phase; mode ${saved.mode || "Unknown"})` : "Unknown; no saved budget phase."],
    ["Observed cached Go bytes", bytes(saved?.cache_bytes)],
    ["Cache observation age", saved?.generated_at ? `${when(saved.generated_at)} / ${Number.isFinite(age) && age >= 0 ? `${Math.round(age)}s old (server-relative)` : "age unknown"}` : "Unknown"],
    ["Saved coverage", coverage],
    ["Soft cache budget", bytes(saved?.cache_budget_bytes)],
  ];
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
function openMaintenance(confirm = false, opener = document.activeElement) {
  if (actionBusy) return;
  if (snapshot?.janitor?.service || cleanupFeature()) {
    showDetails("Maintenance", snapshot?.janitor || {}, opener);
    if (confirm && cleanupAllowed()) confirmAction("run", "Run guarded cleanup");
  } else if (snapshot?.janitor?.last_run) showDetails("Cleanup", snapshot.janitor.last_run, opener);
}
function showDetails(kind, item, opener = document.activeElement) {
  if (actionBusy) return;
  // Post-action facts update the retained dialog, never the historical cached row.
  detailTarget = {kind, item: {...item}};
  detailOpener = focusContext(opener, kind);
  pendingAction = null;
  $("action-confirm").hidden = true;
  $("detail-current").hidden = true;
  text("detail-current", "");
  text("action-result", "");
  const fields = kind === "Maintenance" ? [...maintenanceFields(item), ...storageFields(item.maintenance)] :
    kind === "Service" ? [["Unit", item.unit], ["Scope", item.scope], ["State", item.active], ["Substate", item.sub], ["Description", item.description]] :
    kind === "Process" ? [["Name", item.name], ["PID", item.pid], ["Start ticks", item.start_time_ticks], ["CPU", pct(item.cpu_percent)], ["RAM", bytes(item.memory_bytes)]] :
    kind === "Container" ? [["Name", item.Names], ["Engine", item.engine], ["ID", item.id || item.ID], ["Image", item.Image], ["State", item.State || item.Status]] :
    [["Run", item.run_id], ["Started", when(item.started_at)], ["Finished", when(item.ended_at)], ["Reclaimed", bytes(Number.isFinite(item.freed_kb) ? item.freed_kb * 1024 : null)], ["Safety integrity", item.safety_integrity]];
  text("detail-title", `${kind} details`);
  text("detail-observed", `Historical snapshot details: observed ${when(snapshot?.generated_at)}; fields below are fixed until reopened.`);
  detailFields(fields);
  drawActions();
  $("detail-dialog").showModal();
  $("detail-close").focus();
}
function drawActions() {
  const controls = $("action-controls");
  const focusedAction = controls.contains(document.activeElement) ? document.activeElement.getAttribute("data-action") : null;
  controls.replaceChildren(); controls.hidden = true;
  if (!snapshot?.actions?.enabled || !detailTarget) {
    text("detail-policy", "Read-only mode. Homelab controls are not enabled.");
    if (focusedAction) $("detail-close").focus();
    return;
  }
  const {kind, item} = detailTarget;
  let choices = workloadChoices(kind, item);
  if (kind === "Maintenance") {
    if (!detailTarget.afterAttempt && cleanupAllowed()) choices = [["run", "Run guarded cleanup"]];
    text("detail-policy", detailTarget.afterAttempt ? "Scheduling receipt retained. Refresh/reopen Maintenance for current runner state; no automatic retry." : cleanupPolicy());
  } else if (detailTarget.unverified) {
    text("detail-policy", "Exact target observation is unknown, absent or replaced. No unsafe action granted; reopen from a fresh inventory before choosing another guarded action.");
  } else if (kind === "Container" && choices.length) {
    text("detail-policy", "Fixed existing-container controls keep volumes. Restart disrupts the workload and may force-kill after 10 seconds. Fresh server guards apply.");
  } else if (kind === "Process" && item.controllable) {
    text("detail-policy", "Signals target this exact process identity. A service manager may restart it; stop its user service instead if needed.");
  } else if (kind === "Service" && choices.length) {
    text("detail-policy", "Fixed loaded user-service controls; fresh exact user-manager state and canonical protection are rechecked. No root/enable/unmask. Retained identity can recover a stopped service outside the capped list.");
  } else {
    text("detail-policy", kind === "Service" ? "System/protected service: view-only. No root privileges are granted." :
      kind === "Process" ? "System, protected or unavailable process: view-only." : "No control action for this item.");
  }
  choices.forEach(([action, label]) => {
    const button = document.createElement("button");
    button.type = "button"; button.className = "danger"; button.textContent = label;
    button.setAttribute("data-action", action);
    button.addEventListener("click", event => confirmAction(action, label, event?.currentTarget));
    controls.append(button);
  });
  controls.hidden = !choices.length;
  if (focusedAction) {
    const matching = [...controls.children].find(button => button.getAttribute("data-action") === focusedAction);
    (matching || $("detail-close")).focus();
  }
}
function confirmAction(action, label, opener = document.activeElement) {
  if (actionBusy || !detailTarget) return;
  const {kind, item} = detailTarget;
  if (kind === "Maintenance" && (!cleanupAllowed() || action !== "run")) {
    text("detail-policy", cleanupPolicy()); return;
  }
  if (kind !== "Maintenance" && !workloadChoices(kind, item).some(([choice]) => choice === action)) {
    text("action-result", "Action is not advertised or allowed for this target. Refresh before choosing again."); return;
  }
  const payload = kind === "Maintenance" ? {kind: "cleanup", action: "run"} :
    kind === "Container" ? {kind: "container", engine: item.engine, id: item.id, action} :
    kind === "Process" ? {kind: "process", pid: item.pid, start_time_ticks: item.start_time_ticks, action} :
    {kind: "service", scope: item.scope, unit: item.unit, action};
  const target = kind === "Maintenance" ? `${CLEANUP_UNIT} (current user)` :
    kind === "Container" ? `${item.Names} (${item.engine}, ${item.id})` :
    kind === "Process" ? `${item.name} (PID ${item.pid}), start ticks ${item.start_time_ticks}` : `${item.unit} (${item.scope})`;
  pendingAction = {payload, target, label, opener};
  const effect = kind === "Maintenance" ? " Scheduling only, not cleanup completion. The existing guard may defer busy work or reclaim zero bytes; no force mode." :
    action === "remove" ? " Volumes will be kept." :
    kind === "Container" && ["stop", "restart"].includes(action) ?
      `${action === "restart" ? " Restart disrupts this workload." : ""} The daemon waits up to 10 seconds, then may force-kill if it has not exited.` :
    kind === "Service" && action === "restart" ? " Restart disrupts this user service." : "";
  text("action-question", `${label}: ${target}?${effect} Cancel/Escape sends nothing before dispatch. Once dispatched, closing this view cannot undo the request.`);
  text("action-run", label);
  $("action-confirm").hidden = false;
  text("action-result", "");
  $("action-cancel").focus();
}
function cancelAction() {
  if (actionBusy) return;
  const opener = pendingAction?.opener;
  pendingAction = null; $("action-confirm").hidden = true;
  (canFocus(opener) && $("detail-dialog").contains(opener) ? opener : $("detail-close")).focus();
}
async function runAction() {
  if (actionBusy || !pendingAction || !snapshot?.actions?.nonce) return;
  const request = pendingAction;
  if (request.payload.kind === "cleanup" && !cleanupAllowed()) {
    pendingAction = null; $("action-confirm").hidden = true;
    text("action-result", cleanupPolicy()); $("detail-close").focus(); return;
  }
  if (request.payload.kind !== "cleanup" &&
      !workloadChoices(detailTarget.kind, detailTarget.item).some(([action]) => action === request.payload.action)) {
    pendingAction = null; $("action-confirm").hidden = true;
    text("action-result", "Action metadata/state changed; no unsupported request sent."); $("detail-close").focus(); return;
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
    const receipt = actionReceipt(result);
    applyActionObservation(request.payload, result);
    if (request.payload.kind === "cleanup") {
      text("action-result", receipt + cleanupReceipt(result) + (!response.ok || !result.ok ?
        " Refresh the observation before retrying; an interrupted request may still complete." : ""));
    } else {
      const message = result.message || result.error || `Action failed (HTTP ${response.status})`;
      text("action-result", receipt + message + (observedActionsFeature() ? ` ${workloadObservation(result.observation)}` : "") +
        (!response.ok || !result.ok ? " Refresh the observation before retrying; an interrupted request may still complete." : ""));
    }
  } catch (error) {
    if (observedActionsFeature()) {
      actionReceipt({});
      applyActionObservation(request.payload, {});
    }
    text("action-result", `${observedActionsFeature() ? `${unlinkedAttempt} ` : ""}${error.message}. Refresh the observation before retrying; an interrupted request may still complete.`);
  } finally {
    actionBusy = false; pendingAction = null;
    $("action-run").disabled = false; $("action-cancel").disabled = false; $("detail-close").disabled = false;
    $("action-confirm").hidden = true;
    drawActions();
    if ($("detail-dialog").open) $("detail-close").focus();
    refreshActionHistory(true);
    refresh(true);
  }
}
function inspectRow(values, kind, item) {
  const labels = kind === "Service" ? ["Service", "Scope", "State", "Description"] : ["Process", "PID", "CPU", "RAM"];
  const tr = row(values, labels), button = document.createElement("button");
  button.type = "button"; button.className = "row-button"; button.textContent = values[0];
  inspectControl(button, kind, item);
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
  const focus = rowFocus("services", "Service");
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
    `${rows.length} shown. ${[snapshot.system_services, snapshot.user_services].some(scope => scope?.truncated) ? "Inventory capped at 250 per scope." : snapshot.actions?.enabled ? "Tap a user service for advertised fixed controls; system units stay view-only." : "System + user scopes; observer mode."}`);
  restoreFocus(focus);
}
function processRows() {
  const focus = rowFocus("processes", "Process");
  const query = $("process-search").value.toLowerCase(), sort = $("process-sort").value;
  const rows = [...(snapshot?.processes?.items || [])].filter(item => `${item.name} ${item.pid}`.toLowerCase().includes(query));
  rows.sort((a, b) => sort === "name" ? a.name.localeCompare(b.name) :
    (sort === "memory" ? b.memory_bytes - a.memory_bytes : (b.cpu_percent ?? -1) - (a.cpu_percent ?? -1)) || a.pid - b.pid);
  $("processes").replaceChildren(...rows.map(item => inspectRow([item.name, item.pid, pct(item.cpu_percent), bytes(item.memory_bytes)], "Process", item)));
  if (!rows.length) $("processes").append(row([snapshot?.processes ? "No matching displayed processes" : "Process source unavailable", "", "", ""]));
  restoreFocus(focus);
}
function containerRows() {
  const focus = rowFocus("containers", "Container");
  $("containers").replaceChildren();
  const query = $("container-search").value.toLowerCase(), engine = $("container-engine").value || "all", state = $("container-state").value || "all";
  ["docker", "podman"].filter(tool => engine === "all" || tool === engine).forEach(tool => {
    const inventory = snapshot?.[tool];
    const heading = document.createElement("h3"); heading.textContent = tool === "docker" ? "Docker / local engine" : "Podman / current user";
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
      inspectControl(name, "Container", {...item, engine: tool});
      detail.className = "muted"; detail.textContent = `${tool === "podman" ? "Podman (current user)" : "Docker (local engine)"} / State: ${item.State || item.Status || "Unknown"} / Image: ${item.Image || "Unknown"}`;
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
  restoreFocus(focus);
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
  text("cache-detail", `${bytes(saved?.cache_budget_bytes)} soft budget / ${saved?.generated_at ? when(saved.generated_at) : "age unknown"}; cached, not a live scan`);
  detailFields(storageFields(saved), "storage-summary");
  list("storage-caches", (saved?.caches || []).map(cache =>
    `${cache.path || "Unknown cache"} (${cache.kind || "kind unknown"}): ${bytes(cache.bytes)} observed / ${bytes(cache.budget_bytes)} soft budget; ` +
    `active ${typeof cache.active === "boolean" ? cache.active : "Unknown"}; eligible ${typeof cache.eligible === "boolean" ? cache.eligible : "Unknown"} (saved decision, not permission); ${cache.reason || "reason unknown"}.`),
  Array.isArray(saved?.caches) ? "No cache records in the saved list; not whole-disk coverage." : "Cache activity/protection coverage unknown; no saved list.");
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
  list("protected", (saved?.workspaces || []).map(work => `${work.path || "Unknown workspace"}: saved state ${work.status || "Unknown"}`),
    Array.isArray(saved?.workspaces) ? "No workspace records in the saved list; not whole-disk coverage." : "Workspace protection/deferral coverage unknown; no saved list.");
}
function render(value, receipt = null) {
  snapshot = value;
  snapshotReceipt = receipt ? {...receipt, snapshot: value} : null;
  $("content").hidden = false;
  $("login").hidden = true;
  text("host", `${value.host} / ${value.actions?.enabled ? "homelab controls enabled" : "private, read-only monitoring"}`);
  badge("controls-status", value.actions?.enabled ? "Controls on - trusted access" : "Read-only", !value.actions?.enabled);
  const unsupported = [!cleanupFeature() && "guarded cleanup",
    !["Container", "Service"].every(kind => advertisedActions(kind).some(action => ["start", "restart"].includes(action))) && "recovery",
    !actionHistoryFeature() && "action history"].filter(Boolean);
  text("controls-compat", !value.actions?.enabled ? "Observer mode: inspection only; no control requests." :
    unsupported.length ? `Legacy/partial backend metadata: existing fixed controls remain available; ${unsupported.join(", ")} not advertised.` :
    "Backend-advertised fixed controls only; each action rechecks metadata and target state.");
  badge("connection", paused ? "Paused" : value.status === "healthy" ? "Live" : "Partial data", !paused && value.status === "healthy");
  liveControls();
  text("updated", `Snapshot observed ${when(value.generated_at)}`);
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
  renderStorage();
  $("filesystems").replaceChildren(...(value.filesystems || []).map(disk => row([disk.mount, bytes(disk.available_bytes), pct(disk.used_percent), disk.inode_used_percent == null ? "Unknown" : pct(disk.inode_used_percent)], ["Mount", "Available", "Used", "Inodes"])));
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
  renderActionHistory();
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
    refreshActionHistory(manual);
    refreshStorage(manual);
    const age = observationAge(value.generated_at);
    if (!Number.isFinite(age)) { badge("connection", "Age unknown / stale", false); notice("Server-relative snapshot age evidence is unavailable. Refresh for authoritative time evidence; guarded cleanup stays disabled."); }
    else if (age > (value.stale_after_seconds || 20)) { badge("connection", "Stale data", false); notice(`Last observation is ${Math.round(age)} seconds old; the collector may be delayed.`); }
    else notice("");
    sessionStorage.setItem("dashboard-token", token);
  } catch (error) {
    if (paused && !manual) return;
    badge("connection", paused ? "Paused / stale" : "Disconnected / stale", false);
    const retained = snapshot ? `Retaining the dated observation from ${when(snapshot.generated_at)}; values may be stale.` : "No snapshot has been observed yet.";
    notice(`${error.message}. ${retained} ${paused ? "Paused on this device. Use Refresh now or Resume updates to read again; no automatic retry." : "Retrying automatically."}`);
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
$("detail-close").addEventListener("click", () => { if (!actionBusy) $("detail-dialog").close(); });
$("detail-dialog").addEventListener("cancel", event => {
  if (actionBusy) event.preventDefault();
  else { pendingAction = null; $("action-confirm").hidden = true; }
});
$("detail-dialog").addEventListener("close", () => {
  pendingAction = null; $("action-confirm").hidden = true;
  const opener = detailOpener; detailOpener = null; restoreFocus(opener);
});
$("action-run").addEventListener("click", runAction);
$("action-cancel").addEventListener("click", cancelAction);
$("cleanup-details").addEventListener("click", event => openMaintenance(false, event?.currentTarget));
$("cleanup-run").addEventListener("click", event => openMaintenance(true, event?.currentTarget));
$("storage-root").addEventListener("change", () => { storagePresetRevision++; storagePresetPending = true; });
$("storage-start").addEventListener("click", startStorage);
$("storage-cancel").addEventListener("click", cancelStorage);
$("storage-back").addEventListener("click", () => {
  const job = storageView?.job;
  if (job?.breadcrumbs.length > 1) storageNavigate(job.breadcrumbs.at(-2).id, job.id);
});
document.querySelectorAll("[data-section-jump]").forEach(link => link.addEventListener("click", event => {
  const heading = $(link.getAttribute("data-section-jump"));
  event.preventDefault(); heading.focus({preventScroll: true}); heading.scrollIntoView({block: "start"});
}));
refresh();
