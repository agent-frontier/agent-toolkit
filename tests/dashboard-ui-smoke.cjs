const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function application() {
  const nodes = new Map();
  const requests = [];
  const fetchCalls = [];
  const pending = [], timers = new Map();
  const clock = {monotonic: 0};
  // Receipt evidence comes from a server Date header, never from the VM phone clock or read_at.
  const receiptFixture = () => ({
    serverTime: Date.parse(new Date().toUTCString()),
    requestedAt: clock.monotonic, receivedAt: clock.monotonic,
  });
  let nextTimer = 0;
  function element() {
    return {
      value: "", children: [], textContent: "", events: {}, attributes: {}, open: false,
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener(name, callback) { this.events[name] = callback; },
      setAttribute(name, value) { this.attributes[name] = value; },
      showModal() { this.open = true; },
      close() { this.open = false; },
    };
  }
  const document = {
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
    createElement: element,
    createElementNS: element,
  };
  const context = vm.createContext({
    document, URLSearchParams, location: { hash: "" },
    AbortSignal, performance: {now: () => clock.monotonic}, receiptFixture,
    clearTimeout(id) { timers.delete(id); },
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    fetch(url, options) { requests.push(url); fetchCalls.push({url, options}); return new Promise(resolve => pending.push(resolve)); },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    fixture: {
      system_services: { items: [{ unit: "active-system.service", scope: "system", active: "active", sub: "running", description: "system worker" }] },
      user_services: { items: [{ unit: "failed-user.service", scope: "user", active: "failed", sub: "failed", description: "user failure" }] },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../dashboard/app.js"), "utf8"), context);
  document.getElementById("service-state").value = "live";
  return { nodes, context, requests, pending, timers, fetchCalls, clock };
}

function snapshotResponse(value, date = new Date().toUTCString()) {
  return {
    status: 200, ok: true, headers: {get: name => name === "Date" ? date : null},
    json: async () => value,
  };
}

function maintenanceFixture() {
  const unit = "system-janitor-catchup.service";
  return {
    generated_at: new Date().toISOString(), stale_after_seconds: 20, host: "fixture", status: "healthy",
    system: {}, filesystems: [], network: [], history: [], errors: [],
    actions: {
      enabled: true, nonce: "fixture-cleanup-nonce", owner_uid: 1000,
      protected_services: [unit, "agent-toolkit-dashboard.service"],
      capabilities: ["cleanup-job-state", "guarded-cleanup"],
      cleanup: {kind: "cleanup", action: "run", unit, scope: "user", dispatch_only: true},
    },
    janitor: {
      service: {
        unit, id: unit, scope: "user", status: "available", load_state: "loaded",
        active: "inactive", sub: "dead", result: "success", exit_status: 0,
        invocation_id: "b".repeat(32), can_start: true, refuse_manual_start: false,
        read_at: new Date().toISOString(),
      },
      timer: {ActiveState: "active"},
      maintenance: {
        status: "cleaned", generated_at: "2020-01-02T00:00:00Z",
        cache_bytes: 128, cache_budget_bytes: 4096, cooldown_remaining_sec: 0,
        removed_bytes: 32, observed_reclaimed_bytes: 10, workspaces: [],
        actions: [{path: "/fixture/artifact", removed_bytes: 32}],
        service_invocation_id: "c".repeat(32),
      },
      maintenance_phase_linked: false,
      last_run: {run_id: "native-old", ended_at: "2020-01-01T00:00:00Z", freed_kb: 7, safety_integrity: "ok"},
    },
  };
}

function recoveryFixture() {
  const value = maintenanceFixture();
  Object.assign(value.actions, {
    capabilities: ["action-history", "cleanup-job-state", "guarded-cleanup", "observed-action-state", "workload-recovery"],
    container_actions: ["kill", "remove", "restart", "start", "stop"],
    service_actions: ["restart", "start", "stop"],
  });
  value.user_services = {items: [{unit: "fixture-worker.service", scope: "user", active: "active",
    sub: "running", description: "owned fixture"}]};
  value.podman = {status: "available", items: [{id: "a".repeat(64), ID: "a".repeat(12),
    Names: "owned-container", engine: "podman", Image: "local-fixture", State: "created", protected: false}]};
  return value;
}

function recoveryServiceState(active, changes = {}) {
  return {...maintenanceFixture().janitor.service, unit: "fixture-worker.service", id: "fixture-worker.service",
    state: active, active, sub: active === "active" ? "running" : "dead", protected: false, ...changes};
}

function actionHistoryFixture(events = [], changes = {}) {
  return {events, status: "available", truncated: false, read_at: new Date().toISOString(),
    detail: "Actual current-file receipts only.", quality: {
      tail_truncated: false, events_truncated: false, invalid_lines: 0, incomplete_first_line: false,
      incomplete_tail: false, changed_during_read: false,
    }, ...changes};
}

function actionHistoryView(nodes) {
  return {events: nodes.get("recent-actions").children.map(node => node.textContent),
    quality: nodes.get("actions-quality").textContent};
}

async function historyApplication() {
  const app = application();
  app.pending.shift()(snapshotResponse(recoveryFixture()));
  await new Promise(resolve => setImmediate(resolve));
  const started = {action_id: "a".repeat(32), kind: "service", action: "stop", event: "started",
    time: new Date().toISOString(), target: {scope: "user", unit: "fixture-worker.service"}};
  const history = actionHistoryFixture([started]);
  app.pending.shift()(snapshotResponse(history));
  await new Promise(resolve => setImmediate(resolve));
  return {...app, started, history};
}

function automaticSnapshot(app) {
  assert.equal(app.timers.size, 1);
  app.timers.values().next().value(); // Actual refresh() timer, not the manual refresh(true) path.
  assert.equal(app.fetchCalls.at(-1).url, "/api/snapshot");
}

test("opening the plain URL requests data without a token or a terminal command", () => {
  const { requests } = application();
  assert.deepEqual(requests, ["/api/snapshot"]);
});

test("failed user services stay above active system services", () => {
  // Per-scope sorting is insufficient when the UI merges both inventories.
  const { nodes, context } = application();
  vm.runInContext("snapshot=fixture; serviceRows();", context);
  assert.deepEqual(nodes.get("services").children.map(row => row.children[0].textContent),
    ["failed-user.service", "active-system.service"]);
});

test("mixed-scope service filtering retains explicit failed and empty states", () => {
  const { nodes, context } = application();
  nodes.get("service-state").value = "failed";
  vm.runInContext("snapshot=fixture; serviceRows();", context);
  assert.equal(nodes.get("services").children[0].children[0].textContent, "failed-user.service");
  nodes.get("service-search").value = "no matching service";
  vm.runInContext("serviceRows();", context);
  assert.equal(nodes.get("services").children[0].children[0].textContent, "No matching observed services");
});

test("service name sorting is explicit while the default remains failed-first", () => {
  const { nodes, context } = application();
  nodes.get("service-sort").value = "name";
  vm.runInContext("snapshot=fixture; serviceRows();", context);
  assert.equal(nodes.get("services").children[0].children[0].textContent, "active-system.service");
});

test("displayed process sample can be searched and sorted by RAM", () => {
  const { nodes, context } = application();
  context.processes = {items: [
    {name: "cpu-worker", pid: 11, cpu_percent: 80, memory_bytes: 10},
    {name: "memory-worker", pid: 22, cpu_percent: 2, memory_bytes: 200},
  ]};
  nodes.get("process-sort").value = "memory";
  vm.runInContext("snapshot={processes}; processRows();", context);
  assert.equal(nodes.get("processes").children[0].children[0].textContent, "memory-worker");
  nodes.get("process-search").value = "11";
  vm.runInContext("processRows();", context);
  assert.equal(nodes.get("processes").children.length, 1);
  assert.equal(nodes.get("processes").children[0].children[0].textContent, "cpu-worker");
});

test("Podman created containers remain visible and engine/state filters work", () => {
  const { nodes, context } = application();
  context.inventories = {
    docker: {status: "available", items: [{Names: "docker-running", Image: "image", State: "running", Status: "Up"}]},
    podman: {status: "available", items: [{Names: "podman-created", Image: "image", State: "created", Status: "Created"}]},
  };
  vm.runInContext("snapshot=inventories; containerRows();", context);
  assert.deepEqual(nodes.get("containers").children.filter(node => node.className === "workload").map(node => node.children[0].textContent),
    ["docker-running", "podman-created"]);
  nodes.get("container-engine").value = "podman";
  nodes.get("container-state").value = "stopped";
  vm.runInContext("containerRows();", context);
  assert.equal(nodes.get("containers").children.find(node => node.className === "workload").children[0].textContent, "podman-created");
  nodes.get("container-state").value = "running";
  vm.runInContext("containerRows();", context);
  assert.equal(nodes.get("containers").children.filter(node => node.className === "workload").length, 0);
});

test("chart ranges use the latest observed timestamp even while paused", () => {
  const { nodes, context } = application();
  context.points = [{ts: "2020-01-01T00:00:00Z"}, {ts: "2020-01-01T00:09:00Z"}, {ts: "2020-01-01T00:10:00Z"}];
  nodes.get("history-window").value = "5";
  assert.equal(vm.runInContext("snapshot={history:points}; historyRows().length", context), 2);
  nodes.get("history-window").value = "0";
  assert.equal(vm.runInContext("historyRows().length", context), 3);
});

test("tap details use snapshot text and a dismissible dialog, not commands", () => {
  const { nodes, context } = application();
  vm.runInContext("snapshot={generated_at:'2020-01-01T00:00:00Z'}; showDetails('Process',{name:'<script>not executable</script>',pid:22,cpu_percent:null,memory_bytes:100});", context);
  assert.equal(nodes.get("detail-dialog").open, true);
  assert.equal(nodes.get("detail-fields").children[1].textContent, "<script>not executable</script>");
  nodes.get("detail-close").events.click();
  assert.equal(nodes.get("detail-dialog").open, false);
});

test("pause freezes an in-flight automatic update; manual refresh and resume do not overlap requests", async () => {
  const { nodes, context, requests, pending, timers } = application();
  context.published = [];
  vm.runInContext("render=value=>{snapshot=value;published.push(value);};", context);
  const response = () => snapshotResponse({generated_at: new Date().toISOString(), status: "healthy"});
  nodes.get("live-toggle").events.click();
  pending.shift()(response());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.published.length, 0);
  assert.equal(timers.size, 0);
  nodes.get("refresh-now").events.click();
  nodes.get("refresh-now").events.click();
  assert.equal(requests.length, 2);
  pending.shift()(response());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.published.length, 1);
  assert.equal(timers.size, 0);
  nodes.get("live-toggle").events.click();
  assert.equal(requests.length, 3);
  pending.shift()(response());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.size, 1);
  assert.equal(nodes.get("live-toggle").attributes["aria-pressed"], "false");
});

