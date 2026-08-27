"use strict";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function request(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error ?? `Request failed (${response.status})`);
  return value;
}

const attemptFor = (state, id) => state.attempts.find((attempt) => attempt.id === id) ?? null;

function badge(status) {
  const node = el("span", `status status--${status}`, status);
  node.setAttribute("aria-label", `State: ${status}`);
  return node;
}

function messageRow(message) {
  const row = el("li", "record");
  const identity = el("div", "record__summary");
  identity.append(el("strong", null, `${message.sequence}. ${message.kind}`), badge(message.status));
  row.append(identity, el("p", null, message.text));
  if (message.result) row.append(el("pre", "output", message.result));
  return row;
}

function attemptRow(attempt, currentId) {
  const row = el("li", "record");
  const summary = el("div", "record__summary");
  summary.append(
    el("code", null, attempt.id),
    badge(attempt.status),
    el("span", "muted", `generation ${attempt.generation}${attempt.id === currentId ? " · current" : ""}`),
  );
  row.append(summary);
  if (attempt.thread_id) row.append(el("div", "muted", `thread ${attempt.thread_id}`));
  if (attempt.turn_id) row.append(el("div", "muted", `turn ${attempt.turn_id}`));
  if (attempt.error) row.append(el("p", "error", attempt.error));
  if (attempt.stale_output) {
    row.append(el("strong", "stale-label", "Stale output — visible, not accepted"));
    row.append(el("pre", "output output--stale", attempt.stale_output));
  }
  return row;
}

function roleCard(state, role, refresh, reportError) {
  const card = el("article", "role-card");
  const heading = el("header", "role-card__header");
  const title = el("div");
  title.append(el("h2", null, role.name), el("div", "muted", `${role.id} · generation ${role.generation}`));
  heading.append(title, badge(role.status));
  card.append(heading);

  const checkpoint = el("section", "inset");
  checkpoint.append(el("h3", null, "Accepted checkpoint"));
  checkpoint.append(el("p", null, role.checkpoint.latest_correction ?? "No accepted correction yet."));
  if (role.checkpoint.latest_result) checkpoint.append(el("pre", "output", role.checkpoint.latest_result));
  card.append(checkpoint);

  const send = el("form", "form");
  const input = el("textarea");
  input.dataset.draftKey = `message:${role.id}`;
  input.placeholder = `Message ${role.name} directly`;
  input.required = true;
  const sendButton = el("button", "button button--primary", "Send message");
  sendButton.type = "submit";
  send.append(input, sendButton);
  send.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await request(`/api/workbench/roles/${role.id}/messages`, { text: input.value, kind: "message" });
      input.value = "";
      await refresh();
    } catch (error) { reportError(error); }
  });
  card.append(send);

  const current = attemptFor(state, role.current_attempt_id);
  if (current) {
    const controls = el("section", "inset form");
    controls.append(el("h3", null, "Exact attempt controls"));
    const correction = el("textarea");
    correction.dataset.draftKey = `correction:${current.id}`;
    correction.placeholder = "Correction for an exact redirect";
    const actions = el("div", "actions");
    if (["running", "waiting"].includes(current.status)) {
      const redirect = el("button", "button", "Redirect");
      redirect.type = "button";
      redirect.addEventListener("click", async () => {
        try {
          await request(`/api/workbench/attempts/${current.id}/redirect`, { text: correction.value });
          correction.value = "";
          await refresh();
        } catch (error) { reportError(error); }
      });
      const stop = el("button", "button button--danger", "Stop");
      stop.type = "button";
      stop.addEventListener("click", async () => {
        try { await request(`/api/workbench/attempts/${current.id}/stop`, {}); await refresh(); }
        catch (error) { reportError(error); }
      });
      actions.append(redirect, stop);
    }
    if (["stopped", "stale", "failed", "unknown"].includes(current.status)) {
      const replace = el("button", "button button--primary", "Replace from checkpoint");
      replace.type = "button";
      replace.addEventListener("click", async () => {
        try { await request(`/api/workbench/attempts/${current.id}/replace`, {}); await refresh(); }
        catch (error) { reportError(error); }
      });
      actions.append(replace);
    }
    controls.append(correction, actions);
    card.append(controls);
  }

  const messages = state.messages.filter((message) => message.role_id === role.id);
  const messageSection = el("section");
  messageSection.append(el("h3", null, "Durable messages"));
  const messageList = el("ol", "list");
  messages.forEach((message) => messageList.append(messageRow(message)));
  if (!messages.length) messageList.append(el("li", "muted", "No messages yet."));
  messageSection.append(messageList);
  card.append(messageSection);

  const attempts = state.attempts.filter((attempt) => attempt.role_id === role.id).reverse();
  const attemptSection = el("section");
  attemptSection.append(el("h3", null, "Attempt identity and history"));
  const attemptList = el("ul", "list");
  attempts.forEach((attempt) => attemptList.append(attemptRow(attempt, role.current_attempt_id)));
  if (!attempts.length) attemptList.append(el("li", "muted", "No attempt yet."));
  attemptSection.append(attemptList);
  card.append(attemptSection);
  return card;
}

