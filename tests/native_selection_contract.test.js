"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const fixtures = JSON.parse(
  fs.readFileSync(path.join(__dirname, "fixtures", "native_selection_v1.json"), "utf8"),
);

function expected(caseFixture) {
  if (caseFixture.expectedBase) return fixtures.baseExpected;
  if (caseFixture.expectedError) {
    return {
      code: caseFixture.expectedError,
      message: fixtures.errorMessages[caseFixture.expectedError],
    };
  }
  return caseFixture.expected;
}

test("shared native-selection-v1 fixtures execute through the production resolver", () => {
  const environment = { ...process.env, PYTHONPATH: path.join(root, "src") };
  const python = process.env.PYTHON || "python";
  const bridge = spawnSync(python, [path.join(__dirname, "native_selection_contract_runner.py")], {
    cwd: root,
    env: environment,
    input: JSON.stringify(fixtures),
    encoding: "utf8",
  });

  assert.equal(bridge.status, 0, bridge.stderr);
  const actual = JSON.parse(bridge.stdout);
  assert.deepEqual(actual.resolveResults, fixtures.resolveCases.map(expected));
  for (const privacyResult of actual.privacyResults) {
    assert.deepEqual(privacyResult.projected, fixtures.baseExpected);
    assert.equal(privacyResult.rejected, true);
  }
  assert.equal(actual.unknownRejected, true);
});
