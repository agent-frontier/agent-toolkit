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
