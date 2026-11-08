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
  // Model connected identities, explicit focus calls and dialog close/cancel events.
  // This is not a layout engine, native focus trap or proof of browser touch geometry.
  function element(tag = "div") {
    let ownText = "";
    const node = {
      tagName: tag.toUpperCase(), value: "", children: [], events: {}, attributes: {},
      parentElement: null, open: false, disabled: false, className: "",
      get textContent() { return ownText + this.children.map(child => child.textContent).join(""); },
      set textContent(value) { this.replaceChildren(); ownText = String(value); },
      get isConnected() { return this === document.body || !!this.parentElement?.isConnected; },
      get hidden() { return this.getAttribute("hidden") !== null; },
      set hidden(value) { if (value) this.setAttribute("hidden", ""); else delete this.attributes.hidden; },
      append(...children) {
        children.forEach(child => { child.parentElement = this; this.children.push(child); });
      },
      replaceChildren(...children) {
        this.children.forEach(child => { child.parentElement = null; });
        this.children = []; ownText = "";
        if (document.activeElement && !document.activeElement.isConnected) document.activeElement = document.body;
        this.append(...children);
      },
      contains(other) { return this === other || this.children.some(child => child.contains(other)); },
      matches(selector) {
        if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
        const match = /^([a-z][\w-]*)?(?:\[([\w-]+)\])?$/i.exec(selector);
        return !!match && (!match[1] || this.tagName === match[1].toUpperCase()) &&
          (!match[2] || this.getAttribute(match[2]) !== null);
      },
      closest(selector) {
        for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
        return null;
      },
      addEventListener(name, callback) { this.events[name] = callback; },
      getAttribute(name) { return this.attributes[name] ?? null; },
      setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "id") nodes.set(String(value), this);
        if (name === "class") this.className = String(value);
      },
      focus() { if (this.isConnected && !this.disabled && !this.closest("[hidden]")) document.activeElement = this; },
      scrollIntoView() { this.scrolled = true; },
      showModal() { this.open = true; },
      close() { if (this.open) { this.open = false; this.events.close?.({target: this, currentTarget: this}); } },
    };
    return node;
  }
  const document = {
    activeElement: null,
    getElementById(id) {
      if (!nodes.has(id)) { const node = element(); node.setAttribute("id", id); this.body.append(node); }
      return nodes.get(id);
    },
    createElement: element,
    createElementNS(namespace, tag) { return element(tag); },
    querySelectorAll(selector) {
      const parts = selector.split(/\s+/), result = [];
      const visit = node => {
        if (node.matches(parts.at(-1))) {
          let ancestor = node.parentElement, index = parts.length - 2;
          while (ancestor && index >= 0) {
            if (ancestor.matches(parts[index])) index--;
            ancestor = ancestor.parentElement;
          }
          if (index < 0) result.push(node);
        }
        node.children.forEach(visit);
      };
      this.body.children.forEach(visit);
      return result;
    },
  };
  document.body = element("body"); document.activeElement = document.body;
  // Load attributes/ancestry from the real HTML instead of inventing named dialogs or navigation.
  const html = fs.readFileSync(path.join(__dirname, "../dashboard/index.html"), "utf8");
  const stack = [document.body], voidTags = new Set(["input", "br", "hr", "img"]);
  let inBody = false, previous = 0;
  for (const match of html.matchAll(/<(\/?)([a-z][\w-]*)\b([^>]*)>/gi)) {
    const [, closing, tag, attributes] = match;
    if (tag === "body") { if (closing) break; inBody = true; previous = match.index + match[0].length; continue; }
    if (!inBody) continue;
    const content = html.slice(previous, match.index).trim();
    if (content) { const node = element("text"); node.textContent = content; stack.at(-1).append(node); }
    previous = match.index + match[0].length;
    if (closing) { stack.pop(); continue; }
    const node = element(tag);
    for (const attr of attributes.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) node.setAttribute(attr[1], attr[2] ?? "");
    stack.at(-1).append(node);
    if (!voidTags.has(tag)) stack.push(node);
  }
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
  const escapeDialog = () => {
    const dialog = nodes.get("detail-dialog");
    const event = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
    dialog.events.cancel?.(event);
    if (!event.defaultPrevented) dialog.close(); // Native cancel default; close then emits its own event.
    return event;
  };
  return { nodes, context, document, escapeDialog, requests, pending, timers, fetchCalls, clock };
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

function identityFixture() {
  const value = recoveryFixture();
  value.system_services = {items: [{...value.user_services.items[0], scope: "system"}]};
  value.docker = {status: "available", items: [{...value.podman.items[0], engine: "docker"}]};
  value.processes = {items: [{name: "fixture-worker", pid: 22, start_time_ticks: 123,
    cpu_percent: 5, memory_bytes: 100, controllable: true}], total_observed: 1, unreadable: 0};
  return value;
}

