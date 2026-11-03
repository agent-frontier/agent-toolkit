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
    AbortSignal,
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
  return { nodes, context, requests, pending, timers, fetchCalls };
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
  const response = () => ({status: 200, ok: true, json: async () => ({generated_at: new Date().toISOString(), status: "healthy"})});
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