test("kill requires an exact-target confirmation and posts its stable process identity once", async () => {
  const { nodes, context, requests, pending, fetchCalls } = application();
  vm.runInContext("snapshot={generated_at:'2020-01-01T00:00:00Z',actions:{enabled:true,nonce:'fixture-nonce'}}; showDetails('Process',{name:'worker',pid:22,start_time_ticks:123,controllable:true,cpu_percent:50,memory_bytes:100});", context);
  const kill = nodes.get("action-controls").children.find(button => button.textContent === "Force kill");
  kill.events.click();
  assert.equal(requests.length, 1);
  assert.match(nodes.get("action-question").textContent, /worker \(PID 22\)/);
  nodes.get("action-cancel").events.click();
  assert.equal(requests.length, 1);
  kill.events.click();
  nodes.get("action-run").events.click();
  nodes.get("action-run").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
  const posted = fetchCalls.find(call => call.url === "/api/action");
  assert.equal(posted.options.headers["X-Action-Token"], "fixture-nonce");
  assert.deepEqual(JSON.parse(posted.options.body), {kind: "process", pid: 22, start_time_ticks: 123, action: "kill"});
  pending[1]({ok: true, status: 200, json: async () => ({ok: true, message: "Signal sent"})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes.get("action-result").textContent, "Signal sent");
  assert.equal(nodes.get("detail-close").disabled, false);
});

test("observer/protected items have no controls; stopped-container removal keeps volumes", () => {
  const { nodes, context } = application();
  vm.runInContext("snapshot={actions:{enabled:false}}; showDetails('Process',{name:'worker',pid:22,controllable:true});", context);
  assert.equal(nodes.get("action-controls").hidden, true);
  vm.runInContext("snapshot={actions:{enabled:true,nonce:'fixture-nonce',protected_services:['agent-toolkit-dashboard.service']}}; showDetails('Service',{unit:'agent-toolkit-dashboard.service',scope:'user'});", context);
  assert.equal(nodes.get("action-controls").hidden, true);
  vm.runInContext("showDetails('Process',{pid:1,name:'systemd',controllable:false});", context);
  assert.equal(nodes.get("action-controls").hidden, true);
  context.container = {id: "a".repeat(64), ID: "a".repeat(12), Names: "fixture", engine: "podman", State: "created"};
  vm.runInContext("showDetails('Container',container);", context);
  assert.equal(nodes.get("action-controls").children.length, 1);
  nodes.get("action-controls").children[0].events.click();
  assert.match(nodes.get("action-question").textContent, /Volumes will be kept/);
  context.container.State = "running";
  vm.runInContext("showDetails('Container',container);", context);
  nodes.get("action-controls").children.find(button => button.textContent === "Stop gracefully").events.click();
  assert.match(nodes.get("action-question").textContent, /10 seconds.*force-kill/);
});

test("Maintenance Cancel posts zero; double Confirm posts exactly one fixed guarded cleanup", async () => {
  // Exercise the actual app listeners, payload and receipt rather than counting fake button calls.
  const { nodes, context, requests, pending, fetchCalls } = application();
  context.maintenance = maintenanceFixture();
  vm.runInContext("render(maintenance, receiptFixture());", context);
  assert.equal(nodes.get("cleanup-run").hidden, false);
  assert.equal(nodes.get("cleanup-run").disabled, false);
  assert.match(nodes.get("cleanup-run").textContent, /Run.*cleanup/);
  nodes.get("cleanup-run").events.click();
  assert.equal(nodes.get("detail-dialog").open, true);
  assert.equal(nodes.get("detail-title").textContent, "Maintenance details");
  assert.match(nodes.get("action-question").textContent, /system-janitor-catchup\.service \(current user\)/);
  assert.match(nodes.get("action-question").textContent, /Scheduling only.*defer.*zero bytes.*no force/);
  nodes.get("action-cancel").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
  nodes.get("action-controls").children.find(button => /Run.*cleanup/.test(button.textContent)).events.click();
  nodes.get("action-run").events.click();
  nodes.get("action-run").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
  const posted = fetchCalls.find(call => call.url === "/api/action");
  assert.deepEqual(JSON.parse(posted.options.body), {kind: "cleanup", action: "run"});
  assert.equal(posted.options.headers["Content-Type"], "application/json");
  assert.equal(posted.options.headers["X-Action-Token"], "fixture-cleanup-nonce");
  assert.equal(nodes.get("cleanup-run").disabled, true);
  const observation = {...context.maintenance.janitor.service, active: "activating", sub: "start"};
  pending[1]({ok: true, status: 200, json: async () => ({
    ok: true, dispatch_only: true, message: "Guarded scheduling accepted.",
    dispatch: {status: "accepted", dispatched: true}, observation,
  })});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get("action-result").textContent, /dispatch only, not cleanup completion/);
  assert.match(nodes.get("action-result").textContent, /Fresh unit observation:.*activating.*start/);
  assert.equal(nodes.get("detail-close").disabled, false);
  nodes.get("action-run").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
});