const identityCases = [
  {key: "service:user:fixture-worker.service", heading: "services-heading",
    remove(value) { value.user_services.items = []; }},
  {key: `container:podman:${"a".repeat(64)}`, heading: "containers-heading",
    remove(value) { value.podman.items = []; }},
  {key: "process:22:123", heading: "processes-heading",
    remove(value) { value.processes.items[0].start_time_ticks = 124; }},
];

function inspectButton(app, key) {
  const button = app.document.querySelectorAll("button[data-target-key]")
    .find(button => button.getAttribute("data-target-key") === key);
  assert.ok(button, `Missing exact inspect identity: ${key}`);
  return button;
}

function inspectClick(button) {
  button.focus();
  button.events.click({currentTarget: button, target: button});
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

test("DP3 real HTML section links focus their destinations without replacing legacy controls", () => {
  // Execute the app's jump listeners against its actual HTML; this proves focus wiring, not viewport geometry.
  const app = application(), {nodes, context, document, requests} = app;
  context.value = maintenanceFixture();
  delete context.value.actions.capabilities;
  delete context.value.actions.cleanup;
  vm.runInContext("render(value, receiptFixture()); refreshActionHistory();", context);
  const links = document.querySelectorAll("nav a");
  assert.deepEqual(links.slice(0, 3).map(link => link.getAttribute("href")),
    ["#maintenance-heading", "#containers-heading", "#services-heading"]);
  for (const link of links) {
    let prevented = false;
    link.events.click({preventDefault() { prevented = true; }});
    const id = link.getAttribute("href").slice(1);
    assert.equal(document.activeElement.getAttribute("id"), id);
    assert.equal(nodes.get(id).getAttribute("tabindex"), "-1");
    assert.equal(nodes.get(id).scrolled, true);
    assert.equal(prevented, true);
  }
  for (const id of ["service-search", "service-state", "service-sort", "history-window", "refresh-now"]) {
    assert.equal(nodes.get(id).isConnected, true);
  }
  assert.match(nodes.get("controls-compat").textContent, /Legacy.*fixed controls/);
  assert.equal(requests.filter(url => url === "/api/actions" || url === "/api/action").length, 0);
});

test("DP3 row focus follows engine/full-ID, scope/unit and PID/start-ticks across real redraws", () => {
  // Same names, cross-engine IDs, cross-scope units and reused PIDs must never steal an old target's focus.
  for (const entry of identityCases) {
    const app = application(), {nodes, context, document, requests} = app;
    context.value = identityFixture();
    vm.runInContext("render(value, receiptFixture());", context);
    const original = inspectButton(app, entry.key);
    original.focus();
    vm.runInContext("render(value, receiptFixture());", context);
    assert.equal(original.isConnected, false);
    assert.equal(document.activeElement.getAttribute("data-target-key"), entry.key);
    assert.equal(document.activeElement.isConnected, true);
    entry.remove(context.value);
    vm.runInContext("render(value, receiptFixture());", context);
    assert.equal(document.activeElement.getAttribute("id"), entry.heading);
    assert.equal(nodes.get(entry.heading).getAttribute("tabindex"), "-1");
    assert.equal(requests.filter(url => url === "/api/action").length, 0);
  }
});

test("DP3 close/Escape restore the actual matching opener or its section after identity removal", () => {
  // A retained stale DOM node is not an opener; close must find the rebuilt exact identity, never a namesake.
  for (const entry of identityCases) {
    for (const close of ["button", "escape"]) {
      const app = application(), {context, document, nodes, requests} = app;
      context.value = identityFixture();
      vm.runInContext("render(value, receiptFixture());", context);
      inspectClick(inspectButton(app, entry.key));
      assert.equal(document.activeElement.getAttribute("id"), "detail-close");
      vm.runInContext("render(value, receiptFixture());", context);
      assert.equal(document.activeElement.getAttribute("id"), "detail-close");
      if (close === "button") nodes.get("detail-close").events.click();
      else app.escapeDialog();
      assert.equal(document.activeElement.getAttribute("data-target-key"), entry.key);
      inspectClick(inspectButton(app, entry.key));
      entry.remove(context.value);
      vm.runInContext("render(value, receiptFixture());", context);
      app.escapeDialog();
      assert.equal(document.activeElement.getAttribute("id"), entry.heading);
      vm.runInContext("runAction();", context);
      assert.equal(requests.filter(url => url === "/api/action").length, 0);
    }
  }
  for (const id of ["cleanup-details", "cleanup-run"]) {
    const app = application();
    app.context.value = maintenanceFixture();
    vm.runInContext("render(value, receiptFixture());", app.context);
    inspectClick(app.nodes.get(id));
    app.escapeDialog();
    assert.equal(app.document.activeElement.getAttribute("id"), id);
    assert.equal(app.requests.filter(url => url === "/api/action").length, 0);
  }
});

test("DP3 named dialog focuses Close then Cancel; Escape cancels only before dispatch", async () => {
  // Drive actual listeners and POST guards: safe focus, Cancel/Escape zero, double Confirm one, no post-dispatch undo.
  const app = await historyApplication(), {nodes, context, document, requests, pending} = app;
  const opener = nodes.get("services").children[0].children[0].children[0];
  inspectClick(opener);
  assert.equal(nodes.get("detail-dialog").getAttribute("aria-labelledby"), "detail-title");
  assert.equal(document.activeElement.getAttribute("id"), "detail-close");
  let stop = nodes.get("action-controls").children.find(button => button.textContent === "Stop service");
  stop.events.click({currentTarget: stop});
  assert.equal(document.activeElement.getAttribute("id"), "action-cancel");
  assert.match(nodes.get("action-question").textContent, /fixture-worker\.service \(user\)/);
  nodes.get("action-cancel").events.click();
  assert.equal(document.activeElement.getAttribute("data-action"), "stop");
  stop.events.click({currentTarget: stop});
  app.escapeDialog();
  assert.equal(document.activeElement.getAttribute("data-target-key"), "service:user:fixture-worker.service");
  vm.runInContext("runAction();", context);
  assert.equal(requests.filter(url => url === "/api/action").length, 0);
  inspectClick(inspectButton(app, "service:user:fixture-worker.service"));
  stop = nodes.get("action-controls").children.find(button => button.textContent === "Stop service");
  stop.events.click({currentTarget: stop});
  nodes.get("action-run").focus();
  nodes.get("action-run").events.click();
  nodes.get("action-run").events.click();
  assert.equal(app.escapeDialog().defaultPrevented, true);
  assert.equal(nodes.get("detail-dialog").open, true);
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
  pending.shift()(snapshotResponse({ok: true, action_id: "c".repeat(32), message: "Fixture stop completed.",
    observation: recoveryServiceState("inactive")}));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(document.activeElement.getAttribute("id"), "detail-close");
  assert.equal(nodes.get("action-controls").children.some(button => button.textContent === "Start service"), true);
  const start = nodes.get("action-controls").children.find(button => button.textContent === "Start service");
  start.events.click({currentTarget: start});
  vm.runInContext("delete snapshot.actions.service_actions; render(snapshot, receiptFixture());", context);
  nodes.get("action-run").focus();
  nodes.get("action-run").events.click();
  assert.equal(document.activeElement.getAttribute("id"), "detail-close");
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
});

for (const failure of ["HTTP", "JSON"]) {
  test(`DP3 failed paused manual ${failure} read retains dated values and requires Refresh/Resume`, async () => {
    // Paused errors must not advertise a nonexistent retry or renew old server-relative receipt evidence.
    const app = application(), {nodes, context, pending, timers} = app;
    const value = maintenanceFixture();
    value.filesystems = [{mount: "/", available_bytes: 1000, total_bytes: 2000, used_percent: 50}];
    pending.shift()(snapshotResponse(value));
    await new Promise(resolve => setImmediate(resolve));
    const receipt = vm.runInContext("snapshotReceipt", context);
    nodes.get("live-toggle").events.click();
    nodes.get("refresh-now").events.click();
    pending.shift()(failure === "HTTP" ? {ok: false, status: 503} :
      {ok: true, status: 200, json: async () => { throw new Error("Fixture snapshot decode gap"); }});
    await new Promise(resolve => setImmediate(resolve));
    assert.match(nodes.get("notice").textContent, /Retaining.*dated.*Paused.*Refresh now.*Resume updates/);
    assert.doesNotMatch(nodes.get("notice").textContent, /retrying automatically/i);
    assert.equal(nodes.get("disk-free").textContent, "1000 B");
    assert.equal(vm.runInContext("snapshotReceipt", context), receipt);
    assert.equal(vm.runInContext("snapshot", context), value);
    assert.equal(vm.runInContext("paused", context), true);
    assert.equal(nodes.get("live-toggle").getAttribute("aria-pressed"), "true");
    assert.equal(timers.size, 0);
    nodes.get("refresh-now").events.click();
    pending.shift()(snapshotResponse({...value, host: "manual recovery"}));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(vm.runInContext("paused", context), true);
    assert.equal(timers.size, 0);
  });
}

test("DP3 discarded automatic snapshot failure cannot publish over a frozen paused view", async () => {
  // An automatic response finishing after Pause is not a manual error and cannot replace frozen status or evidence.
  const app = application(), {nodes, context, pending} = app;
  pending.shift()(snapshotResponse(maintenanceFixture()));
  await new Promise(resolve => setImmediate(resolve));
  automaticSnapshot(app);
  nodes.get("live-toggle").events.click();
  const notice = nodes.get("notice").textContent, receipt = vm.runInContext("snapshotReceipt", context);
  pending.shift()({ok: false, status: 503});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes.get("notice").textContent, notice);
  assert.equal(nodes.get("connection").textContent, "Paused");
  assert.equal(vm.runInContext("snapshotReceipt", context), receipt);
});

