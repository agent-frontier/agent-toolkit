const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function application() {
  const nodes = new Map();
  const requests = [];
  function element() {
    return {
      value: "", children: [], textContent: "",
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener() {},
    };
  }
  const document = {
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
    createElement: element,
  };
  const context = vm.createContext({
    document, URLSearchParams, location: { hash: "" },
    AbortSignal, clearTimeout() {},
    fetch(url) { requests.push(url); return new Promise(() => {}); },
    sessionStorage: { getItem() { return null; } },
    fixture: {
      system_services: { items: [{ unit: "active-system.service", scope: "system", active: "active", sub: "running", description: "system worker" }] },
      user_services: { items: [{ unit: "failed-user.service", scope: "user", active: "failed", sub: "failed", description: "user failure" }] },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../dashboard/app.js"), "utf8"), context);
  document.getElementById("service-state").value = "live";
  return { nodes, context, requests };
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