test("new assets keep old manifests view-only for cleanup while retaining legacy workload controls", () => {
  // Absence of approved metadata must prevent an unsupported new kind reaching an older backend.
  const { nodes, context, requests } = application();
  context.maintenance = maintenanceFixture();
  delete context.maintenance.actions.cleanup;
  delete context.maintenance.actions.capabilities;
  delete context.maintenance.janitor.service;
  vm.runInContext("render(maintenance, receiptFixture());", context);
  assert.equal(nodes.get("cleanup-run").hidden, true);
  assert.equal(nodes.get("cleanup-details").textContent, "View last cleanup");
  assert.equal(nodes.get("maintenance-status").textContent, "cleaned");
  nodes.get("cleanup-details").events.click();
  assert.equal(nodes.get("detail-title").textContent, "Cleanup details");
  assert.equal(nodes.get("detail-fields").children[1].textContent, "native-old");
  vm.runInContext("showDetails('Maintenance',maintenance.janitor); confirmAction('run','Run guarded cleanup'); runAction();", context);
  assert.equal(nodes.get("action-controls").hidden, true);
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
  vm.runInContext("showDetails('Process',{pid:22,name:'worker',start_time_ticks:123,controllable:true});", context);
  assert.deepEqual(nodes.get("action-controls").children.map(button => button.textContent), ["Terminate", "Force kill"]);
  vm.runInContext("showDetails('Service',{unit:'system-janitor-catchup.service',scope:'user'});", context);
  assert.equal(nodes.get("action-controls").hidden, true);
});

test("cleanup requires approved metadata and a fresh loaded canonical startable user unit", () => {
  // Cached success, missing/masked units, stale reads and busy runners cannot grant dispatch authority.
  const { nodes, context, requests } = application();
  const changes = [
    {status: "unknown"}, {status: "unavailable", load_state: "masked"},
    {load_state: "not-found"}, {id: "different.service"}, {scope: "system"},
    {read_at: "2020-01-01T00:00:00Z"}, {read_at: "not a date"},
    {read_at: new Date(Date.now() + 120000).toISOString()},
    {can_start: false}, {refuse_manual_start: true},
    {active: "active"}, {active: "activating"}, {active: "deactivating"},
  ];
  for (const change of changes) {
    context.maintenance = maintenanceFixture();
    Object.assign(context.maintenance.janitor.service, change);
    vm.runInContext("render(maintenance, receiptFixture());", context);
    assert.equal(nodes.get("cleanup-run").disabled, true, JSON.stringify(change));
    nodes.get("cleanup-run").events.click();
    nodes.get("action-run").events.click();
    assert.equal(nodes.get("action-controls").children.length, 0);
  }
  for (const change of [
    {capabilities: ["guarded-cleanup"]},
    {cleanup: {kind: "cleanup", action: "run", unit: "different.service", scope: "user", dispatch_only: true}},
    {cleanup: {kind: "cleanup", action: "run", unit: "system-janitor-catchup.service", scope: "user", dispatch_only: "true"}},
  ]) {
    context.maintenance = maintenanceFixture();
    Object.assign(context.maintenance.actions, change);
    vm.runInContext("render(maintenance, receiptFixture());", context);
    assert.equal(nodes.get("cleanup-run").hidden, true);
    nodes.get("cleanup-run").events.click();
    nodes.get("action-run").events.click();
  }
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
});

