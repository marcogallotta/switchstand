"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const assets = path.join(__dirname, "..", "..", "src", "switchstand", "static");
const state = {
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

let server;
let origin;
let apiRequests;

test.beforeAll(async () => {
  apiRequests = 0;
  server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname === "/api/workbench") {
      apiRequests += 1;
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
