"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(document, tagName) {
    this.ownerDocument = document;
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.value = "";
    this.open = false;
    this.scrollTop = 0;
    this.listeners = {};
    this.style = { setProperty() {} };
  }

  append(...children) {
    children.forEach((child) => {
      child.parentNode = this;
      this.children.push(child);
    });
  }

  contains(target) {
    return this === target || this.children.some((child) => child.contains(target));
  }

  replaceChildren(...children) {
    if (this.contains(this.ownerDocument.activeElement)) {
      this.ownerDocument.activeElement = this.ownerDocument.body;
    }
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.append(...children);
  }

  querySelectorAll(selector) {
    const matches = (selector === "[data-focus-key]" && this.dataset.focusKey !== undefined)
      || (selector === "details[data-node-key]" && this.tagName === "DETAILS" && this.dataset.nodeKey !== undefined)
      || (selector === "details[data-node-key][open]" && this.tagName === "DETAILS" && this.open);
    return [
      ...(matches ? [this] : []),
      ...this.children.flatMap((child) => child.querySelectorAll(selector)),
    ];
  }

  setAttribute() {}
  addEventListener(type, callback) { this.listeners[type] = callback; }

  closest(selector) {
    if (selector === "[data-focus-key]" && this.dataset.focusKey !== undefined) return this;
    return this.parentNode?.closest(selector) ?? null;
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

}

class Document {
  constructor() {
    this.body = new Element(this, "body");
    this.activeElement = this.body;
    this.tree = new Element(this, "div");
    this.trail = new Element(this, "ol");
    this.disclosure = new Element(this, "p");
    this.observer = new Element(this, "section");
    this.currentTarget = new Element(this, "section");
    this.error = new Element(this, "div");
    this.roles = new Element(this, "section");
    this.nativeSurface = new Element(this, "div");
    this.eyebrow = new Element(this, "p");
    this.description = new Element(this, "p");
    this.headlineFacts = new Element(this, "p");
    this.nativeSurface.append(this.observer, this.currentTarget, this.tree, this.trail, this.disclosure);
    this.body.append(this.error, this.eyebrow, this.description, this.headlineFacts, this.nativeSurface, this.roles);
  }

  createElement(tagName) {
    return new Element(this, tagName);
  }

  querySelector(selector) {
    if (selector === "#tree") return this.tree;
    if (selector === "#trail") return this.trail;
    if (selector === "#observer") return this.observer;
    if (selector === "#current-target") return this.currentTarget;
    if (selector === "#disclosure") return this.disclosure;
    if (selector === "#error") return this.error;
    if (selector === "#roles") return this.roles;
    if (selector === "#native-surface") return this.nativeSurface;
    if (selector === "#eyebrow") return this.eyebrow;
    if (selector === "#description") return this.description;
    if (selector === "#headline-facts") return this.headlineFacts;
    return null;
  }
}

const state = {
  mode: "native",
  observation: {
    connected: true, available: true, historical: false, errorCode: null,
    completedAt: "2026-08-27T12:00:00Z", passAgeSeconds: 0, kind: "completed_multi_request_pass",
  },
  agents: [{
    agentRef: "agent-1", label: "Root", parentRef: null, depth: 0,
    sourceKind: "thread/read", sourceDetail: "root", createdAt: "2026-08-27T11:00:00Z",
    updatedAt: "2026-08-27T12:00:00Z", status: "active", activeFlags: ["waiting"],
    activeObservedSeconds: 10, updatedAgeSeconds: 0,
  }],
  trail: [{ observedAt: "2026-08-27T12:00:00Z", agentRef: "agent-1", changes: { status: { from: "idle", to: "active" } } }],
  trailLimit: 50,
  disclosure: "Polling may miss intermediate transitions; trail entries are observed endpoint differences, not native events.",
};

test("fake-DOM seam: 50 refreshes preserve focused tree row, open state, and scroll", async () => {
  const document = new Document();
  let intervalCallback;
  const view = { scrollX: 0, scrollY: 0 };
  const context = {
    console,
    document,
    Error,
    fetch: async () => ({ ok: true, json: async () => state }),
    Map,
    Object,
    Set,
    setTimeout,
    window: {
      addEventListener() {},
      clearInterval() {},
      getSelection() { return { rangeCount: 0 }; },
      get scrollX() { return view.scrollX; },
      get scrollY() { return view.scrollY; },
      scrollTo(x, y) { view.scrollX = x; view.scrollY = y; },
      setInterval(callback) { intervalCallback = callback; return 1; },
    },
  };
  const scriptPath = path.join(__dirname, "..", "src", "switchstand", "static", "app.js");
  vm.runInNewContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
  await new Promise(setImmediate);

  let before = document.tree.querySelectorAll("[data-focus-key]")[0];
  before.open = true;
  before.focus();
  document.tree.scrollTop = 17;
  view.scrollY = 31;

  for (let cycle = 0; cycle < 50; cycle += 1) {
    await intervalCallback();
    const after = document.tree.querySelectorAll("[data-focus-key]")[0];
    assert.notStrictEqual(after, before, "the test must exercise a real rerender");
    assert.strictEqual(document.activeElement, after);
    assert.equal(after.open, true);
    assert.equal(document.tree.scrollTop, 17);
    assert.equal(view.scrollY, 31);
    before = after;
  }
});