test("cleanup confirmation rechecks metadata at dispatch if the backend was downgraded", () => {
  // A confirmation opened against new metadata is not authority after a subsequent old snapshot.
  const { nodes, context, requests } = application();
  context.maintenance = maintenanceFixture();
  vm.runInContext("render(maintenance, receiptFixture());", context);
  nodes.get("cleanup-run").events.click();
  delete context.maintenance.actions.cleanup;
  vm.runInContext("render(maintenance, receiptFixture());", context);
  nodes.get("action-run").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
  assert.match(nodes.get("action-result").textContent, /not supported.*metadata/);
});

test("the live runner badge outranks old cleaned cache and saved phases stay budget-only", () => {
  // An equal invocation links a phase, not completion of the wrapper that may still run native Janitor.
  const { nodes, context } = application();
  context.maintenance = maintenanceFixture();
  context.maintenance.janitor.service.active = "active";
  context.maintenance.janitor.service.sub = "running";
  vm.runInContext("render(maintenance, receiptFixture());", context);
  assert.equal(nodes.get("maintenance-status").textContent, "Runner running");
  let summary = nodes.get("maintenance-summary").children.map(node => node.textContent).join(" ");
  assert.match(summary, /Saved budget phase cleaned/);
  assert.match(summary, /Dated\/unlinked last result/);
  assert.match(summary, /Native Janitor result \(separate\)/);
  assert.match(summary, /Saved removed artifact bytes 32 B Saved observed free-space change 10 B/);
  assert.match(nodes.get("maintenance-actions").textContent, /separate, not a guaranteed reclaim total/);
  context.maintenance.janitor.maintenance_phase_linked = true;
  context.maintenance.janitor.maintenance.service_invocation_id = context.maintenance.janitor.service.invocation_id;
  vm.runInContext("render(maintenance, receiptFixture());", context);
  summary = nodes.get("maintenance-summary").children.map(node => node.textContent).join(" ");
  assert.match(summary, /Linked.*budget phase only, not whole-wrapper or native Janitor completion/);
  assert.equal(nodes.get("maintenance-status").textContent, "Runner running");
  context.maintenance.janitor.maintenance.service_invocation_id = "c".repeat(32);
  vm.runInContext("render(maintenance, receiptFixture());", context);
  summary = nodes.get("maintenance-summary").children.map(node => node.textContent).join(" ");
  assert.match(summary, /Dated\/unlinked last result/);
  Object.assign(context.maintenance.janitor.service, {active: "failed", sub: "failed", result: "exit-code", exit_status: 7});
  vm.runInContext("render(maintenance, receiptFixture());", context);
  assert.equal(nodes.get("maintenance-status").textContent, "Runner failed");
});

test("deferred, zero, unavailable and unknown Maintenance observations are distinct", () => {
  // No record or failed DBus read must not masquerade as a healthy zero-reclamation run.
  const { nodes, context } = application();
  context.maintenance = maintenanceFixture();
  Object.assign(context.maintenance.janitor.maintenance, {status: "deferred", actions: [], removed_bytes: 0, observed_reclaimed_bytes: 0});
  vm.runInContext("render(maintenance, receiptFixture());", context);
  assert.match(nodes.get("maintenance-actions").textContent, /phase deferred.*Busy or unbacked/);
  context.maintenance.janitor.maintenance.status = "idle";
  vm.runInContext("render(maintenance, receiptFixture());", context);
  assert.match(nodes.get("maintenance-actions").textContent, /No deletion recorded.*zero work is valid/);
  delete context.maintenance.janitor.maintenance;
  Object.assign(context.maintenance.janitor.service, {status: "unknown", detail: "Fixture DBus failure.", exit_status: null});
  vm.runInContext("render(maintenance, receiptFixture());", context);
  assert.equal(nodes.get("maintenance-status").textContent, "Runner unknown");
  assert.match(nodes.get("maintenance-actions").textContent, /phase unknown; no result yet/);
  assert.equal(nodes.get("cache").textContent, "Unknown");
  context.maintenance.janitor.service.status = "unavailable";
  vm.runInContext("render(maintenance, receiptFixture());", context);
  assert.equal(nodes.get("maintenance-status").textContent, "Runner unavailable");
  assert.match(nodes.get("cleanup-policy").textContent, /No fallback cleanup/);
});

test("an accepted scheduling request with unknown unit state never retries cleanup automatically", async () => {
  // A timeout after launch is a request receipt plus an observation gap, not permission to replay.
  const { nodes, context, requests, pending } = application();
  context.maintenance = maintenanceFixture();
  vm.runInContext("render(maintenance, receiptFixture());", context);
  nodes.get("cleanup-run").events.click();
  nodes.get("action-run").events.click();
  pending[1]({ok: false, status: 504, json: async () => ({
    ok: false, outcome: "unknown", dispatch_only: true,
    error: "Scheduling accepted; execution outcome unknown.",
    dispatch: {status: "accepted", dispatched: true},
    observation: {...context.maintenance.janitor.service, status: "unknown", detail: "State timeout."},
  })});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get("action-result").textContent, /execution outcome unknown.*not cleanup completion.*Fresh unit observation:.*unknown/);
  assert.match(nodes.get("action-result").textContent, /Refresh the observation before retrying/);
  nodes.get("action-run").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
});