test("DP3 historical detail fields remain separate from receipts and exact observations while paused", async () => {
  // Fresh post-state can authorize retained Start without relabelling old snapshot fields as current or resuming polling.
  const app = await historyApplication(), {nodes, context, pending, requests, timers} = app;
  inspectClick(nodes.get("services").children[0].children[0].children[0]);
  const historical = nodes.get("detail-observed").textContent;
  assert.match(historical, /Historical snapshot details.*fixed until reopened/);
  assert.equal(nodes.get("detail-current").hidden, true);
  nodes.get("live-toggle").events.click();
  const stop = nodes.get("action-controls").children.find(button => button.textContent === "Stop service");
  stop.events.click({currentTarget: stop});
  nodes.get("action-run").events.click();
  pending.shift()(snapshotResponse({ok: true, action_id: "d".repeat(32), message: "Fixture stop completed.",
    observation: recoveryServiceState("inactive")}));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get("action-result").textContent, /Request receipt.*Fixture stop completed/);
  assert.equal(nodes.get("detail-current").hidden, false);
  assert.match(nodes.get("detail-current").textContent, /Exact post-attempt state: inactive; server read.*not application health/i);
  assert.equal(nodes.get("detail-observed").textContent, historical);
  assert.match(nodes.get("detail-fields").textContent, /Stateactive/);
  pending.shift()(snapshotResponse(actionHistoryFixture([])));
  const updated = recoveryFixture(); updated.user_services.items = [];
  pending.shift()(snapshotResponse(updated));
  await new Promise(resolve => setImmediate(resolve));
  pending.shift()(snapshotResponse(actionHistoryFixture([])));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes.get("detail-observed").textContent, historical);
  assert.equal(nodes.get("action-controls").children.some(button => button.textContent === "Start service"), true);
  assert.equal(vm.runInContext("paused", context), true);
  assert.equal(timers.size, 0);
  assert.equal(requests.filter(url => url === "/api/action").length, 1);
});