test("legacy response retains the two-role surface and reports failed writes", async () => {
  const document = new Document();
  const legacy = {
    roles: {
      a: { id: "a", name: "Role A", generation: 0, status: "idle", current_attempt_id: null,
        checkpoint: { latest_correction: null, latest_result: null } },
      b: { id: "b", name: "Role B", generation: 0, status: "idle", current_attempt_id: null,
        checkpoint: { latest_correction: null, latest_result: null } },
    },
    messages: [], attempts: [],
  };
  const requests = [];
  const context = { console, document, Error, fetch: async (url, options = {}) => {
    requests.push([url, options.method]);
    return options.method === "POST" ? { ok: false, json: async () => ({ error: "denied" }) }
      : { ok: true, json: async () => legacy };
  },
    Map, Object, Set, window: { addEventListener() {}, clearInterval() {}, setInterval() { return 1; },
      getSelection() { return { rangeCount: 0 }; }, scrollX: 0, scrollY: 0, scrollTo() {} } };
  const scriptPath = path.join(__dirname, "..", "src", "switchstand", "static", "app.js");
  vm.runInNewContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
  await new Promise(setImmediate);
  assert.equal(document.roles.hidden, false);
  assert.equal(document.nativeSurface.hidden, true);
  assert.equal(document.roles.children.length, 2);
  const send = document.roles.children[0].children[2];
  send.children[0].value = "hello";
  await send.listeners.submit({ preventDefault() {} });
  assert.deepEqual(requests.at(-1), ["/api/workbench/roles/a/messages", "POST"]);
  assert.equal(document.error.textContent, "denied");
  assert.equal(document.error.hidden, false);
});

test("fake-DOM refresh failure clears selection and fences its delayed prior result", async () => {
  const document = new Document();
  const values = new Map();
  const storage = { getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => values.delete(key), setItem: (key, value) => values.set(key, value) };
  const second = { ...state.agents[0], agentRef: "agent-2", label: "Second agent", parentRef: "agent-1" };
  const pairs = new Map(["agent-1", "agent-2"].map((agentRef) => [agentRef,
    { observationRunRef: "observation-run", agentRef }]));
  const seams = new Map([...pairs].map(([agentRef, selection]) => [agentRef, { selection,
    snapshot: { version: "native-selection-v1", ...selection, connected: true, present: true } }]));
  let supplied;
  let intervalCallback;
  let failRefresh = false;
  let delayedSnapshot;
  let releaseDelayed;
  const delayed = new Promise((resolve) => { releaseDelayed = resolve; });
  const view = { scrollX: 0, scrollY: 0 };
  const window = { addEventListener() {}, clearInterval() {}, getSelection() { return { rangeCount: 0 }; },
    localStorage: storage, scrollX: 0, scrollY: 0, scrollTo(x, y) { view.scrollX = x; view.scrollY = y; },
    setInterval(callback) { intervalCallback = callback; return 1; }, switchstandNativeSelectionAdapter: {
      resolve: async (_selection, snapshot) => {
        if (snapshot === delayedSnapshot) await delayed;
        return snapshot;
      },
      selectionForAgent: (agentRef) => seams.get(agentRef),
      subscribe(callback) { supplied = callback; },
    } };
  Object.defineProperties(window, { scrollX: { get: () => view.scrollX }, scrollY: { get: () => view.scrollY } });
  const context = { console, document, Error, fetch: async () => failRefresh
    ? { ok: false, json: async () => ({ error: "unavailable" }) }
    : { ok: true, json: async () => state },
    Map, Object, Set, setTimeout, window };
  const controllerPath = path.join(__dirname, "..", "src", "switchstand", "static",
    "native_selection_controller.js");
  vm.runInNewContext(fs.readFileSync(controllerPath, "utf8"), context, { filename: controllerPath });
  window.SwitchstandNativeSelection = context.SwitchstandNativeSelection;
  state.agents.push(second);
  const scriptPath = path.join(__dirname, "..", "src", "switchstand", "static", "app.js");
  vm.runInNewContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
  await new Promise(setImmediate);
  const focused = document.tree.querySelectorAll("[data-focus-key]")
    .find((node) => node.dataset.focusKey === "select:agent-2");
  focused.parentNode.open = true;
  focused.focus();
  document.tree.scrollTop = 19;
  view.scrollY = 37;
  focused.listeners.click();
  await new Promise(setImmediate);
  assert.equal(values.size, 1);
  delayedSnapshot = { version: "native-selection-v1", ...pairs.get("agent-2"),
    connected: true, present: true, name: "Late identity" };
  supplied({ selection: pairs.get("agent-2"), snapshot: delayedSnapshot });
  seams.set("agent-2", { selection: pairs.get("agent-2"), snapshot: {
    code: "APP_SERVER_DISCONNECTED", message: "Agent connection is unavailable.",
  } });
  failRefresh = true;
  await intervalCallback();
  releaseDelayed();
  await new Promise(setImmediate);
  const after = document.tree.querySelectorAll("[data-focus-key]")
    .find((node) => node.dataset.focusKey === "select:agent-2");
  assert.strictEqual(document.activeElement, after);
  assert.equal(after.parentNode.open, true);
  assert.equal(document.tree.scrollTop, 19);
  assert.equal(view.scrollY, 37);
  assert.equal(values.size, 0);
  assert.equal(document.currentTarget.children[0].textContent, "No current target selected.");
  assert.equal(document.observer.children[0].textContent, "Historical snapshot");
  state.agents.pop();
});
