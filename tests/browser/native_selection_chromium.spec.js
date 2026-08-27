"use strict";

const fs = require("node:fs");
const { once } = require("node:events");
const http = require("node:http");
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const root = path.join(__dirname, "..", "..");
const assets = path.join(root, "src", "switchstand", "static");
const fixtures = JSON.parse(fs.readFileSync(
  path.join(root, "tests", "fixtures", "native_selection_v1.json"), "utf8",
));
const agents = [
  ["agent-alpha-1", "First observed agent", null, 0],
  ["agent-beta-1", "Second observed agent", "agent-alpha-1", 1],
  ...Array.from({ length: 6 }, (_, index) => [
    `agent-extra-${index}`, `Observed agent ${index + 3}`, "agent-alpha-1", 1,
  ]),
].map(([agentRef, label, parentRef, depth]) => ({
  agentRef, label, parentRef, depth, sourceKind: "thread/list", sourceDetail: "fixture",
  createdAt: "2026-08-27T11:00:00Z", updatedAt: "2026-08-27T12:00:00Z",
  updatedAgeSeconds: 0, status: "active", activeFlags: [], activeObservedSeconds: 10,
}));
const board = {
  mode: "native",
  observation: { connected: true, available: true, historical: false, errorCode: null,
    completedAt: "2026-08-27T12:00:00Z", passAgeSeconds: 0,
    kind: "completed_multi_request_pass" },
  agents,
  trail: [],
  trailLimit: 50,
  disclosure: "Polling may miss intermediate transitions; trail entries are observed endpoint differences, not native events.",
};

let server;
let origin;
let failRequests;

test.beforeAll(async () => {
  server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname === "/api/workbench") {
      if (failRequests) {
        response.writeHead(503, { "Content-Type": "application/json" }).end('{"error":"unavailable"}');
        return;
      }
      response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(board));
      return;
    }
    const names = { "/": "index.html", "/app.js": "app.js",
      "/native_selection_controller.js": "native_selection_controller.js",
      "/styles.css": "styles.css" };
    const name = names[pathname];
    if (!name) {
      response.writeHead(404).end();
      return;
    }
    const body = fs.readFileSync(path.join(assets, name));
    const contentType = name.endsWith(".js") ? "text/javascript"
      : name.endsWith(".css") ? "text/css" : "text/html";
    response.writeHead(200, { "Content-Type": contentType, "Content-Length": body.length }).end(body);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  origin = `http://127.0.0.1:${server.address().port}`;
});

test.afterAll(async () => {
  if (server) {
    server.close();
    await once(server, "close");
  }
});

test.beforeEach(async ({ page }) => {
  failRequests = false;
  await page.addInitScript(({ contract }) => {
    const pair = (observationRunRef, agentRef) => ({ observationRunRef, agentRef });
    const successful = (selection, display = {}) => ({ version: "native-selection-v1",
      ...selection, connected: true, present: true, ...display });
    const errors = contract.errorMessages;
    const invalid = () => ({ code: "INVALID_AGENT_REF", message: errors.INVALID_AGENT_REF });
    const alpha = pair(contract.baseSelection.observationRunRef, contract.baseSelection.agentRef);
    const beta = pair(contract.baseSelection.observationRunRef, "agent-beta-1");
    const seams = new Map([
      [alpha.agentRef, { selection: alpha, snapshot: { ...contract.baseExpected,
        preview: "forbidden prompt", threadId: "forbidden native id",
        transcript: ["forbidden transcript"], parentRef: "forbidden topology" } }],
      [beta.agentRef, { selection: beta, snapshot: successful(beta,
        { name: "Review agent", agentNickname: "Reviewer" }) }],
    ]);
    for (let index = 0; index < 6; index += 1) {
      const extra = pair(contract.baseSelection.observationRunRef, `agent-extra-${index}`);
      seams.set(extra.agentRef, { selection: extra, snapshot: successful(extra) });
    }
    const gates = new Map();
    let listener = () => {};
    let initialGate = null;
    try {
      if (window.name === "delay-selection-resolve") {
        initialGate = {};
        initialGate.promise = new Promise((resolve) => { initialGate.resolve = resolve; });
      }
    } catch (_error) {
      initialGate = null;
    }
    const finish = (selection, snapshot) => {
      if (snapshot?.version === "native-selection-v1"
        && (snapshot.observationRunRef !== selection.observationRunRef
          || snapshot.agentRef !== selection.agentRef)) return invalid();
      return snapshot;
    };
    window.__selectionFake = {
      calls: [],
      delay(snapshot) {
        const gate = {};
        gate.promise = new Promise((resolve) => { gate.resolve = resolve; });
        gates.set(snapshot, gate);
        return gate;
      },
      release(snapshot) { gates.get(snapshot).resolve(); },
      emit(agentRef, seam) {
        seams.set(agentRef, seam);
        listener(seam);
      },
      replace(agentRef, seam) { seams.set(agentRef, seam); },
      releaseInitial() {
        initialGate.resolve();
        initialGate = null;
        window.name = "";
      },
      seam(agentRef) { return seams.get(agentRef); },
    };
    window.switchstandNativeSelectionAdapter = {
      selectionForAgent(agentRef) { return seams.get(agentRef) ?? null; },
      subscribe(callback) { listener = callback; },
      async resolve(selection, snapshot) {
        window.__selectionFake.calls.push({ ...selection });
        const gate = initialGate ?? gates.get(snapshot);
        if (gate) await gate.promise;
        return finish(selection, snapshot);
      },
    };
  }, { contract: fixtures });
});