function age(value) {
  if (value === null || value === undefined) return "unavailable";
  return `${Math.max(0, Math.floor(Date.now() / 1000 - value))}s ago`;
}

function selectable(key, text) {
  const node = el("span", null, text);
  node.dataset.selectionKey = key;
  return node;
}

function nativeThread(thread, labels, observation) {
  const card = el("article", "native-thread");
  card.dataset.focusKey = thread.ref;
  card.tabIndex = 0;
  card.style.setProperty("--depth", thread.depth);
  const heading = el("div", "record__summary");
  heading.append(el("h2", null, thread.label), badge(thread.status.type));
  const facts = el("div", "native-thread__facts muted");
  const source = el("span", null, "source ");
  source.append(selectable(`source:${thread.ref}`, thread.source));
  const updated = el("span", null, "updated ");
  updated.append(selectable(`updated:${thread.ref}`, age(thread.updatedAt)));
  facts.append(
    el("span", null, `parent ${thread.parentRef ? labels.get(thread.parentRef) : "none"}`),
    el("span", null, `depth ${thread.depth}`),
    source,
    updated,
  );
  const active = thread.status.type === "active" && observation.connected && !observation.historical
    ? `${Math.floor(thread.activeObservedSeconds)}s`
    : "unavailable";
  facts.append(el("span", null, `consecutive observed active ${active}`));
  const flags = thread.status.activeFlags ?? [];
  if (flags.length) facts.append(el("span", null, `flags ${flags.join(", ")}`));
  card.append(heading, facts);
  return card;
}

function flightBoard(state) {
  const board = el("section", "flight-board");
  const observation = state.observation;
  const meta = el("header", `flight-meta${observation.historical ? " historical" : ""}`);
  meta.append(
    el("p", "eyebrow", "Read-only native agent flight board"),
    el("h2", null, observation.connected ? "Observer connected" : "Observer unavailable"),
    el("p", null, `${observation.kind}; last complete pass ${age(observation.completedAt)}.`),
    el("p", "muted", `${observation.caveat} Observed endpoint differences are neither atomic snapshots nor native events.`),
  );
  if (observation.errorCode) meta.append(el("p", "error", observation.errorCode));
  if (observation.historical) meta.append(el("p", "error", "Showing the last complete pass as historical evidence."));
  board.append(meta);

  const labels = new Map(state.threads.map((thread) => [thread.ref, thread.label]));
  const tree = el("section", "native-tree");
  state.threads.forEach((thread) => tree.append(nativeThread(thread, labels, observation)));
  if (!state.threads.length) tree.append(el("p", "muted", "No complete observation pass is available."));
  board.append(tree);

  const trail = el("section", "flight-meta difference-trail");
  trail.dataset.scrollKey = "differences";
  trail.append(el("h2", null, "Observed endpoint differences"));
  const list = el("ol", "list");
  [...state.differences].reverse().forEach((item) => {
    const before = typeof item.before === "object" ? JSON.stringify(item.before) : item.before;
    const after = typeof item.after === "object" ? JSON.stringify(item.after) : item.after;
    list.append(el("li", "record", `${labels.get(item.threadRef) ?? item.threadRef}: ${item.field} · ${before} → ${after}`));
  });
  if (!state.differences.length) list.append(el("li", "muted", "No endpoint difference observed yet."));
  trail.append(list);
  board.append(trail);
  return board;
}