test("DP3 storage explains only cached coverage, budgets and protection; missing remains unknown", () => {
  // Cached categories are not an attribution/reclaim calculation; missing policy lists cannot imply safety or zero.
  const {nodes, context, requests} = application();
  context.value = maintenanceFixture();
  context.value.filesystems = [{mount: "/", available_bytes: 1000, total_bytes: 2000, used_percent: 50}];
  Object.assign(context.value.janitor.maintenance, {mode: "run", caches: [{
    path: "/fictional/cache", kind: "shared", bytes: 128, budget_bytes: 256,
    active: true, eligible: false, reason: "active_build",
  }], workspaces: [{path: "/fictional/work", status: "protected_dirty"}]});
  vm.runInContext("render(value, receiptFixture());", context);
  let fields = nodes.get("storage-summary").textContent;
  assert.match(fields, /maintenance-last\.json.*server-relative/);
  assert.match(fields, /1 cache record.*1 workspace record/);
  assert.match(nodes.get("storage-caches").textContent, /128 B.*256 B.*active true.*eligible false.*active_build/);
  assert.match(nodes.get("protected").textContent, /protected_dirty/);
  assert.match(nodes.get("storage-explanation").textContent, /budget is not.*permission/i);
  assert.match(nodes.get("storage-explanation").textContent, /previously reclaimed.*not.*reclaim.*again/i);
  assert.match(nodes.get("storage-explanation").textContent, /root.*separate.*never add or subtract/i);
  assert.equal(nodes.get("disk-free").textContent, "1000 B");
  const saved = context.value.janitor.maintenance;
  for (const key of ["generated_at", "caches", "workspaces", "cache_budget_bytes", "cache_bytes"]) delete saved[key];
  vm.runInContext("render(value, receiptFixture());", context);
  fields = nodes.get("storage-summary").textContent;
  assert.match(fields, /Cache observation ageUnknown.*Saved coverageUnknown.*Soft cache budgetUnknown/);
  assert.match(nodes.get("storage-caches").textContent, /unknown.*no saved list/i);
  assert.match(nodes.get("protected").textContent, /unknown.*no saved list/i);
  assert.equal(nodes.get("cache").textContent, "Unknown");
  assert.equal(requests.filter(url => url !== "/api/snapshot").length, 0);
});

function storageMetadata(enabled = true) {
  return {supported: true, api_version: 1, enabled, available: true, capabilities: ["storage-scan"],
    roots: [{id: "home", label: "Home"}, {id: "root", label: "Root filesystem"}, {id: "tmp", label: "/tmp"}],
    limits: {wall_seconds: 120, max_directories: 128}};
}

function storageFixture(changes = {}) {
  return {...storageMetadata(), read_at: new Date().toISOString(), job: {
    id: "a".repeat(32), root: "home", directory_id: "b".repeat(32), state: "complete",
    breadcrumbs: [{id: "b".repeat(32), name: "Home"}], elapsed_seconds: 1.5,
    started_at: "2026-11-08T00:00:00Z", finished_at: "2026-11-08T00:00:02Z",
    root_bytes: 100, direct_bytes: 5, coverage: "complete",
    directories: [
      {id: "c".repeat(32), name: "large <img onerror=bad>", bytes: 75, navigable: true, detail: null},
      {id: "d".repeat(32), name: "small\\nfolder", bytes: 15, navigable: true, detail: null},
    ],
    omitted_directories: {count: 2, bytes: 5}, warning_count: 0, warning_sample: "",
    permission_denied: false, stdout_bytes: 100, stderr_bytes: 0, error: null, error_code: null,
    ...changes,
  }};
}