const rowFor = (page, label) => page.locator("details", { hasText: label });

async function selectAgent(page, label) {
  await rowFor(page, label).getByRole("button", { name: "Select as current target" }).click();
}

test("explicit non-default selection persists only the exact pair and reload re-resolves", async ({ page }) => {
  await page.goto(origin);
  await expect(page.getByText("No current target selected.")).toBeVisible();
  await expect(page.getByText("Current target:")).toHaveCount(0);

  await selectAgent(page, "Observed agent 3");
  await expect(page.getByText("Current target selected.", { exact: true })).toBeVisible();
  await selectAgent(page, "Second observed agent");
  await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
  await expect(rowFor(page, "Second observed agent").getByRole("button", { name: "Current target" })).toBeVisible();
  const evidence = await page.evaluate(() => ({
    calls: window.__selectionFake.calls,
    keys: Object.keys(localStorage),
    stored: JSON.parse(localStorage.getItem("switchstand.native-selection.v1")),
  }));
  expect(evidence.calls.at(-1)).toEqual({
    observationRunRef: fixtures.baseSelection.observationRunRef, agentRef: "agent-beta-1",
  });
  expect(evidence.keys).toEqual(["switchstand.native-selection.v1"]);
  expect(evidence.stored).toEqual(evidence.calls.at(-1));

  await page.evaluate(() => { window.name = "delay-selection-resolve"; });
  await page.reload();
  await expect(page.getByText("No current target selected.")).toBeVisible();
  await expect(page.getByText("Current target: Reviewer · Review agent")).toHaveCount(0);
  await page.evaluate(() => window.__selectionFake.releaseInitial());
  await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("forbidden prompt");
  await expect(page.locator("body")).not.toContainText("forbidden native id");
  await expect(page.locator("body")).not.toContainText("forbidden transcript");
  await expect(page.locator("body")).not.toContainText("forbidden topology");
});

test("delayed old reselection successes and failures cannot mutate the newer selection", async ({ page }) => {
  await page.goto(origin);
  const race = async (oldSnapshot) => {
    const gate = await page.evaluate((snapshot) => {
      const old = window.__selectionFake.seam("agent-alpha-1");
      old.snapshot = snapshot;
      const pending = window.__selectionFake.delay(old.snapshot);
      window.__selectionFake.emit("agent-alpha-1", old);
      return Boolean(pending);
    }, oldSnapshot);
    expect(gate).toBe(true);
    await selectAgent(page, "First observed agent");
    await selectAgent(page, "Second observed agent");
    await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
    await page.evaluate(() => window.__selectionFake.release(
      window.__selectionFake.seam("agent-alpha-1").snapshot,
    ));
    await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
  };
  await race(fixtures.baseExpected);
  await race({ code: "AGENT_NOT_PRESENT", message: fixtures.errorMessages.AGENT_NOT_PRESENT });
});

