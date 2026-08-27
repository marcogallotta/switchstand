"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..", "..");
const fixtures = JSON.parse(fs.readFileSync(
  path.join(root, "tests", "fixtures", "native_selection_v1.json"), "utf8",
));
const source = fs.readFileSync(
  path.join(root, "src", "switchstand", "static", "native_selection_controller.js"), "utf8",
);

class Storage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  removeItem(key) { this.values.delete(key); }
  setItem(key, value) { this.values.set(key, value); }
}

const tick = () => new Promise(setImmediate);
const plain = (value) => JSON.parse(JSON.stringify(value));
const pair = (observationRunRef, agentRef) => ({ observationRunRef, agentRef });
const seam = (selection, snapshot) => ({ selection, snapshot });
const success = (selection, display = {}) => ({
  version: "native-selection-v1", ...selection, connected: true, present: true, ...display,
});
const failure = (code) => ({ code, message: fixtures.errorMessages[code] });

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function controller(storage, pending = new Map()) {
  const context = { console, globalThis: {} };
  vm.runInNewContext(source, context, { filename: "native_selection_controller.js" });
  const changes = [];
  const created = context.globalThis.SwitchstandNativeSelection.createController({
    storage,
    resolve: (_selection, snapshot) => pending.get(snapshot)?.promise ?? snapshot,
    onChange: (state) => changes.push(state),
  });
  return { changes, controller: created, key: context.globalThis.SwitchstandNativeSelection.storageKey };
}

test("selection is explicit, persists only the exact pair, and projects only safe display", async () => {
  const storage = new Storage();
  const created = controller(storage);
  assert.equal(created.controller.getState().currentTarget, null);
  assert.equal(storage.values.size, 0);

  const selection = pair(fixtures.baseSelection.observationRunRef, fixtures.baseSelection.agentRef);
  const snapshot = { ...fixtures.baseExpected, preview: "prompt secret", threadId: "native secret",
    transcript: ["content secret"], parentRef: "topology secret" };
  created.controller.select(seam(selection, snapshot));
  await tick();

  assert.deepEqual(plain(created.controller.getState().currentTarget), fixtures.baseExpected);
  assert.deepEqual(JSON.parse(storage.getItem(created.key)), fixtures.baseSelection);
  assert.deepEqual([...storage.values.keys()], [created.key]);
  const persisted = storage.getItem(created.key);
  for (const secret of ["prompt secret", "native secret", "content secret", "topology secret",
    fixtures.baseExpected.name, fixtures.baseExpected.agentNickname]) {
    assert.equal(persisted.includes(secret), false);
  }

  const anonymous = pair("observation-run-anonymous", "agent-anonymous");
  created.controller.select(seam(anonymous, success(anonymous)));
  await tick();
  assert.deepEqual(plain(created.controller.getState().currentTarget), {
    version: "native-selection-v1", ...anonymous, connected: true, present: true,
  });
});

test("reload restores only an unresolved candidate and fails closed without fallback", async () => {
  const storage = new Storage();
  const selection = pair(fixtures.baseSelection.observationRunRef, fixtures.baseSelection.agentRef);
  const first = controller(storage);
  first.controller.select(seam(selection, fixtures.baseExpected));
  await tick();

  const restored = controller(storage);
  assert.deepEqual(plain(restored.controller.getState().candidate), selection);
  assert.equal(restored.controller.getState().currentTarget, null);
  restored.controller.supplySeam(seam(selection, failure("APP_SERVER_DISCONNECTED")));
  await tick();
  assert.deepEqual(plain(restored.controller.getState()), { candidate: null, currentTarget: null });
  assert.equal(storage.getItem(restored.key), null);
});

test("delayed old selection success and failure cannot mutate a newer exact selection", async () => {
  const storage = new Storage();
  const pending = new Map();
  const created = controller(storage, pending);
  const oldPair = pair("observation-run-one", "agent-old");
  const newPair = pair("observation-run-one", "agent-new");
  const oldSuccess = success(oldPair, { name: "Old identity" });
  const oldFailure = failure("AGENT_NOT_PRESENT");

  for (const delayedResult of [oldSuccess, oldFailure]) {
    const gate = deferred();
    pending.set(delayedResult, gate);
    created.controller.select(seam(oldPair, delayedResult));
    created.controller.select(seam(newPair, success(newPair, { agentNickname: "New target" })));
    await tick();
    gate.resolve(delayedResult);
    await tick();
    assert.deepEqual(plain(created.controller.getState().currentTarget), {
      version: "native-selection-v1", ...newPair, connected: true, present: true,
      agentNickname: "New target",
    });
    assert.deepEqual(JSON.parse(storage.getItem(created.key)), newPair);
  }
});

test("same-target revalidation retains the last validated target until failure is known", async () => {
  const storage = new Storage();
  const pending = new Map();
  const created = controller(storage, pending);
  const selection = pair("observation-run-one", "agent-current");
  const validated = success(selection, { name: "Current target" });
  created.controller.select(seam(selection, validated));
  await tick();

  const delayedFailure = failure("OBSERVATION_STALE");
  const gate = deferred();
  pending.set(delayedFailure, gate);
  created.controller.supplySeam(seam(selection, delayedFailure));
  assert.deepEqual(plain(created.controller.getState().currentTarget), validated);
  assert.deepEqual(JSON.parse(storage.getItem(created.key)), selection);

  gate.resolve(delayedFailure);
  await tick();
  assert.deepEqual(plain(created.controller.getState()), { candidate: null, currentTarget: null });
  assert.equal(storage.getItem(created.key), null);
});

test("every clearing result fences a delayed older success and never chooses another agent", async () => {
  const storage = new Storage();
  const pending = new Map();
  const created = controller(storage, pending);
  const selected = pair("observation-run-one", "agent-reused");
  const stillAvailable = pair("observation-run-two", "agent-other");

  for (const code of ["INVALID_AGENT_REF", "APP_SERVER_DISCONNECTED", "OBSERVATION_STALE",
    "AGENT_NOT_PRESENT"]) {
    created.controller.select(seam(selected, success(selected, { name: "Selected" })));
    await tick();
    const late = success(selected, { name: "Stale selected identity" });
    const gate = deferred();
    pending.set(late, gate);
    created.controller.supplySeam(seam(selected, late));
    created.controller.supplySeam(seam(stillAvailable, failure(code)));
    await tick();
    gate.resolve(late);
    await tick();
    assert.deepEqual(plain(created.controller.getState()), { candidate: null, currentTarget: null });
    assert.equal(storage.getItem(created.key), null);
  }
});
