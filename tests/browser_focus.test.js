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
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.selectionDirection = "none";
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
    const matches = selector === "textarea[data-draft-key]"
      && this.tagName === "TEXTAREA"
      && this.dataset.draftKey !== undefined;
    return [
      ...(matches ? [this] : []),
      ...this.children.flatMap((child) => child.querySelectorAll(selector)),
    ];
  }

  setAttribute() {}
  addEventListener() {}

  focus() {
    this.ownerDocument.activeElement = this;
  }

  setSelectionRange(start, end, direction) {
    this.selectionStart = start;
    this.selectionEnd = end;
    this.selectionDirection = direction;
  }
}

class Document {
  constructor() {
    this.body = new Element(this, "body");
    this.activeElement = this.body;
    this.roles = new Element(this, "section");
    this.error = new Element(this, "div");
    this.body.append(this.error, this.roles);
  }

  createElement(tagName) {
    return new Element(this, tagName);
  }

  querySelector(selector) {
    if (selector === "#roles") return this.roles;
    if (selector === "#error") return this.error;
    return null;
  }
}

const state = {
  roles: {
    "role-a": {
      id: "role-a",
      name: "Design",
      generation: 1,
      status: "idle",
      current_attempt_id: null,
      checkpoint: { latest_correction: null, latest_result: null },
    },
    "role-b": {
      id: "role-b",
      name: "Review",
      generation: 1,
      status: "idle",
      current_attempt_id: null,
      checkpoint: { latest_correction: null, latest_result: null },
    },
  },
  attempts: [],
  messages: [],
};

test("background refresh preserves the focused message draft and selection", async () => {
  const document = new Document();
  let intervalCallback;
  const context = {
    console,
    document,
    Error,
    fetch: async () => ({ ok: true, json: async () => state }),
    Map,
    Object,
    setTimeout,
    window: {
      addEventListener() {},
      clearInterval() {},
      setInterval(callback) { intervalCallback = callback; return 1; },
    },
  };
  const scriptPath = path.join(__dirname, "..", "src", "switchstand", "static", "app.js");
  vm.runInNewContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
  await new Promise(setImmediate);

  const before = document.roles.querySelectorAll("textarea[data-draft-key]")[0];
  before.value = "keep this in-progress message";
  before.setSelectionRange(5, 12, "forward");
  before.focus();

  await intervalCallback();

  const after = document.roles.querySelectorAll("textarea[data-draft-key]")[0];
  assert.notStrictEqual(after, before, "the test must exercise a real rerender");
  assert.equal(after.value, "keep this in-progress message");
  assert.strictEqual(document.activeElement, after);
  assert.deepEqual(
    [after.selectionStart, after.selectionEnd, after.selectionDirection],
    [5, 12, "forward"],
  );
});
