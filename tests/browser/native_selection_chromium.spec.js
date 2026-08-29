"use strict";

const fs = require("node:fs");
const { once } = require("node:events");
const http = require("node:http");
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const root = path.join(__dirname, "..", "..");
const assets = path.join(root, "src", "switchstand", "static");
const agents = [
  ["agent-alpha", "First observed agent", null, 0],
  ["agent-beta", "Second observed agent", "agent-alpha", 1],
  ...Array.from({ length: 6 }, (_, index) => [
    `agent-extra-${index}`, `Observed agent ${index + 3}`, "agent-alpha", 1,
  ]),
].map(([agentRef, label, parentRef, depth]) => ({
  agentRef, label, parentRef, depth, sourceKind: "thread/list", sourceDetail: "fixture",
  createdAt: "2026-08-27T11:00:00Z", updatedAt: "2026-08-27T12:00:00Z",
  updatedAgeSeconds: 0, status: depth === 1 ? "idle" : "active",
  turnStatus: depth === 1 ? "inProgress" : "none",
  activeFlags: [], activeObservedSeconds: 10,
}));
const board = {
  mode: "native",
  observation: { connected: true, available: true, historical: false, errorCode: null,
    completedAt: "2026-08-27T12:00:00Z", passAgeSeconds: 0,
    kind: "completed_multi_request_pass" },
  agents,
  trail: [],
  trailLimit: 50,
  disclosure: [
    "Polling may miss intermediate transitions;",
    "trail entries are observed endpoint differences, not native events.",
  ].join(" "),
};

let server;
let origin;
let failWorkbench;
let failSelection;
let observationRunRef;
let selectionOverride;
let delayedSelections;
let inputResults;
let requests;

const json = (response, status, value) => {
  const body = JSON.stringify(value);
  response.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
  response.end(body);
};

const readJson = async (request) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
};

const selectionFor = (agentRef) => {
  const selection = { observationRunRef, agentRef };
  if (selectionOverride) return typeof selectionOverride === "function"
    ? selectionOverride(selection) : selectionOverride;
  return { selection, snapshot: { version: "native-selection-v1", ...selection,
    connected: true, present: true,
    ...(agentRef === "agent-beta" ? { name: "Review agent", agentNickname: "Reviewer",
      preview: "forbidden prompt", threadId: "forbidden native id",
      transcript: ["forbidden transcript"] } : {}) } };
};