for (const skew of [120000, -120000]) {
  test(`fresh server unit allows guarded cleanup with ${skew / 1000}s phone clock skew`, async () => {
    // Real receipt parsing and app controls must work without synchronized phone/server wall clocks.
    const { nodes, context, pending, fetchCalls } = application();
    const serverNow = Date.now(), value = maintenanceFixture();
    value.generated_at = value.janitor.service.read_at = new Date(serverNow).toISOString();
    context.phoneNow = serverNow + skew;
    vm.runInContext("Date.now=()=>phoneNow;", context);
    pending.shift()(snapshotResponse(value, new Date(serverNow).toUTCString()));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(vm.runInContext("cleanupAllowed()", context), true);
    assert.equal(nodes.get("cleanup-run").disabled, false);
    assert.equal(nodes.get("connection").textContent, "Live");
    nodes.get("cleanup-run").events.click();
    nodes.get("action-run").events.click();
    assert.deepEqual(JSON.parse(fetchCalls.find(call => call.url === "/api/action").options.body),
      {kind: "cleanup", action: "run"});
  });
}

test("fresh HTTP Date cannot renew an old service read or an old cached snapshot", async () => {
  // New receipt time is not new observation time: both source dates remain load-bearing.
  for (const source of ["service", "cache"]) {
    const { nodes, context, pending } = application();
    const serverNow = Date.now(), value = maintenanceFixture();
    value.generated_at = value.janitor.service.read_at = new Date(serverNow).toISOString();
    if (source === "service") value.janitor.service.read_at = new Date(serverNow - 60000).toISOString();
    else value.generated_at = new Date(serverNow - 60000).toISOString();
    pending.shift()(snapshotResponse(value, new Date(serverNow).toUTCString()));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(vm.runInContext("cleanupAllowed()", context), false, source);
    assert.equal(nodes.get("cleanup-run").disabled, true);
    if (source === "cache") {
      assert.equal(nodes.get("connection").textContent, "Stale data");
      assert.match(nodes.get("notice").textContent, /observation is .* seconds old/);
    }
  }
});

test("paused displayed receipts expire by elapsed monotonic time even if phone wall time stands still", async () => {
  // A frozen page and an adjusted phone wall clock cannot keep an old guarded observation fresh.
  const { nodes, context, pending, clock } = application();
  const serverNow = Date.now(), value = maintenanceFixture();
  value.generated_at = value.janitor.service.read_at = new Date(serverNow).toISOString();
  context.phoneNow = serverNow;
  vm.runInContext("Date.now=()=>phoneNow;", context);
  pending.shift()(snapshotResponse(value, new Date(serverNow).toUTCString()));
  await new Promise(resolve => setImmediate(resolve));
  nodes.get("live-toggle").events.click();
  assert.equal(vm.runInContext("cleanupAllowed()", context), true);
  clock.monotonic = 30000;
  assert.equal(vm.runInContext("cleanupAllowed()", context), false);
  nodes.get("cleanup-run").events.click();
  assert.equal(nodes.get("action-controls").children.length, 0);
  assert.match(nodes.get("detail-policy").textContent, /unknown or stale/);
});

test("an unrendered paused response cannot replace the displayed snapshot's valid receipt evidence", async () => {
  // Invalid receipt evidence from discarded data must neither clear nor rejuvenate the frozen view.
  const { nodes, context, pending, clock } = application();
  const serverNow = Date.now(), value = maintenanceFixture();
  value.generated_at = value.janitor.service.read_at = new Date(serverNow).toISOString();
  context.phoneNow = serverNow;
  vm.runInContext("Date.now=()=>phoneNow;", context);
  pending.shift()(snapshotResponse(value, new Date(serverNow).toUTCString()));
  await new Promise(resolve => setImmediate(resolve));
  const displayed = vm.runInContext("snapshot", context);
  vm.runInContext("refresh();", context);
  nodes.get("live-toggle").events.click();
  clock.monotonic = 15000;
  pending.shift()(snapshotResponse(maintenanceFixture(), "invalid Date"));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext("snapshot", context), displayed);
  assert.equal(vm.runInContext("cleanupAllowed()", context), true);
  clock.monotonic = 30000;
  assert.equal(vm.runInContext("cleanupAllowed()", context), false);
});

test("missing or malformed server Date evidence is explicit and never grants cleanup freshness", async () => {
  // Browser wall time is not a fallback authority, even when it happens to match the server.
  for (const date of [null, "", "invalid Date", "0", "Wed, 05 Nov 2026 01:34:08 GMT"]) {
    const { nodes, context, pending } = application();
    pending.shift()(snapshotResponse(maintenanceFixture(), date));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(vm.runInContext("cleanupAllowed()", context), false, String(date));
    assert.equal(nodes.get("cleanup-run").disabled, true);
    assert.match(nodes.get("cleanup-policy").textContent, /Server-relative.*age.*unavailable/);
    assert.equal(nodes.get("connection").textContent, "Age unknown / stale");
    assert.match(nodes.get("notice").textContent, /server-relative.*age.*unavailable/i);
  }
});

test("transport and JSON delay consume freshness instead of dating old data at render time", async () => {
  // Header precision, request transit and delayed parsing must count against the same stale limit.
  const { nodes, context, pending, clock } = application();
  const serverNow = Date.now(), value = maintenanceFixture();
  value.generated_at = value.janitor.service.read_at = new Date(serverNow).toISOString();
  context.phoneNow = serverNow;
  vm.runInContext("Date.now=()=>phoneNow;", context);
  let parsed;
  const response = snapshotResponse(value, new Date(serverNow).toUTCString());
  response.json = () => new Promise(resolve => { parsed = resolve; });
  clock.monotonic = 5000;
  pending.shift()(response);
  await new Promise(resolve => setImmediate(resolve));
  clock.monotonic = 30000;
  parsed(value);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext("cleanupAllowed()", context), false);
  assert.equal(nodes.get("cleanup-run").disabled, true);
  assert.equal(nodes.get("connection").textContent, "Stale data");
});