async function storageApplication(view = storageFixture()) {
  const app = application(), value = maintenanceFixture();
  value.storage_scan = storageMetadata(view.enabled);
  app.pending.shift()(snapshotResponse(value));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.fetchCalls.at(-1).url, "/api/storage-scan");
  app.pending.shift()(snapshotResponse(view));
  await new Promise(resolve => setImmediate(resolve));
  return {...app, value};
}

function storageViewText(app) {
  return ["storage-state", "storage-total", "storage-date", "storage-coverage", "storage-error", "storage-directories"]
    .map(id => app.nodes.get(id).textContent);
}

test("DS1 real Storage DOM has fixed presets, explicit controls and a proportional decorative map plus ranked routes", async () => {
  // Execute actual app/HTML and numerical SVG areas; native 390px touch geometry remains a parent field gate.
  const app = await storageApplication(), {nodes, document, fetchCalls} = app;
  assert.equal(nodes.get("storage-root").tagName, "SELECT");
  assert.deepEqual(nodes.get("storage-root").children.map(node => node.getAttribute("value")), ["home", "root", "tmp"]);
  assert.equal(nodes.get("storage-start").tagName, "BUTTON");
  assert.equal(nodes.get("storage-cancel").tagName, "BUTTON");
  assert.equal(nodes.get("storage-map").getAttribute("role"), "img");
  assert.equal(nodes.get("storage-map").getAttribute("viewBox"), "0 0 1000 500");
  assert.match(nodes.get("storage-state").textContent, /complete/i);
  assert.match(nodes.get("storage-coverage").textContent, /allocated.*not reclaimable/i);
  assert.match(nodes.get("storage-date").textContent, /2026/);
  const buttons = document.querySelectorAll("button[data-directory-id]")
    .filter(node => nodes.get("storage-directories").contains(node));
  assert.deepEqual(buttons.map(node => node.getAttribute("data-directory-id")), ["c".repeat(32), "d".repeat(32)]);
  assert.match(buttons[0].textContent, /large <img onerror=bad>/);
  assert.equal(document.querySelectorAll("img").length, 0);
  const tiles = document.querySelectorAll("rect[data-bytes]");
  assert.equal(tiles.length, 4);
  for (const tile of tiles) {
    const area = Number(tile.getAttribute("width")) * Number(tile.getAttribute("height"));
    assert.ok(Math.abs(area / 500000 - Number(tile.getAttribute("data-bytes")) / 100) < 1e-9);
    assert.equal(tile.events.click, undefined); // Tiny tiles are never the only navigation control.
  }
  const css = fs.readFileSync(path.join(__dirname, "../dashboard/style.css"), "utf8");
  assert.match(css, /\.storage-directory[^}]*min-height:\s*44px/);
  assert.match(css, /#storage-map[^}]*width:\s*100%/);
  assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 0);
});

test("DS1 legacy, malformed metadata and observer modes never make unsupported or automatic scan requests", () => {
  // Feature detection protects old backends while preserving their original workload choices and Storage jump.
  for (const metadata of [undefined, {...storageMetadata(), api_version: 2}, {...storageMetadata(), capabilities: []}]) {
    const app = application();
    app.context.value = maintenanceFixture(); app.context.value.storage_scan = metadata;
    vm.runInContext("render(value, receiptFixture()); refreshStorage();", app.context);
    assert.equal(app.fetchCalls.filter(call => call.url.startsWith("/api/storage-scan")).length, 0);
    assert.equal(app.nodes.get("storage-start").disabled, true);
    assert.match(app.nodes.get("storage-support").textContent, /not advertised|unsupported/i);
    assert.equal(app.nodes.get("storage-heading").getAttribute("tabindex"), "-1");
  }
  const app = application();
  app.context.value = maintenanceFixture(); app.context.value.actions.enabled = false;
  app.context.value.storage_scan = storageMetadata(false);
  vm.runInContext("render(value, receiptFixture());", app.context);
  app.nodes.get("storage-start").events.click();
  assert.equal(app.fetchCalls.filter(call => call.options?.method === "POST").length, 0);
  assert.equal(app.nodes.get("storage-start").disabled, true);
  assert.match(app.nodes.get("storage-support").textContent, /observer|disabled/i);
});

