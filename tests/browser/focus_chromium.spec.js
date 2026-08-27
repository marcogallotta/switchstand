"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const assets = path.join(__dirname, "..", "..", "src", "switchstand", "static");
const syntheticState = {
  roles: {
    design: {
      id: "design",
      name: "Design",
      generation: 1,
      status: "idle",
      current_attempt_id: null,
      checkpoint: { latest_correction: null, latest_result: null },
    },
  },
  attempts: [],
  messages: [],
};
let currentState = syntheticState;

function nativeState({ sequence, connected = true, historical = false, status = "active", activeSeconds = 5 }) {
  const threads = Array.from({ length: 18 }, (_, index) => ({
    ref: `thread-${index + 1}`,
    label: index === 0 ? "Root" : `Agent ${index}`,
    parentRef: index === 0 ? null : "thread-1",
    depth: index === 0 ? 0 : 1,
    source: index === 0 ? "cli" : "subAgent:review",
    createdAt: 100 + index,
    updatedAt: 180 + index,
    status: index === 1
      ? { type: status, ...(status === "active" ? { activeFlags: ["waitingOnUserInput"] } : {}) }
      : { type: "idle" },
    activeObservedSeconds: index === 1 && status === "active" ? activeSeconds : null,
  }));
  return {
    mode: "native",
    readOnly: true,
    passSequence: sequence,
    observation: {
      connected,
      historical,
      errorCode: connected ? null : "native_observation_unavailable",
      completedAt: 190,
      kind: "completed multi-request observation pass",
      caveat: "Polling may miss intermediate endpoint transitions.",
    },
    threads,
    differences: Array.from({ length: 20 }, (_, index) => ({
      observedAt: 190,
      threadRef: `thread-${(index % 17) + 2}`,
      field: "status",
      before: { type: "active", activeFlags: [] },
      after: { type: "idle" },
    })),
  };
}

let server;
let origin;
let apiRequests;

test.beforeAll(async () => {
  apiRequests = 0;
  server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname === "/api/workbench") {
      apiRequests += 1;
      const body = Buffer.from(JSON.stringify(currentState));
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

async function waitForReplacement(page) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const value = await page.evaluate(() => {
      const current = document.querySelector("textarea[data-draft-key]");
      if (!current || current === window.__previousTextarea) return null;
      window.__previousTextarea = current;
      return {
        value: current.value,
        focused: document.activeElement === current,
        start: current.selectionStart,
        end: current.selectionEnd,
        direction: current.selectionDirection,
      };
    });
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("textarea was not replaced after refresh");
}

test("real Chromium preserves draft, focus, and selection across 50 replaced textareas", async ({ page }) => {
  currentState = syntheticState;
  apiRequests = 0;
  await page.clock.install();
  await page.goto(origin);
  const textarea = page.locator("textarea[data-draft-key]").first();
  await textarea.waitFor();
  await expect.poll(() => apiRequests).toBe(1);
  await page.evaluate(() => {
    const input = document.querySelector("textarea[data-draft-key]");
    input.value = "keep this in-progress message";
    input.focus();
    input.setSelectionRange(5, 12, "forward");
    window.__previousTextarea = input;
  });

  for (let cycle = 1; cycle <= 50; cycle += 1) {
    const response = page.waitForResponse(`${origin}/api/workbench`);
    await page.clock.runFor(1000);
    await response;
    expect(await waitForReplacement(page)).toEqual({
      value: "keep this in-progress message",
      focused: true,
      start: 5,
      end: 12,
      direction: "forward",
    });
    expect(apiRequests).toBe(cycle + 1);
  }
  expect(apiRequests).toBe(51);
});

test("native flight board preserves focus, text selection, and scroll across 50 refreshes", async ({ page }) => {
  currentState = nativeState({ sequence: 1 });
  apiRequests = 0;
  await page.clock.install({ time: 200000 });
  await page.goto(origin);
  const card = page.locator('[data-focus-key="thread-2"]');
  await card.waitFor();
  await expect(card).toContainText("Agent 1");
  await expect(card).toContainText("waitingOnUserInput");
  await expect(card).toContainText("consecutive observed active 5s");
  await expect(page.locator(".flight-meta").first()).toContainText("multi-request observation pass");
  const initial = await page.evaluate(() => {
    const cardNode = document.querySelector('[data-focus-key="thread-2"]');
    const selectionNode = document.querySelector('[data-selection-key="source:thread-2"]');
    const trail = document.querySelector('[data-scroll-key="differences"]');
    cardNode.focus({ preventScroll: true });
    const selection = window.getSelection();
    selection.setBaseAndExtent(selectionNode.firstChild, 0, selectionNode.firstChild, selectionNode.textContent.length);
    trail.scrollTop = 90;
    window.scrollTo(0, 500);
    window.__previousNativeCard = cardNode;
    return { scrollY: window.scrollY, trailScroll: trail.scrollTop };
  });
  expect(initial.scrollY).toBeGreaterThan(0);
  expect(initial.trailScroll).toBeGreaterThan(0);

  for (let cycle = 1; cycle <= 50; cycle += 1) {
    if (cycle === 20) currentState = nativeState({ sequence: 2, activeSeconds: 10 });
    if (cycle === 30) currentState = nativeState({ sequence: 2, connected: false, historical: true });
    if (cycle === 40) currentState = nativeState({ sequence: 3, status: "idle" });
    if (cycle === 45) currentState = nativeState({ sequence: 4, activeSeconds: 0 });
    const response = page.waitForResponse(`${origin}/api/workbench`);
    await page.clock.runFor(1000);
    await response;
    const value = await expect.poll(() => page.evaluate(() => {
      const current = document.querySelector('[data-focus-key="thread-2"]');
      if (!current || current === window.__previousNativeCard) return null;
      window.__previousNativeCard = current;
      return {
        focused: document.activeElement === current,
        selection: window.getSelection().toString(),
        scrollY: window.scrollY,
        trailScroll: document.querySelector('[data-scroll-key="differences"]').scrollTop,
      };
    })).not.toBeNull();
    void value;
    const preserved = await page.evaluate(() => ({
      focused: document.activeElement === document.querySelector('[data-focus-key="thread-2"]'),
      selection: window.getSelection().toString(),
      scrollY: window.scrollY,
      trailScroll: document.querySelector('[data-scroll-key="differences"]').scrollTop,
    }));
    expect(preserved).toEqual({
      focused: true,
      selection: "subAgent:review",
      scrollY: initial.scrollY,
      trailScroll: initial.trailScroll,
    });
    if (cycle === 10) await expect(card).toContainText("consecutive observed active 5s");
    if (cycle === 20) await expect(card).toContainText("consecutive observed active 10s");
    if (cycle === 30) {
      await expect(page.locator(".flight-meta").first()).toContainText("historical evidence");
      await expect(card).toContainText("consecutive observed active unavailable");
    }
    if (cycle === 40) await expect(card).toContainText("consecutive observed active unavailable");
    if (cycle === 45) await expect(card).toContainText("consecutive observed active 0s");
  }
  expect(apiRequests).toBe(51);
});