test("advertised recovery choices keep legacy backends safe and disclose exact-ID restart grace", () => {
  // New verbs require real lists/capabilities; old assets/backends keep only their original fixed choices.
  const {nodes, context, requests} = application();
  context.recovery = recoveryFixture();
  vm.runInContext("render(recovery, receiptFixture()); showDetails('Container',recovery.podman.items[0]);", context);
  assert.deepEqual(nodes.get("action-controls").children.map(button => button.textContent), ["Start container", "Remove container"]);
  nodes.get("action-controls").children[0].events.click();
  assert.match(nodes.get("action-question").textContent, new RegExp(`podman, ${"a".repeat(64)}`));
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
  for (const engine of ["docker", "podman"]) {
    context.recovery.podman.items[0].engine = engine;
    context.recovery.podman.items[0].State = "running";
    vm.runInContext("showDetails('Container',recovery.podman.items[0]);", context);
    nodes.get("action-controls").children.find(button => button.textContent === "Restart container").events.click();
    assert.match(nodes.get("action-question").textContent, /Restart disrupts.*10 seconds.*force-kill/);
  }
  for (const state of ["paused", "restarting", "unknown"]) {
    context.recovery.podman.items[0].State = state;
    vm.runInContext("showDetails('Container',recovery.podman.items[0]);", context);
    assert.equal(nodes.get("action-controls").children.some(button => button.textContent === "Restart container"), false);
  }
  delete context.recovery.actions.container_actions;
  delete context.recovery.actions.service_actions;
  context.recovery.podman.items[0].State = "created";
  vm.runInContext("render(recovery, receiptFixture()); showDetails('Container',recovery.podman.items[0]); refreshActionHistory(); confirmAction('start','Start container'); runAction();", context);
  assert.deepEqual(nodes.get("action-controls").children.map(button => button.textContent), ["Remove container"]);
  assert.equal(requests.filter(url => url === "/api/actions").length, 0);
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
  vm.runInContext("showDetails('Service',recovery.user_services.items[0]);", context);
  assert.deepEqual(nodes.get("action-controls").children.map(button => button.textContent), ["Stop service"]);
});

test("a recovery confirmation cannot post a verb removed by downgraded metadata", () => {
  // A once-visible button is not authority after a new snapshot removes its advertised action list.
  const {nodes, context, requests} = application();
  context.recovery = recoveryFixture();
  vm.runInContext("render(recovery, receiptFixture()); showDetails('Container',recovery.podman.items[0]);", context);
  nodes.get("action-controls").children.find(button => button.textContent === "Start container").events.click();
  delete context.recovery.actions.container_actions;
  vm.runInContext("render(recovery, receiptFixture());", context);
  nodes.get("action-run").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
  assert.match(nodes.get("action-result").textContent, /no unsupported request sent/);
});

test("retained service dialog survives row GC for Stop -> Start -> Restart -> Stop", async () => {
  // Use actual listeners and returned exact observations; inventory disappearance is never evidence of stopping.
  const {nodes, context, pending, fetchCalls, requests} = application();
  context.recovery = recoveryFixture();
  vm.runInContext("render(recovery, receiptFixture()); showDetails('Service',recovery.user_services.items[0]);", context);
  for (const [index, verb, label, state] of [
    [1, "stop", "Stop service", "inactive"], [2, "start", "Start service", "active"],
    [3, "restart", "Restart service", "active"], [4, "stop", "Stop service", "inactive"],
  ]) {
    nodes.get("action-controls").children.find(button => button.textContent === label).events.click();
    nodes.get("action-run").events.click();
    nodes.get("action-run").events.click();
    const posted = fetchCalls.filter(call => call.url === "/api/action").at(-1);
    assert.deepEqual(JSON.parse(posted.options.body),
      {kind: "service", scope: "user", unit: "fixture-worker.service", action: verb});
    pending.at(-1)({ok: true, status: 200, json: async () => ({
      ok: true, action_id: String(index).repeat(32), message: `User service ${verb} completed.`,
      observation: recoveryServiceState(state),
    })});
    await new Promise(resolve => setImmediate(resolve));
    assert.match(nodes.get("action-result").textContent, /Request receipt.*Exact target observation:/);
    assert.match(nodes.get("action-result").textContent, new RegExp(`observation: ${state}; server read`));
    assert.equal(nodes.get("action-controls").hidden, false);
    if (index === 1) {
      assert.equal(context.recovery.user_services.items[0].active, "active");
      context.recovery.user_services = {items: [], truncated: true};
      vm.runInContext("render(recovery, receiptFixture());", context);
      assert.equal(nodes.get("services").children[0].children[0].textContent, "No matching observed services");
      assert.equal(nodes.get("action-controls").children.some(button => button.textContent === "Start service"), true);
    }
  }
  assert.equal(requests.filter(url => url === "/api/action").length, 4);
  assert.equal(nodes.get("action-controls").children.some(button => button.textContent === "Start service"), true);
});

test("a successful receipt with unknown observation grants no unsafe controls or invented app health", async () => {
  // Read uncertainty is not command failure and cannot turn an old running row into recovery authority.
  const {nodes, context, pending, requests} = application();
  context.recovery = recoveryFixture();
  context.recovery.podman.items[0].State = "running";
  vm.runInContext("render(recovery, receiptFixture()); showDetails('Container',recovery.podman.items[0]);", context);
  nodes.get("action-controls").children.find(button => button.textContent === "Stop gracefully").events.click();
  nodes.get("action-run").events.click();
  pending[1]({ok: true, status: 200, json: async () => ({
    ok: true, action_id: "d".repeat(32), message: "Container stop completed.",
    observation: {id: "a".repeat(64), engine: "podman", state: "unknown", status: "unknown",
      read_at: new Date().toISOString(), detail: "Exact read malformed; receipt unchanged."},
  })});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get("action-result").textContent, /Request receipt.*Container stop completed.*observation: unknown/);
  assert.doesNotMatch(nodes.get("action-result").textContent, /Action failed|healthy/);
  assert.equal(nodes.get("action-controls").hidden, true);
  nodes.get("action-run").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
});