test("DS1 explicit preset, drill and Back post exactly one current-job ID and never paths", async () => {
  // Directory labels are untrusted display text; only the cached opaque IDs authorize a new read-only scan.
  const app = await storageApplication(), {nodes, fetchCalls, pending} = app;
  nodes.get("storage-root").value = "tmp";
  nodes.get("storage-root").events.change();
  assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 0);
  nodes.get("storage-start").events.click(); nodes.get("storage-start").events.click();
  assert.equal(fetchCalls.at(-1).url, "/api/storage-scan/start");
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body), {root: "tmp"});
  pending.shift()(snapshotResponse(storageFixture({root: "tmp", state: "starting"})));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetchCalls.at(-1).url, "/api/storage-scan");
  pending.shift()(snapshotResponse(storageFixture({root: "tmp"})));
  await new Promise(resolve => setImmediate(resolve));
  const oldButton = app.document.querySelectorAll("button[data-directory-id]")
    .find(node => node.getAttribute("data-directory-id") === "c".repeat(32));
  oldButton.events.click();
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body),
    {job_id: "a".repeat(32), directory_id: "c".repeat(32)});
  pending.shift()(snapshotResponse(storageFixture({state: "starting"})));
  await new Promise(resolve => setImmediate(resolve));
  const drilled = storageFixture({id: "e".repeat(32), directory_id: "f".repeat(32),
    breadcrumbs: [{id: "1".repeat(32), name: "/tmp"}, {id: "f".repeat(32), name: "large <img onerror=bad>"}]});
  pending.shift()(snapshotResponse(drilled));
  await new Promise(resolve => setImmediate(resolve));
  const posts = fetchCalls.filter(call => call.options?.method === "POST").length;
  oldButton.events.click(); // A retained stale DOM event cannot smuggle a previous result ID.
  assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, posts);
  assert.equal(nodes.get("storage-back").disabled, false);
  nodes.get("storage-back").events.click();
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body),
    {job_id: "e".repeat(32), directory_id: "1".repeat(32)});
  assert.equal(fetchCalls.filter(call => call.url === "/api/action").length, 0);
  assert.ok(fetchCalls.filter(call => call.options?.method === "POST")
    .every(call => call.options.headers["X-Action-Token"] === app.value.actions.nonce));
});

test("DS1 Cancel shows cancelling until a fresh terminal read and cannot free or replay the scan slot", async () => {
  // A receipt is not finalization; only the cached manager state can make another Scan available.
  const app = await storageApplication(storageFixture({state: "running", finished_at: null})), {nodes, fetchCalls, pending} = app;
  assert.equal(nodes.get("storage-start").disabled, true);
  nodes.get("storage-cancel").events.click(); nodes.get("storage-cancel").events.click();
  assert.equal(fetchCalls.at(-1).url, "/api/storage-scan/cancel");
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body), {job_id: "a".repeat(32)});
  pending.shift()(snapshotResponse(storageFixture({state: "cancelling", finished_at: null})));
  await new Promise(resolve => setImmediate(resolve));
  pending.shift()(snapshotResponse(storageFixture({state: "cancelling", finished_at: null})));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get("storage-state").textContent, /cancelling/i);
  assert.equal(nodes.get("storage-start").disabled, true);
  assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 1);
  vm.runInContext("refreshStorage(true);", app.context);
  pending.shift()(snapshotResponse(storageFixture({state: "cancelled", root_bytes: null, direct_bytes: null, coverage: "lower_bound"})));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get("storage-state").textContent, /cancelled/i);
  assert.equal(nodes.get("storage-start").disabled, false);
  assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 1);
});

for (const failure of ["success", "HTTP", "JSON"]) {
  test(`DS1 Pause discards delayed automatic storage ${failure} publication after await`, async () => {
    // Pause must freeze the actual separate storage read, including error/quality text, not merely snapshot polling.
    const app = await storageApplication(), before = storageViewText(app);
    automaticSnapshot(app);
    app.pending.shift()(snapshotResponse(app.value));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(app.fetchCalls.at(-1).url, "/api/storage-scan");
    app.nodes.get("live-toggle").events.click();
    const response = failure === "HTTP" ? {ok: false, status: 503} :
      failure === "JSON" ? {ok: true, status: 200, json: async () => { throw Error("fixture torn JSON"); }} :
        snapshotResponse(storageFixture({root_bytes: 999, state: "partial", coverage: "lower_bound"}));
    app.pending.shift()(response);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(storageViewText(app), before);
    assert.equal(app.nodes.get("live-toggle").textContent, "Resume updates");
    assert.equal(app.fetchCalls.filter(call => call.options?.method === "POST").length, 0);
  });
}

test("DS1 manual paused refresh queues its own fresh storage GET behind a discarded automatic read", async () => {
  // Do not relabel a delayed automatic response as manual or leave the independent read slot occupied.
  const app = await storageApplication();
  automaticSnapshot(app);
  app.pending.shift()(snapshotResponse(app.value));
  await new Promise(resolve => setImmediate(resolve));
  const automatic = app.pending.shift();
  app.nodes.get("live-toggle").events.click();
  app.nodes.get("refresh-now").events.click();
  app.pending.shift()(snapshotResponse(app.value));
  await new Promise(resolve => setImmediate(resolve));
  const count = app.fetchCalls.filter(call => call.url === "/api/storage-scan").length;
  automatic({ok: false, status: 503});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.fetchCalls.filter(call => call.url === "/api/storage-scan").length, count + 1);
  app.pending.shift()(snapshotResponse(storageFixture({state: "partial", coverage: "lower_bound", permission_denied: true,
    warning_count: 51, warning_sample: "Permission denied; vanished during scan"})));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(app.nodes.get("storage-state").textContent, /partial/i);
  assert.match(app.nodes.get("storage-coverage").textContent, /51|Permission/);
  assert.equal(app.nodes.get("live-toggle").textContent, "Resume updates");
});

