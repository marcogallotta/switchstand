"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const assets = path.join(__dirname, "..", "..", "src", "switchstand", "static");
const state = {
  mode: "native",
  observation: {
    connected: true, available: true, historical: false, errorCode: null,
    completedAt: "2026-08-27T12:00:00Z", passAgeSeconds: 0, kind: "completed_multi_request_pass",
  },
  agents: [{ agentRef: "agent-1", label: "Root", parentRef: null, depth: 0,
    sourceKind: "thread/read", sourceDetail: "root", createdAt: "2026-08-27T11:00:00Z",
    updatedAt: "2026-08-27T12:00:00Z", updatedAgeSeconds: 0, status: "active",
    activeFlags: ["waiting"], activeObservedSeconds: 10 }],
  trail: [{ observedAt: "2026-08-27T12:00:00Z", agentRef: "agent-1",
    changes: { status: { from: "idle", to: "active" } } }],
  trailLimit: 50,
  disclosure: "Polling may miss intermediate transitions; trail entries are observed endpoint differences, not native events.",
};

let server;
let origin;
let apiRequests;
let failRequests;
let stopRequests;
let stopOutcome;

test.beforeAll(async () => {
  apiRequests = 0;
  failRequests = false;
  stopRequests = [];
  server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname.startsWith("/api/native-stop/")) {
      stopRequests.push({ pathname, header: request.headers["x-switchstand-control"] });
      const value = pathname.endsWith("prepare")
        ? { code: "prepared", agentRef: "agent-1", confirmationRef: "opaque-confirmation" }
        : { code: "stop_result", operationRef: "opaque-confirmation", outcome: stopOutcome };
      response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(value));
      return;
    }
    if (pathname === "/api/workbench") {
      apiRequests += 1;
      if (failRequests) {
        response.writeHead(503, { "Content-Type": "application/json" }).end('{"error":"unavailable"}');
        return;
      }
      const body = Buffer.from(JSON.stringify(state));
      response.writeHead(200, { "Content-Type": "application/json", "Content-Length": body.length });
      response.end(body);
      return;
    }
    const names = { "/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css" };
    const name = names[pathname];
    if (!name) {
      response.writeHead(404).end();
      return;
    }
    const body = fs.readFileSync(path.join(assets, name));
    const type = name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "text/html";
    response.writeHead(200, { "Content-Type": type, "Content-Length": body.length });
    response.end(body);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  origin = `http://127.0.0.1:${server.address().port}`;
});

test.afterAll(async () => {
  if (server) await new Promise((resolve) => server.close(resolve));
});

test.beforeEach(() => {
  failRequests = false;
  stopRequests = [];
  stopOutcome = "requested";
  state.agents[0].status = "active";
});

async function waitForReplacement(page) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const value = await page.evaluate(() => {
      const current = document.querySelector("details[data-focus-key]");
      if (!current || current === window.__previousRow) return null;
      window.__previousRow = current;
      return {
        focused: document.activeElement === current,
        open: current.open,
        selected: window.getSelection().toString(),
        scrollY: window.scrollY,
      };
    });
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("tree row was not replaced after refresh");
}

test("real Chromium preserves tree focus, selection, open state, and scroll across 50 refreshes", async ({ page }) => {
  await page.clock.install();
  await page.goto(origin);
  const row = page.locator("details[data-focus-key]").first();
  await row.waitFor();
  await expect.poll(() => apiRequests).toBe(1);
  await expect(page.locator("body")).not.toContainText("agent-1");
  await expect(page.getByText("consecutive observed active")).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop current turn" })).toHaveCount(1);
  await page.evaluate(() => {
    document.body.style.minHeight = "2000px";
    const current = document.querySelector("details[data-focus-key]");
    current.open = true;
    current.focus();
    const text = current.querySelector("strong").firstChild;
    window.getSelection().setBaseAndExtent(text, 0, text, 4);
    window.scrollTo(0, 120);
    window.__previousRow = current;
  });

  for (let cycle = 1; cycle <= 50; cycle += 1) {
    const response = page.waitForResponse(`${origin}/api/workbench`);
    await page.clock.runFor(1000);
    await response;
    expect(await waitForReplacement(page)).toEqual({
      focused: true,
      open: true,
      selected: "Root",
      scrollY: 120,
    });
    expect(apiRequests).toBe(cycle + 1);
  }
  expect(apiRequests).toBe(51);

  failRequests = true;
  const response = page.waitForResponse(`${origin}/api/workbench`);
  await page.clock.runFor(1000);
  await response;
  await expect(page.locator("#observer")).toContainText("Historical snapshot");
  await expect(page.locator("#tree")).toContainText("Root");
  await expect(page.locator("#error")).toContainText("displayed board is a historical snapshot");
});

test("confirmation cancel, confirm, and refresh never replay a native stop", async ({ page }) => {
  await page.goto(origin);
  const stop = page.getByRole("button", { name: "Stop current turn" });
  const prompt = "Stop Root’s current turn? Switchstand will request cancellation of that exact turn only. Work already performed is not undone. Background processes and descendant agents may continue.";
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toBe(prompt);
    await dialog.dismiss();
  });
  await stop.click();
  await expect(page.getByText("Stop outcome: not_sent")).toBeVisible();
  expect(stopRequests.map((value) => value.pathname)).toEqual(["/api/native-stop/prepare"]);

  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toBe(prompt);
    await dialog.accept();
  });
  await page.getByRole("button", { name: "Stop current turn" }).click();
  await expect(page.getByText("Stop outcome: requested")).toBeVisible();
  expect(stopRequests.map((value) => value.pathname)).toEqual([
    "/api/native-stop/prepare", "/api/native-stop/prepare", "/api/native-stop/commit",
  ]);
  expect(stopRequests.every((value) => value.header === "native-stop-v1")).toBe(true);

  await page.reload();
  await expect(page.getByRole("button", { name: "Stop current turn" })).toBeVisible();
  expect(stopRequests).toHaveLength(3);
});

test("a later active turn can be stopped without reloading", async ({ page }) => {
  stopOutcome = "confirmed";
  await page.goto(origin);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Stop current turn" }).click();
  await expect(page.getByText("Stop outcome: confirmed")).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop current turn" })).toHaveCount(0);

  state.agents[0].status = "idle";
  await page.waitForResponse(`${origin}/api/workbench`);
  state.agents[0].status = "active";
  await page.waitForResponse(`${origin}/api/workbench`);

  await expect(page.getByRole("button", { name: "Stop current turn" })).toBeVisible();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Stop current turn" }).click();
  expect(stopRequests.map((value) => value.pathname)).toEqual([
    "/api/native-stop/prepare", "/api/native-stop/commit", "/api/native-stop/prepare",
  ]);
});