test("lost response or action ID remains an UNLINKED browser attempt even beside same-target history", async () => {
  // No action ID means no association by name/time; later history, reopening and refresh cannot replay a mutation.
  for (const lost of ["response", "id"]) {
    const {nodes, context, pending, requests} = application();
    context.recovery = recoveryFixture();
    vm.runInContext("render(recovery, receiptFixture()); showDetails('Service',recovery.user_services.items[0]);", context);
    nodes.get("action-controls").children.find(button => button.textContent === "Stop service").events.click();
    nodes.get("action-run").events.click();
    pending[1]({ok: true, status: 200, json: async () => {
      if (lost === "response") throw new Error("Response lost");
      return {ok: true, message: "Stop receipt body without ID.", observation: recoveryServiceState("inactive")};
    }});
    await new Promise(resolve => setImmediate(resolve));
    assert.match(nodes.get("action-result").textContent, /UNLINKED browser attempt.*no action ID/);
    const events = [{action_id: "b".repeat(32), kind: "service", action: "stop", event: "started",
      time: new Date().toISOString(), target: {scope: "user", unit: "fixture-worker.service"}}];
    pending[2](snapshotResponse(actionHistoryFixture(events)));
    await new Promise(resolve => setImmediate(resolve));
    assert.match(nodes.get("unlinked-attempt").textContent, /Not attached to history by target or time/);
    assert.doesNotMatch(nodes.get("unlinked-attempt").textContent, /bbbbbbbb/);
    assert.match(nodes.get("recent-actions").children[0].textContent, /bbbbbbbb.*unresolved/);
    vm.runInContext("render(recovery, receiptFixture()); showDetails('Service',recovery.user_services.items[0]); runAction(); refreshActionHistory();", context);
    assert.equal(requests.filter(url => url === "/api/action").length, 1);
  }
});

test("recent activity uses actual IDs, preserves started-only/C1 dispatch semantics and exposes quality", async () => {
  // Same target/time but different actual IDs never resolves a started-only record or claims cleanup completed.
  const {nodes, context, pending, requests} = application();
  context.recovery = recoveryFixture();
  const time = new Date().toISOString(), target = {scope: "user", unit: "fixture-worker.service"};
  const events = [
    {action_id: "a".repeat(32), kind: "service", action: "restart", event: "started", time, target},
    {action_id: "d".repeat(32), kind: "service", action: "restart", event: "failed", time, target},
    {action_id: "b".repeat(32), kind: "cleanup", action: "run", event: "accepted", time},
    {action_id: "c".repeat(32), kind: "cleanup", action: "run", event: "observed", time, nonce: "secret-extra"},
  ];
  vm.runInContext("render(recovery, receiptFixture()); refreshActionHistory(); refreshActionHistory();", context);
  assert.equal(requests.filter(url => url === "/api/actions").length, 1);
  pending[1](snapshotResponse(actionHistoryFixture(events, {status: "partial", truncated: true, quality: {
    tail_truncated: true, events_truncated: true, invalid_lines: 2, incomplete_first_line: true,
    incomplete_tail: true, changed_during_read: false,
  }})));
  await new Promise(resolve => setImmediate(resolve));
  const lines = nodes.get("recent-actions").children.map(node => node.textContent);
  assert.match(lines[0], /aaaaaaaa.*started; unresolved/);
  assert.match(lines[2], /accepted scheduling only, not cleanup completion/);
  assert.match(lines[3], /existing runner observed; no duplicate scheduled/);
  assert.doesNotMatch(lines.join(" "), /secret-extra/);
  assert.match(nodes.get("actions-quality").textContent, /partial.*invalid lines: 2.*incomplete tail: true/);
  vm.runInContext("refreshActionHistory();", context);
  pending[2](snapshotResponse(actionHistoryFixture([], {status: "no_history_yet"})));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get("recent-actions").children[0].textContent, /NoHistoryYet/);
  vm.runInContext("refreshActionHistory();", context);
  pending[3](snapshotResponse(actionHistoryFixture([], {quality: {invalid_lines: "not numeric"}})));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get("actions-quality").textContent, /Unavailable.*Malformed history response.*no replay/);
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
});

test("per-verb service UI does not confuse CanStart false with protected or unavailable Stop", () => {
  // A readable loaded user unit can be stopped despite manual-start policy; protected requested/canonical IDs stay view-only.
  const {nodes, context} = application();
  context.recovery = recoveryFixture();
  context.unit = {...context.recovery.user_services.items[0], id: "fixture-worker.service",
    load_state: "loaded", can_start: false, refuse_manual_start: true, status: "unavailable"};
  vm.runInContext("render(recovery, receiptFixture()); showDetails('Service',unit);", context);
  assert.deepEqual(nodes.get("action-controls").children.map(button => button.textContent), ["Stop service"]);
  context.unit.id = "agent-toolkit-dashboard.service";
  vm.runInContext("showDetails('Service',unit);", context);
  assert.equal(nodes.get("action-controls").hidden, true);
  context.unit.id = "fixture-worker.service";
  context.unit.unit = "agent-toolkit-dashboard.service";
  vm.runInContext("showDetails('Service',unit);", context);
  assert.equal(nodes.get("action-controls").hidden, true);
});

test("DP2 history pause freezes delayed automatic publication; manual snapshot and resume still publish", async () => {
  // A Pause during response.json() must freeze both receipt meaning and quality, not just stop future requests.
  const app = await historyApplication(), {nodes, context, pending, requests, timers} = app;
  const frozen = actionHistoryView(nodes);
  assert.match(frozen.events[0], /started; unresolved/);
  automaticSnapshot(app);
  pending.shift()(snapshotResponse(recoveryFixture()));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.fetchCalls.at(-1).url, "/api/actions");
  const terminal = actionHistoryFixture([app.started, {...app.started, event: "succeeded"}],
    {detail: "Automatic terminal history", status: "partial", truncated: true,
      quality: {...app.history.quality, invalid_lines: 1, incomplete_tail: true}});
  let parsed;
  const response = snapshotResponse(terminal);
  response.json = () => new Promise(resolve => { parsed = resolve; });
  pending.shift()(response);
  await new Promise(resolve => setImmediate(resolve));
  nodes.get("live-toggle").events.click();
  parsed(terminal);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(actionHistoryView(nodes), frozen);
  assert.equal(vm.runInContext("actionHistory", context), app.history);
  assert.equal(vm.runInContext("actionHistoryBusy", context), false);
  assert.equal(nodes.get("connection").textContent, "Paused");
  assert.equal(timers.size, 0);
  const count = requests.length;
  vm.runInContext("refreshActionHistory();", context);
  assert.equal(requests.length, count); // Automatic history is also quiet after Pause.
  nodes.get("refresh-now").events.click();
  pending.shift()(snapshotResponse(recoveryFixture()));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.fetchCalls.at(-1).url, "/api/actions");
  assert.equal(pending.length, 1);
  const manual = {...terminal, detail: "Manual paused history"};
  pending.shift()(snapshotResponse(manual));
  await new Promise(resolve => setImmediate(resolve));
  assert.doesNotMatch(actionHistoryView(nodes).events[0], /unresolved/);
  assert.match(actionHistoryView(nodes).quality, /Manual paused history/);
  assert.equal(nodes.get("connection").textContent, "Paused");
  assert.equal(timers.size, 0);
  nodes.get("live-toggle").events.click();
  pending.shift()(snapshotResponse(recoveryFixture()));
  await new Promise(resolve => setImmediate(resolve));
  pending.shift()(snapshotResponse({...manual, detail: "Resumed history"}));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(actionHistoryView(nodes).quality, /Resumed history/);
  assert.equal(nodes.get("live-toggle").attributes["aria-pressed"], "false");
  automaticSnapshot(app);
  pending.shift()(snapshotResponse(recoveryFixture()));
  await new Promise(resolve => setImmediate(resolve));
  pending.shift()(snapshotResponse({...manual, detail: "Automatic history after resume"}));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(actionHistoryView(nodes).quality, /Automatic history after resume/);
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
});