test("DS1 explicit paused Scan with a lost reply reads once, retains Pause and never automatically replays POST", async () => {
  // Transport uncertainty is not rejection or permission to retry an expensive operation.
  const app = await storageApplication(), {nodes, pending, fetchCalls} = app;
  nodes.get("live-toggle").events.click();
  nodes.get("storage-start").events.click();
  pending.shift()({ok: true, status: 202, json: async () => { throw Error("fixture reply lost"); }});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetchCalls.at(-1).url, "/api/storage-scan");
  pending.shift()(snapshotResponse(storageFixture({state: "partial", coverage: "lower_bound"})));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes.get("live-toggle").textContent, "Resume updates");
  assert.match(nodes.get("storage-error").textContent, /no automatic replay/i);
  assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 1);
  assert.equal(app.timers.size, 0);
});

test("DS1 failed traversal root zero stays unknown; omitted directories and direct bytes stay distinct", async () => {
  // A failed root traversal must not render 0 B/full-disk success or a reclaimable-space promise.
  const app = await storageApplication(storageFixture({state: "partial", coverage: "lower_bound",
    root_bytes: null, direct_bytes: null, directories: [], omitted_directories: {count: 0, bytes: 0},
    warning_count: 1, warning_sample: "Permission denied", permission_denied: true}));
  assert.match(app.nodes.get("storage-total").textContent, /Unknown/i);
  assert.doesNotMatch(app.nodes.get("storage-total").textContent, /\b0 B\b/);
  assert.match(app.nodes.get("storage-coverage").textContent, /lower.bound|permission/i);
  assert.match(app.nodes.get("storage-map-note").textContent, /unknown|no measured/i);
  assert.equal(app.document.querySelectorAll("rect[data-bytes]").length, 0);
});

test("DS1 ranked directory focus survives cached redraw and falls back to Storage after current-job identity changes", async () => {
  // Keyboard users must not lose their route when automatic publication rebuilds the ranked list.
  const app = await storageApplication();
  const button = app.document.querySelectorAll("button[data-directory-id]")
    .find(node => node.getAttribute("data-directory-id") === "c".repeat(32));
  button.focus();
  vm.runInContext("refreshStorage(true);", app.context);
  app.pending.shift()(snapshotResponse(storageFixture()));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.document.activeElement.getAttribute("data-directory-id"), "c".repeat(32));
  vm.runInContext("refreshStorage(true);", app.context);
  // Keep the new empty result's accounting valid so this exercises focus, not the malformed-view guard.
  app.pending.shift()(snapshotResponse(storageFixture({id: "e".repeat(32), directory_id: "f".repeat(32),
    breadcrumbs: [{id: "f".repeat(32), name: "Home"}], directories: [],
    root_bytes: 4096, direct_bytes: 4096, omitted_directories: {count: 0, bytes: 0}})));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.document.activeElement.getAttribute("id"), "storage-heading");
});

test("DS1 Storage puts visual results before collapsed dates, method and diagnostics", async () => {
  // Native 390px evidence found prose burying the map. Prove actual-app DOM order here;
  // the parent separately proves viewport geometry, not this lightweight DOM fixture.
  const app = await storageApplication(storageFixture({state: "partial", coverage: "lower_bound",
    elapsed_seconds: 115.6, warning_count: 8, permission_denied: true, warning_sample: "Permission denied"}));
  const order = [];
  const visit = node => { order.push(node); node.children.forEach(visit); };
  visit(app.document.body);
  const position = id => order.indexOf(app.nodes.get(id));
  const verbose = ["storage-support", "storage-date", "storage-coverage", "storage-map-note", "storage-buckets", "storage-warnings"];
  for (const id of verbose) {
    assert.ok(position("storage-map") < position(id) && position("storage-directories") < position(id),
      `${id} must follow the map and ranked directories, not bury visual results`);
  }
  const details = app.nodes.get("storage-details");
  assert.equal(details.tagName, "DETAILS");
  assert.equal(details.getAttribute("open"), null);
  assert.equal(details.children[0].tagName, "SUMMARY");
  for (const id of verbose) assert.equal(app.nodes.get(id).closest("details"), details);
  for (const id of ["storage-root", "storage-start", "storage-cancel", "storage-back",
    "storage-state", "storage-total", "storage-visual-warning", "storage-error"]) {
    assert.ok(position(id) < position("storage-map"), `${id} must stay ahead of the visual result`);
    assert.equal(app.nodes.get(id).closest("details"), null);
  }
  const warning = app.nodes.get("storage-visual-warning");
  assert.equal(warning.closest("[hidden]"), null);
  assert.match(warning.textContent, /lower.bound/i);
  assert.match(warning.textContent, /permission|unreadable/i);
  assert.match(app.nodes.get("storage-state").textContent, /PARTIAL/);
  assert.equal(app.nodes.get("storage-heading").textContent, "Storage usage");
  assert.equal(app.document.querySelectorAll("button[data-directory-id]")
    .filter(button => app.nodes.get("storage-directories").contains(button)).length, 2);
});