test("run change, disappearance, stale, and disconnect clear with no fallback or stale resurrection", async ({ page }) => {
  await page.goto(origin);
  const betaPair = { observationRunRef: fixtures.baseSelection.observationRunRef, agentRef: "agent-beta-1" };
  const scenarios = [
    { selection: { observationRunRef: "observation-run-after-restart", agentRef: "agent-beta-1" },
      snapshot: { version: "native-selection-v1", observationRunRef: "observation-run-after-restart",
        agentRef: "agent-beta-1", connected: true, present: true, name: "Reused ref agent" } },
    { selection: betaPair, snapshot: { code: "AGENT_NOT_PRESENT",
      message: fixtures.errorMessages.AGENT_NOT_PRESENT } },
    { selection: betaPair, snapshot: { code: "OBSERVATION_STALE",
      message: fixtures.errorMessages.OBSERVATION_STALE } },
    { selection: betaPair, snapshot: { code: "APP_SERVER_DISCONNECTED",
      message: fixtures.errorMessages.APP_SERVER_DISCONNECTED } },
  ];

  for (const scenario of scenarios) {
    await page.evaluate(({ selection, snapshot }) => {
      window.__selectionFake.emit("agent-beta-1", { selection, snapshot });
    }, { selection: betaPair, snapshot: { version: "native-selection-v1", ...betaPair,
      connected: true, present: true, name: "Review agent", agentNickname: "Reviewer" } });
    await selectAgent(page, "Second observed agent");
    await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
    await page.evaluate((value) => {
      const current = window.__selectionFake.seam("agent-beta-1");
      const pending = window.__selectionFake.delay(current.snapshot);
      window.__selectionFake.emit("agent-beta-1", current);
      window.__lateSelectionGate = pending;
      window.__selectionFake.emit("agent-beta-1", value);
    }, scenario);
    await expect(page.getByText("No current target selected.")).toBeVisible();
    expect(await page.evaluate(() => localStorage.length)).toBe(0);
    await page.evaluate(() => window.__lateSelectionGate.resolve());
    await expect(page.getByText("No current target selected.")).toBeVisible();
    await expect(rowFor(page, "First observed agent")
      .getByRole("button", { name: "Select as current target" })).toBeVisible();
  }
});

test("focus, open state, tree and page scroll survive selection, revalidation, and clearing", async ({ page }) => {
  await page.goto(origin);
  const row = rowFor(page, "Second observed agent");
  const select = row.getByRole("button", { name: "Select as current target" });
  await page.evaluate(() => {
    document.body.style.minHeight = "2000px";
    const tree = document.querySelector("#tree");
    tree.style.height = "80px";
    tree.style.overflow = "auto";
  });
  await row.evaluate((node) => { node.open = true; });
  await select.focus();
  await page.locator("#tree").evaluate((node) => { node.scrollTop = 45; });
  await page.evaluate(() => window.scrollTo(0, 130));
  await select.evaluate((node) => node.click());
  await expect(row.getByRole("button", { name: "Current target" })).toBeFocused();

  await page.evaluate(() => {
    const current = window.__selectionFake.seam("agent-beta-1");
    window.__selectionFake.emit("agent-beta-1", current);
  });
  await expect(row.getByRole("button", { name: "Current target" })).toBeFocused();
  await page.evaluate((message) => {
    const current = window.__selectionFake.seam("agent-beta-1");
    window.__selectionFake.emit("agent-beta-1", { selection: current.selection,
      snapshot: { code: "OBSERVATION_STALE", message } });
  }, fixtures.errorMessages.OBSERVATION_STALE);
  await expect(row.getByRole("button", { name: "Select as current target" })).toBeFocused();
  expect(await row.evaluate((node) => node.open)).toBe(true);
  expect(await page.locator("#tree").evaluate((node) => node.scrollTop)).toBe(45);
  expect(await page.evaluate(() => window.scrollY)).toBe(130);
});

test("workbench refresh failure clears storage and fences a delayed earlier resolve", async ({ page }) => {
  await page.goto(origin);
  await selectAgent(page, "Second observed agent");
  await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
  await page.evaluate((message) => {
    const current = window.__selectionFake.seam("agent-beta-1");
    window.__lateRefreshGate = window.__selectionFake.delay(current.snapshot);
    window.__selectionFake.emit("agent-beta-1", current);
    window.__selectionFake.replace("agent-beta-1", { selection: current.selection,
      snapshot: { code: "APP_SERVER_DISCONNECTED", message } });
  }, fixtures.errorMessages.APP_SERVER_DISCONNECTED);

  failRequests = true;
  await page.waitForResponse((response) => response.url() === `${origin}/api/workbench`
    && response.status() === 503);
  await expect(page.locator("#observer")).toContainText("Historical snapshot");
  await expect(page.getByText("No current target selected.")).toBeVisible();
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
  await page.evaluate(() => window.__lateRefreshGate.resolve());
  await expect(page.getByText("No current target selected.")).toBeVisible();
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
});
