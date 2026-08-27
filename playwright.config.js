"use strict";

const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  use: {
    browserName: "chromium",
    screenshot: "off",
    trace: "off",
    video: "off",
  },
});