function storageAcceptedB(state = "starting") {
  const value = storageFixture({id: "e".repeat(32), directory_id: "f".repeat(32),
    breadcrumbs: [{id: "f".repeat(32), name: "Home"}], state});
  if (state !== "complete") Object.assign(value.job, {finished_at: null, root_bytes: null,
    direct_bytes: null, directories: [], omitted_directories: {count: 0, bytes: 0},
    coverage: "unknown", elapsed_seconds: 0});
  return value;
}

function storageAcceptedResponse(value) {
  return {...snapshotResponse(value), status: 202};
}

test("DS1 known accepted B stays cancellable while paused cached reads fail, without receipt completion or stale A", async () => {
  // Unlike a lost reply, a validated accepted ID is known. A failed/new or delayed/old GET must not erase it.
  for (const reportedState of ["starting", "complete"]) {
    for (const earlierRead of [false, true]) {
      const app = await storageApplication(), {nodes, pending, fetchCalls} = app;
      nodes.get("live-toggle").events.click();
      let stale = null;
      if (earlierRead) {
        vm.runInContext("refreshStorage(true);", app.context);
        stale = pending.shift(); // This manual read began before B was accepted, even though Pause is on.
      }
      nodes.get("storage-start").events.click();
      pending.shift()(storageAcceptedResponse(storageAcceptedB(reportedState)));
      await new Promise(resolve => setImmediate(resolve));
      if (stale) {
        stale(snapshotResponse(storageFixture())); // Old completed A is not a fresh observation of accepted B.
        await new Promise(resolve => setImmediate(resolve));
      }
      assert.equal(fetchCalls.at(-1).url, "/api/storage-scan");
      pending.shift()({ok: false, status: 503});
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(nodes.get("storage-scan").getAttribute("data-job-id"), "e".repeat(32));
      assert.equal(nodes.get("storage-scan").getAttribute("data-state"), "starting");
      assert.equal(nodes.get("storage-scan").getAttribute("data-state-source"), "accepted_receipt");
      assert.equal(nodes.get("storage-start").disabled, true);
      assert.equal(nodes.get("storage-cancel").disabled, false);
      assert.match(nodes.get("storage-state").textContent, /accepted.*unverified/i);
      assert.match(nodes.get("storage-date").textContent, /accepted/i);
      assert.match(nodes.get("storage-total").textContent, /Unknown/);
      assert.equal(nodes.get("storage-directories").children.length, 0);
      assert.equal(nodes.get("live-toggle").textContent, "Resume updates");
      assert.equal(app.timers.size, 0);
      assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 1);
      nodes.get("storage-cancel").events.click();
      assert.equal(fetchCalls.at(-1).url, "/api/storage-scan/cancel");
      assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body), {job_id: "e".repeat(32)});
      pending.shift()(storageAcceptedResponse(storageAcceptedB("cancelling")));
      await new Promise(resolve => setImmediate(resolve));
      pending.shift()({ok: false, status: 503});
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(nodes.get("storage-scan").getAttribute("data-job-id"), "e".repeat(32));
      assert.equal(nodes.get("storage-scan").getAttribute("data-state"), "cancelling");
      assert.equal(nodes.get("storage-start").disabled, true);
      assert.equal(nodes.get("live-toggle").textContent, "Resume updates");
      assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 2); // Both explicit user intents.
    }
  }
});

test("DS1 a newer root edit during delayed Home POST survives its receipt and makes the next Scan tmp", async () => {
  // An accepted earlier request describes its own target; it cannot consume a later operator selection.
  const app = await storageApplication(), {nodes, pending, fetchCalls} = app;
  nodes.get("storage-root").value = "home";
  nodes.get("storage-root").events.change();
  nodes.get("storage-start").events.click();
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body), {root: "home"});
  assert.equal(nodes.get("storage-root").disabled, false); // Match the actual editable dropdown UX.
  nodes.get("storage-root").value = "tmp";
  nodes.get("storage-root").events.change();
  assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 1);
  pending.shift()(storageAcceptedResponse(storageAcceptedB()));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes.get("storage-root").value, "tmp");
  pending.shift()(snapshotResponse(storageAcceptedB("complete")));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes.get("storage-scan").getAttribute("data-state-source"), "cached_read");
  assert.equal(nodes.get("storage-root").value, "tmp");
  assert.equal(nodes.get("storage-start").disabled, false);
  nodes.get("storage-start").events.click();
  assert.equal(fetchCalls.at(-1).url, "/api/storage-scan/start");
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body), {root: "tmp"});
  assert.equal(fetchCalls.filter(call => call.options?.method === "POST").length, 2);
});