for (const failure of ["HTTP", "JSON"]) {
  test(`DP2 history pause freezes delayed automatic ${failure} error quality; explicit manual errors stay visible`, async () => {
    // Catch/finally cannot silently replace frozen quality; authorized manual failures retain dated events and show the gap.
    const app = await historyApplication(), {nodes, context, pending, requests, timers} = app;
    const frozen = actionHistoryView(nodes);
    automaticSnapshot(app);
    pending.shift()(snapshotResponse(recoveryFixture()));
    await new Promise(resolve => setImmediate(resolve));
    nodes.get("live-toggle").events.click();
    const failed = () => failure === "HTTP" ? {ok: false, status: 503} :
      {ok: true, status: 200, json: async () => { throw new Error("Fixture JSON decode gap"); }};
    pending.shift()(failed());
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(actionHistoryView(nodes), frozen);
    assert.equal(vm.runInContext("actionHistoryError", context), "");
    assert.equal(vm.runInContext("actionHistoryBusy", context), false);
    vm.runInContext("refreshActionHistory(true);", context);
    assert.equal(pending.length, 1);
    pending.shift()(failed());
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(actionHistoryView(nodes).events, frozen.events);
    assert.match(actionHistoryView(nodes).quality, /Unavailable.*(HTTP 503|Fixture JSON decode gap).*no replay/);
    vm.runInContext("refreshActionHistory(true);", context);
    assert.equal(pending.length, 1);
    pending.shift()(snapshotResponse({...app.history, detail: "Explicit manual recovery"}));
    await new Promise(resolve => setImmediate(resolve));
    assert.match(actionHistoryView(nodes).quality, /Explicit manual recovery/);
    assert.equal(vm.runInContext("actionHistoryError", context), "");
    assert.equal(nodes.get("connection").textContent, "Paused");
    assert.equal(timers.size, 0);
    assert.equal(requests.filter(url => url === "/api/action").length, 0);
  });
}

test("DP2 history pause defers manual snapshot history behind discarded automatic success or error", async () => {
  // A pending automatic read cannot absorb manual provenance or permanently lose an explicit refresh request.
  for (const outcome of ["success", "error"]) {
    const app = await historyApplication(), {nodes, context, pending, requests, timers} = app;
    const frozen = actionHistoryView(nodes);
    automaticSnapshot(app);
    pending.shift()(snapshotResponse(recoveryFixture()));
    await new Promise(resolve => setImmediate(resolve));
    const automaticHistory = pending.shift();
    nodes.get("live-toggle").events.click();
    nodes.get("refresh-now").events.click();
    pending.shift()(snapshotResponse(recoveryFixture()));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.filter(url => url === "/api/actions").length, 2);
    const terminal = actionHistoryFixture([app.started, {...app.started, event: "succeeded"}]);
    automaticHistory(outcome === "success" ? snapshotResponse(terminal) : {ok: false, status: 503});
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(actionHistoryView(nodes), frozen);
    assert.equal(vm.runInContext("actionHistory", context), app.history);
    assert.equal(requests.filter(url => url === "/api/actions").length, 3);
    assert.equal(pending.length, 1); // A new manual GET, never a relabelled automatic result.
    pending.shift()(snapshotResponse({...terminal, detail: "Queued manual history"}));
    await new Promise(resolve => setImmediate(resolve));
    assert.match(actionHistoryView(nodes).quality, /Queued manual history/);
    assert.doesNotMatch(actionHistoryView(nodes).events[0], /unresolved/);
    assert.equal(vm.runInContext("actionHistoryBusy", context), false);
    assert.equal(nodes.get("connection").textContent, "Paused");
    assert.equal(timers.size, 0);
    assert.equal(requests.filter(url => url === "/api/action").length, 0);
  }
});

test("DP2 history pause preserves the explicit post-attempt read without replaying a confirmed action", async () => {
  // Post-attempt history is an authorized read even while paused and independently of an outstanding snapshot.
  const {nodes, context, pending, requests} = application();
  context.recovery = recoveryFixture();
  vm.runInContext("render(recovery, receiptFixture()); showDetails('Service',recovery.user_services.items[0]);", context);
  nodes.get("live-toggle").events.click();
  nodes.get("action-controls").children.find(button => button.textContent === "Stop service").events.click();
  nodes.get("action-run").events.click();
  pending[1](snapshotResponse({ok: true, action_id: "b".repeat(32), message: "User service stop completed.",
    observation: recoveryServiceState("inactive")}));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.filter(url => url === "/api/actions").length, 1);
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
  pending[2](snapshotResponse(actionHistoryFixture([], {detail: "Post-attempt paused history"})));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(actionHistoryView(nodes).quality, /Post-attempt paused history/);
  assert.equal(nodes.get("connection").textContent, "Paused");
  nodes.get("action-run").events.click();
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
});
