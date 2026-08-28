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
  const completion = created.controller.select(seam(selection, snapshot));
  assert.equal(typeof completion.then, "function");
  assert.equal(created.controller.getState().currentTarget, null);
  await completion;

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
  const noOp = restored.controller.supplySeam(seam(selection, fixtures.baseExpected));
  assert.equal(typeof noOp.then, "function");
  await noOp;
  const cleared = restored.controller.select({ wrong: "shape" });
  assert.equal(typeof cleared.then, "function");
  await cleared;
  assert.deepEqual(plain(restored.controller.getState()), { candidate: null, currentTarget: null });
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

test("same-target success and failure complete only after their deferred result commits", async () => {
  const storage = new Storage();
  const pending = new Map();
  const created = controller(storage, pending);
  const selection = pair("observation-run-one", "agent-current");
  const validated = success(selection, { name: "Current target" });
  const delayedResults = [
    success(selection, { name: "Revalidated target" }),
    failure("OBSERVATION_STALE"),
  ];
  for (const delayedResult of delayedResults) {
    await created.controller.select(seam(selection, validated));
    const gate = deferred();
    pending.set(delayedResult, gate);
    const priorChanges = created.changes.length;
    let settled = false;
    const completion = created.controller.supplySeam(seam(selection, delayedResult))
      .then(() => { settled = true; });
    assert.equal(created.changes.length, priorChanges);
    assert.deepEqual(plain(created.controller.getState().currentTarget), validated);
    assert.deepEqual(JSON.parse(storage.getItem(created.key)), selection);
    await tick();
    assert.equal(settled, false);

    gate.resolve(delayedResult);
    await completion;
    assert.equal(settled, true);
    if (delayedResult.code) {
      assert.deepEqual(plain(created.controller.getState()), { candidate: null, currentTarget: null });
      assert.equal(storage.getItem(created.key), null);
    } else {
      assert.deepEqual(plain(created.controller.getState().currentTarget), delayedResult);
      assert.deepEqual(JSON.parse(storage.getItem(created.key)), selection);
    }
  }
});

test("deferred explicit switch clears confirmed A and a delayed A cannot resurrect", async () => {
  const storage = new Storage();
  const pending = new Map();
  const created = controller(storage, pending);
  const selectionA = pair("observation-run-one", "agent-a");
  const selectionB = pair("observation-run-one", "agent-b");
  const confirmedA = success(selectionA, { name: "Agent A" });
  const lateA = success(selectionA, { name: "Late Agent A" });
  const confirmedB = success(selectionB, { name: "Agent B" });
  await created.controller.select(seam(selectionA, confirmedA));

  const gateA = deferred();
  pending.set(lateA, gateA);
  const completionA = created.controller.supplySeam(seam(selectionA, lateA));
  const gateB = deferred();
  pending.set(confirmedB, gateB);
  let settledB = false;
  const completionB = created.controller.select(seam(selectionB, confirmedB))
    .then(() => { settledB = true; });
  assert.deepEqual(plain(created.controller.getState()), {
    candidate: selectionB,
    currentTarget: null,
  });
  assert.equal(storage.getItem(created.key), null);
  await tick();
  assert.equal(settledB, false);

  gateA.resolve(lateA);
  await completionA;
  assert.equal(settledB, false);
  assert.deepEqual(plain(created.controller.getState()), {
    candidate: selectionB,
    currentTarget: null,
  });
  assert.equal(storage.getItem(created.key), null);

  gateB.resolve(confirmedB);
  await completionB;
  assert.equal(settledB, true);
  assert.deepEqual(plain(created.controller.getState().currentTarget), confirmedB);
  assert.deepEqual(JSON.parse(storage.getItem(created.key)), selectionB);
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