const rolesHost = document.querySelector("#roles");
const errorHost = document.querySelector("#error");
function reportError(value) {
  errorHost.textContent = value instanceof Error ? value.message : String(value);
  errorHost.hidden = false;
}

function captureView() {
  const values = new Map();
  let focused = null;
  rolesHost.querySelectorAll("textarea[data-draft-key]").forEach((input) => {
    const key = input.dataset.draftKey;
    values.set(key, input.value);
    if (input === document.activeElement) {
      focused = {
        key,
        start: input.selectionStart,
        end: input.selectionEnd,
        direction: input.selectionDirection,
      };
    }
  });
  rolesHost.querySelectorAll("[data-focus-key]").forEach((node) => {
    if (node === document.activeElement) focused = { key: node.dataset.focusKey };
  });
  let selection = null;
  const browserSelection = window.getSelection?.();
  if (browserSelection?.rangeCount) {
    rolesHost.querySelectorAll("[data-selection-key]").forEach((node) => {
      if (node.contains(browserSelection.anchorNode) && node.contains(browserSelection.focusNode)) {
        selection = {
          key: node.dataset.selectionKey,
          anchor: browserSelection.anchorOffset,
          focus: browserSelection.focusOffset,
        };
      }
    });
  }
  const innerScroll = new Map();
  rolesHost.querySelectorAll("[data-scroll-key]").forEach((node) => {
    innerScroll.set(node.dataset.scrollKey, node.scrollTop);
  });
  return { values, focused, selection, innerScroll, scrollX: window.scrollX ?? 0, scrollY: window.scrollY ?? 0 };
}

function restoreView(view) {
  rolesHost.querySelectorAll("textarea[data-draft-key]").forEach((input) => {
    const key = input.dataset.draftKey;
    if (view.values.has(key)) input.value = view.values.get(key);
    if (view.focused?.key === key) {
      input.focus({ preventScroll: true });
      input.setSelectionRange(view.focused.start, view.focused.end, view.focused.direction);
    }
  });
  rolesHost.querySelectorAll("[data-focus-key]").forEach((node) => {
    if (view.focused?.key === node.dataset.focusKey) node.focus({ preventScroll: true });
  });
  rolesHost.querySelectorAll("[data-selection-key]").forEach((node) => {
    if (view.selection?.key === node.dataset.selectionKey && node.firstChild) {
      const limit = node.firstChild.textContent.length;
      window.getSelection()?.setBaseAndExtent(
        node.firstChild,
        Math.min(view.selection.anchor, limit),
        node.firstChild,
        Math.min(view.selection.focus, limit),
      );
    }
  });
  rolesHost.querySelectorAll("[data-scroll-key]").forEach((node) => {
    if (view.innerScroll.has(node.dataset.scrollKey)) node.scrollTop = view.innerScroll.get(node.dataset.scrollKey);
  });
  window.scrollTo?.(view.scrollX, view.scrollY);
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const state = await request("/api/workbench");
    errorHost.hidden = true;
    const view = captureView();
    if (state.mode === "native") {
      rolesHost.className = "role-grid role-grid--native";
      rolesHost.replaceChildren(flightBoard(state));
    } else {
      rolesHost.className = "role-grid";
      rolesHost.replaceChildren(...Object.values(state.roles).map((role) => roleCard(state, role, refresh, reportError)));
    }
    restoreView(view);
  } catch (error) { reportError(error); }
  finally { refreshing = false; }
}

refresh();
const timer = window.setInterval(refresh, 1000);
window.addEventListener("pagehide", () => window.clearInterval(timer), { once: true });