test.beforeAll(async () => {
  server = http.createServer(async (request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname === "/api/workbench") {
      requests.push({ pathname, method: request.method, headers: request.headers });
      json(response, failWorkbench ? 503 : 200,
        failWorkbench ? { code: "service_unavailable", outcome: "not_sent" } : board);
      return;
    }
    if (pathname === "/api/native-selection/resolve") {
      const body = await readJson(request);
      requests.push({ pathname, method: request.method, headers: request.headers, body });
      const delayed = delayedSelections.get(body.agentRef);
      if (delayed) await delayed.promise;
      json(response, failSelection ? 503 : 200, failSelection
        ? { code: "service_unavailable", outcome: "not_sent" }
        : selectionFor(body.agentRef));
      return;
    }
    if (pathname === "/api/native-input") {
      const body = await readJson(request);
      requests.push({ pathname, method: request.method, headers: request.headers, body });
      const next = inputResults.shift() ?? { status: 200,
        body: { code: "input_unavailable", outcome: "not_sent" } };
      if (next.promise) await next.promise;
      json(response, next.status, next.body);
      return;
    }
    if (["/api/native-stop/prepare", "/api/native-stop/commit",
      "/api/native-stop/status"].includes(pathname)) {
      const body = await readJson(request);
      requests.push({ pathname, method: request.method, headers: request.headers, body });
      const result = pathname.endsWith("/prepare")
        ? { code: "prepared", agentRef: body.agentRef, confirmationRef: "confirmation-one" }
        : pathname.endsWith("/commit")
          ? { code: "stop_result", operationRef: "operation-one", outcome: "requested" }
          : { code: "stop_result", operationRef: body.operationRef, outcome: "confirmed" };
      json(response, 200, result);
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

test.beforeEach(() => {
  failWorkbench = false;
  failSelection = false;
  observationRunRef = "observation-run-one";
  selectionOverride = null;
  delayedSelections = new Map();
  inputResults = [];
  requests = [];
});

const rowFor = (page, agentRef) => page.locator(`details[data-node-key="${agentRef}"]`);

async function beginSelection(page, agentRef) {
  const row = rowFor(page, agentRef);
  await row.getByRole("button", { name: "Select as current target" }).click();
}

async function selectAgent(page, agentRef) {
  await beginSelection(page, agentRef);
  const row = rowFor(page, agentRef);
  const confirmed = row.getByRole("button", { name: "Current target" });
  await expect(confirmed).toBeVisible();
  await expect(confirmed).toHaveAttribute("aria-pressed", "true");
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function runPoll(page) {
  await page.evaluate(() => refresh());
}

test("explicit selection uses the route, stores only its opaque pair, and reload revalidates", async ({ page }) => {
  await page.goto(origin);
  await expect(page.getByText("No current target selected.")).toBeVisible();
  expect(await page.evaluate(() => localStorage.length)).toBe(0);

  await selectAgent(page, "agent-beta");
  await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
  await expect(page.getByLabel("Message current target")).toBeVisible();
  const selectionRequests = requests.filter((item) => item.pathname === "/api/native-selection/resolve");
  expect(selectionRequests).toHaveLength(1);
  expect(selectionRequests[0].body).toEqual({ agentRef: "agent-beta" });
  expect(selectionRequests[0].headers["x-switchstand-control"]).toBe("native-selection-v1");
  expect(await page.evaluate(() => ({ keys: Object.keys(localStorage),
    value: JSON.parse(localStorage.getItem("switchstand.native-selection.v1")) }))).toEqual({
    keys: ["switchstand.native-selection.v1"],
    value: { observationRunRef: "observation-run-one", agentRef: "agent-beta" },
  });

  await page.reload();
  await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
  expect(requests.filter((item) => item.pathname === "/api/native-selection/resolve").length).toBeGreaterThan(1);
  for (const forbidden of ["forbidden prompt", "forbidden native id", "forbidden transcript"]) {
    await expect(page.locator("body")).not.toContainText(forbidden);
  }
});

test("input sends the exact selected pair and unchanged text with only truthful fixed outcomes", async ({ page }) => {
  inputResults.push(
    { status: 200, body: { code: "input_sent", outcome: "sent", mode: "start" } },
    { status: 200, body: { code: "input_sent", outcome: "sent", mode: "steer" } },
    { status: 200, body: { code: "input_unavailable", outcome: "not_sent" } },
    { status: 503, body: { code: "service_unavailable", outcome: "not_sent" } },
  );
  await page.goto(origin);
  await selectAgent(page, "agent-beta");
  const input = page.getByLabel("Message current target");

  const values = ["  exact start\ntext  ", "exact steer", "retain on refusal", "retain on failure"];
  const outcomes = ["sent · start", "sent · steer", "not sent", "not sent"];
  const statuses = [200, 200, 200, 503];
  for (let index = 0; index < values.length; index += 1) {
    await input.fill(values[index]);
    await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
    await expect(input).toBeVisible();
    await expect(input).toHaveValue(values[index]);
    expect(requests.filter((item) => item.pathname === "/api/native-input")).toHaveLength(index);
    const submit = page.getByRole("button", { name: "Send exact message" });
    await expect(submit).toBeVisible();
    await expect(submit).toBeEnabled();
    const sentRequest = page.waitForRequest(`${origin}/api/native-input`);
    const sentResponse = page.waitForResponse(`${origin}/api/native-input`);
    await submit.click();
    await sentRequest;
    expect((await sentResponse).status()).toBe(statuses[index]);
    await expect.poll(() => requests.filter((item) => item.pathname === "/api/native-input").length)
      .toBe(index + 1);
    expect(requests.filter((item) => item.pathname === "/api/native-input").at(-1).body)
      .toEqual({
        version: "native-input-v1",
        observationRunRef: "observation-run-one",
        agentRef: "agent-beta",
        text: values[index],
      });
    await expect(page.locator("#native-input-outcome")).toHaveText(outcomes[index]);
    await expect(input).toHaveValue(index < 2 ? "" : values[index]);
  }

  const sent = requests.filter((item) => item.pathname === "/api/native-input");
  expect(sent.map((item) => item.body)).toEqual(values.map((text) => ({
    version: "native-input-v1", observationRunRef: "observation-run-one",
    agentRef: "agent-beta", text,
  })));
  expect(sent.every((item) => item.headers["x-switchstand-control"] === "native-input-v1")).toBe(true);
});

test("real timer coalesces one pending cycle and preserves one exact submission", async ({ page }) => {
  inputResults.push(
    { status: 200, body: { code: "input_sent", outcome: "sent", mode: "steer" } },
  );
  await page.goto(origin);
  await selectAgent(page, "agent-beta");
  const input = page.getByLabel("Message current target");
  await input.fill("one exact draft");
  await input.focus();
  await input.evaluate((node) => node.setSelectionRange(4, 9, "forward"));
  const gate = deferred();
  const initialSelectionCount = requests.filter(
    (item) => item.pathname === "/api/native-selection/resolve",
  ).length;
  delayedSelections.set("agent-beta", gate);
  await expect.poll(() => requests.filter(
    (item) => item.pathname === "/api/native-selection/resolve",
  ).length).toBe(initialSelectionCount + 1);
  const pendingCount = requests.filter((item) => item.pathname === "/api/workbench").length;
  const completion = page.evaluate(() => refresh());
  try {
    await page.waitForTimeout(1200);
    expect(requests.filter((item) => item.pathname === "/api/workbench")).toHaveLength(
      pendingCount,
    );
  } finally {
    gate.resolve();
    await completion;
  }
  expect(requests.filter((item) => item.pathname === "/api/workbench")).toHaveLength(
    pendingCount + 1,
  );
  expect(requests.filter((item) => item.pathname === "/api/native-selection/resolve"))
    .toHaveLength(initialSelectionCount + 2);
  await expect(input).toBeFocused();
  await expect(input).toHaveValue("one exact draft");
  expect(await input.evaluate((node) => [
    node.selectionStart, node.selectionEnd, node.selectionDirection,
  ])).toEqual([4, 9, "forward"]);

  const sentRequest = page.waitForRequest(`${origin}/api/native-input`);
  await page.getByRole("button", { name: "Send exact message" }).click();
  await sentRequest;
  const sent = requests.filter((item) => item.pathname === "/api/native-input");
  expect(sent).toHaveLength(1);
  expect(sent[0].body).toEqual({
    version: "native-input-v1", observationRunRef: "observation-run-one",
    agentRef: "agent-beta", text: "one exact draft",
  });
  await expect(page.locator("#native-input-outcome")).toHaveText("sent · steer");
});

test("delayed old selection and input results cannot mutate a newer target or draft", async ({ page }) => {
  await page.goto(origin);
  const oldSelection = deferred();
  delayedSelections.set("agent-alpha", oldSelection);
  await beginSelection(page, "agent-alpha");
  await selectAgent(page, "agent-beta");
  await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
  oldSelection.resolve();
  await page.evaluate(() => 0);
  await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();

  const oldInput = deferred();
  inputResults.push(
    { status: 200, body: { code: "input_sent", outcome: "sent", mode: "start" },
      promise: oldInput.promise },
    { status: 200, body: { code: "input_sent", outcome: "sent", mode: "steer" } },
  );
  const input = page.getByLabel("Message current target");
  await input.fill("old target text");
  await page.evaluate(() => {
    document.querySelector("#native-input-form").requestSubmit();
    document.querySelector("#native-input-form").requestSubmit();
  });
  await expect.poll(() => requests.filter((item) => item.pathname === "/api/native-input").length).toBe(1);
  await selectAgent(page, "agent-alpha");
  await expect(page.getByText("Current target selected.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Send exact message" })).toBeEnabled();
  await input.fill("new target draft");
  await page.getByRole("button", { name: "Send exact message" }).click();
  await expect(page.locator("#native-input-outcome")).toHaveText("sent · steer");
  oldInput.resolve();
  await page.evaluate(() => 0);
  await expect(input).toHaveValue("");
  await expect(page.locator("#native-input-outcome")).toHaveText("sent · steer");
  expect(requests.filter((item) => item.pathname === "/api/native-input")).toHaveLength(2);
  await selectAgent(page, "agent-beta");
  await expect(input).toHaveValue("");
  await expect(page.locator("#native-input-outcome")).toHaveText("sent · start");
});

test("exact-route selection and input preserve Stop confirmation, result, and control header", async ({ page }) => {
  await page.goto(origin);
  await selectAgent(page, "agent-beta");
  const row = rowFor(page, "agent-beta");
  await expect(row.getByText("thread statusidle")).toBeVisible();
  await expect(row.getByText("exact turn statusinProgress")).toBeVisible();
  const warning = [
    "Stop Second observed agent’s current turn? Switchstand will request cancellation of that exact turn only.",
    "Work already performed is not undone. Background processes and descendant agents may continue.",
  ].join(" ");
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toBe(warning);
    await dialog.accept();
  });
  await row.getByRole("button", { name: "Stop current turn" }).click();
  await expect(row.getByText("Stop outcome: requested")).toBeVisible();
  const stopRequests = requests.filter((item) => item.pathname.startsWith("/api/native-stop/"));
  expect(stopRequests.map((item) => [item.pathname, item.body])).toEqual([
    ["/api/native-stop/prepare", { agentRef: "agent-beta" }],
    ["/api/native-stop/commit", { confirmationRef: "confirmation-one" }],
  ]);
  expect(stopRequests.every((item) => item.headers["x-switchstand-control"] === "native-stop-v1"))
    .toBe(true);
});

const invalidEvidenceTestName = [
  "run change, disappearance, staleness, disconnection, malformed response,",
  "and request failure clear without fallback",
].join(" ");
test(invalidEvidenceTestName, async ({ page }) => {
  await page.clock.install();
  await page.goto(origin);
  const failures = [
    () => { observationRunRef = "observation-run-two"; },
    () => { selectionOverride = { selection: null, snapshot: {
      code: "AGENT_NOT_PRESENT", message: "Agent is not present." } }; },
    () => { selectionOverride = { selection: null, snapshot: {
      code: "OBSERVATION_STALE", message: "Observation is stale." } }; },
    () => { selectionOverride = { selection: null, snapshot: {
      code: "APP_SERVER_DISCONNECTED", message: "App Server is disconnected." } }; },
    () => { selectionOverride = { wrong: "shape", rawThreadId: "must-not-render" }; },
    () => { failSelection = true; },
  ];
  for (const arrange of failures) {
    observationRunRef = "observation-run-one";
    selectionOverride = null;
    failSelection = false;
    await selectAgent(page, "agent-beta");
    await expect(page.getByText("Current target: Reviewer · Review agent")).toBeVisible();
    arrange();
    await runPoll(page);
    await expect(page.getByText("No current target selected.")).toBeVisible();
    expect(await page.evaluate(() => localStorage.length)).toBe(0);
    await expect(rowFor(page, "agent-alpha")
      .getByRole("button", { name: "Select as current target" })).toBeVisible();
  }
  await expect(page.locator("body")).not.toContainText("must-not-render");
});

const pollingTestName = [
  "50 polls preserve composer draft, focus, selection, open rows, and scroll;",
  "failure retains the draft",
].join(" ");
test(pollingTestName, async ({ page }) => {
  await page.clock.install();
  await page.goto(origin);
  await selectAgent(page, "agent-beta");
  const row = rowFor(page, "agent-beta");
  const input = page.getByLabel("Message current target");
  await page.evaluate(() => {
    document.body.style.minHeight = "2000px";
    const tree = document.querySelector("#tree");
    tree.style.height = "80px";
    tree.style.overflow = "auto";
  });
  await row.evaluate((node) => { node.open = true; });
  await page.locator("#tree").evaluate((node) => { node.scrollTop = 45; });
  await input.fill("draft survives polling");
  await input.focus();
  await input.evaluate((node) => node.setSelectionRange(2, 9, "forward"));
  await page.evaluate(() => window.scrollTo(0, 130));

  for (let cycle = 0; cycle < 50; cycle += 1) await runPoll(page);
  await expect(input).toBeFocused();
  await expect(input).toHaveValue("draft survives polling");
  expect(await input.evaluate((node) => [node.selectionStart, node.selectionEnd, node.selectionDirection]))
    .toEqual([2, 9, "forward"]);
  expect(await row.evaluate((node) => node.open)).toBe(true);
  expect(await page.locator("#tree").evaluate((node) => node.scrollTop)).toBe(45);
  expect(await page.evaluate(() => window.scrollY)).toBe(130);

  await row.getByRole("button", { name: "Current target" }).focus();
  failWorkbench = true;
  await runPoll(page);
  await expect(page.locator("#observer")).toContainText("Historical snapshot");
  await expect(page.getByText("No current target selected.")).toBeVisible();
  failWorkbench = false;
  await runPoll(page);
  await selectAgent(page, "agent-beta");
  await expect(input).toHaveValue("draft survives polling");
});
