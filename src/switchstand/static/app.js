"use strict";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

async function request(path, body, controlValue = null) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json",
      ...(controlValue ? { "X-Switchstand-Control": controlValue } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error ?? `Request failed (${response.status})`);
  return value;
}

const stopState = new Map();
const stopEpoch = new Map();
const observedStatus = new Map();
async function stopAgent(agent) {
  const epoch = stopEpoch.get(agent.agentRef) ?? 0;
  try {
    const prepared = await request(
      "/api/native-stop/prepare",
      { agentRef: agent.agentRef },
      "native-stop-v1",
    );
    if ((stopEpoch.get(agent.agentRef) ?? 0) !== epoch) return;
    const warning = [
      `Stop ${agent.label}’s current turn?`,
      "Switchstand will request cancellation of that exact turn only.",
      "Work already performed is not undone.",
      "Background processes and descendant agents may continue.",
    ].join(" ");
    if (prepared.code !== "prepared") {
      stopState.set(agent.agentRef, prepared);
    } else if (!window.confirm(warning)) {
      stopState.set(agent.agentRef, { outcome: "not_sent" });
    } else {
      const result = await request(
        "/api/native-stop/commit",
        { confirmationRef: prepared.confirmationRef },
        "native-stop-v1",
      );
      if ((stopEpoch.get(agent.agentRef) ?? 0) !== epoch) return;
      stopState.set(agent.agentRef, result);
    }
    if (lastModel) renderNative(lastModel);
  } catch (_error) {
    if ((stopEpoch.get(agent.agentRef) ?? 0) !== epoch) return;
    stopState.set(agent.agentRef, { outcome: "unknown" });
    if (lastModel) renderNative(lastModel);
  }
}

async function checkStop(agentRef) {
  const current = stopState.get(agentRef);
  const epoch = stopEpoch.get(agentRef) ?? 0;
  try {
    const result = await request(
      "/api/native-stop/status",
      { operationRef: current.operationRef },
      "native-stop-v1",
    );
    if ((stopEpoch.get(agentRef) ?? 0) !== epoch) return;
    stopState.set(agentRef, result);
  } catch (_error) {
    if ((stopEpoch.get(agentRef) ?? 0) !== epoch) return;
    stopState.set(agentRef, { ...current, outcome: "unknown" });
  }
  if (lastModel) renderNative(lastModel);
}

const shown = (value) => value === null || value === undefined ? "not observed"
  : typeof value === "object" ? JSON.stringify(value) : String(value);

function factList(entries) {
  const list = el("dl", "facts-list");
  entries.forEach(([term, value]) => {
    const item = el("div");
    item.append(el("dt", null, term), el("dd", null, shown(value)));
    list.append(item);
  });
  return list;
}

function badge(value, label = "Native status") {
  const node = el("span", `status status--${value}`, value);
  node.setAttribute("aria-label", `${label}: ${value}`);
  return node;
}

function textPoint(root, target) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  let remaining = target;
  while ((node = walker.nextNode())) {
    if (remaining <= node.data.length) return [node, remaining];
    remaining -= node.data.length;
  }
  return [root, root.childNodes.length];
}

function textOffset(root, node, offset) {
  const range = document.createRange();
  range.selectNodeContents(root);
  range.setEnd(node, offset);
  return range.toString().length;
}

function captureView() {
  const focus = document.activeElement?.closest?.("[data-focus-key]");
  const selection = window.getSelection?.();
  const nodes = [...treeHost.querySelectorAll("details[data-node-key]")];
  const textSelection = focus && selection?.rangeCount && focus.contains(selection.anchorNode)
    && focus.contains(selection.focusNode) ? [textOffset(focus, selection.anchorNode, selection.anchorOffset),
      textOffset(focus, selection.focusNode, selection.focusOffset)] : null;
  const inputSelection = focus && typeof focus.selectionStart === "number"
    ? [focus.selectionStart, focus.selectionEnd, focus.selectionDirection] : null;
  return {
    focus: focus?.dataset.focusKey,
    inputSelection,
    textSelection,
    hadTree: nodes.length > 0,
    open: new Set(nodes.filter((node) => node.open).map((node) => node.dataset.nodeKey)),
    treeScroll: treeHost.scrollTop,
    windowScroll: [window.scrollX, window.scrollY],
  };
}

function restoreView(view) {
  treeHost.querySelectorAll("details[data-node-key]").forEach((node) => {
    node.open = view.hadTree ? view.open.has(node.dataset.nodeKey) : true;
  });
  const focus = [...nativeSurface.querySelectorAll("[data-focus-key]")]
    .find((node) => node.dataset.focusKey === view.focus);
  if (focus) {
    focus.focus({ preventScroll: true });
    if (view.inputSelection) {
      focus.setSelectionRange(...view.inputSelection);
    } else if (view.textSelection) {
      const selection = window.getSelection();
      selection.setBaseAndExtent(...textPoint(focus, view.textSelection[0]),
        ...textPoint(focus, view.textSelection[1]));
    }
  }
  treeHost.scrollTop = view.treeScroll;
  window.scrollTo(...view.windowScroll);
}

function renderNative(model) {
  nativeSurface.hidden = false;
  rolesHost.hidden = true;
  eyebrowHost.textContent = "Native agent observation";
  descriptionHost.textContent = "Observed evidence with exact-turn emergency stop.";
  headlineFactsHost.textContent = "Observed state only · exact cancellation requests";
  const view = captureView();
  const observation = model.observation;
  const title = observation.historical ? "Historical snapshot" : "Current observation";
  observerHost.replaceChildren(el("strong", null, title), badge(observation.connected ? "connected" : "disconnected", "Observer"),
    factList([["available", observation.available], ["pass", observation.kind], ["completed", observation.completedAt],
      ["age", observation.passAgeSeconds === null ? null : `${observation.passAgeSeconds}s`], ["error", observation.errorCode]]));
  const labels = new Map(model.agents.map((agent) => [agent.agentRef, agent.label]));
  const selectionState = nativeSelectionController?.getState();
  const agents = model.agents.map((agent) => {
    const previousStatus = observedStatus.get(agent.agentRef);
    if (previousStatus === "active" && agent.status !== "active") {
      stopEpoch.set(agent.agentRef, (stopEpoch.get(agent.agentRef) ?? 0) + 1);
      stopState.delete(agent.agentRef);
    }
    observedStatus.set(agent.agentRef, agent.status);
    const row = el("details", "agent");
    row.dataset.nodeKey = agent.agentRef;
    row.dataset.focusKey = `agent:${agent.agentRef}`;
    row.tabIndex = 0;
    row.style.setProperty("--depth", agent.depth);
    const summary = el("summary", "agent__summary");
    summary.append(el("strong", null, agent.label), badge(agent.status));
    row.append(summary, factList([["parent", agent.parentRef === null ? "none" : labels.get(agent.parentRef) ?? "unavailable"], ["depth", agent.depth],
      ["flags", agent.activeFlags], ["source", `${agent.sourceKind}${agent.sourceDetail ? ` · ${agent.sourceDetail}` : ""}`],
      ["created", agent.createdAt], ["updated", agent.updatedAt], ["updated age", `${agent.updatedAgeSeconds}s`],
      ["consecutive observed active", agent.activeObservedSeconds === null ? null : `${agent.activeObservedSeconds}s`]]));
    const selected = selectionState?.currentTarget?.agentRef === agent.agentRef;
    const select = el("button", selected ? "button button--primary" : "button",
      selected ? "Current target" : "Select as current target");
    select.type = "button";
    select.setAttribute("aria-pressed", selected ? "true" : "false");
    select.dataset.focusKey = `select:${agent.agentRef}`;
    select.addEventListener("click", () => selectAgent(agent.agentRef));
    row.append(select);
    const stop = stopState.get(agent.agentRef);
    if (stop) row.append(el("p", "muted", `Stop outcome: ${stop.outcome}`));
    if (agent.status === "active" && !["requested", "confirmed"].includes(stop?.outcome)) {
      const button = el("button", "button button--danger", "Stop current turn");
      button.type = "button";
      button.addEventListener("click", () => stopAgent(agent));
      row.append(button);
    }
    if (["requested", "unknown"].includes(stop?.outcome) && stop.operationRef) {
      const check = el("button", "button", "Check stop outcome");
      check.type = "button";
      check.addEventListener("click", () => checkStop(agent.agentRef));
      row.append(check);
    }
    return row;
  });
  const tree = el("div", "tree");
  tree.append(...(agents.length ? agents : [el("p", "muted", "No complete agent-tree observation is available.")]));
  treeHost.replaceChildren(tree);
  const trail = model.trail.slice(-model.trailLimit).reverse().map((entry) => {
    const row = el("li", "record");
    row.append(el("strong", null, labels.get(entry.agentRef) ?? "Unknown observed agent"),
      el("div", "muted", entry.observedAt));
    Object.entries(entry.changes).forEach(([field, change]) => {
      const value = (item) => field === "parentRef" ? item === null ? "none" : labels.get(item) ?? "unavailable" : shown(item);
      row.append(factList([[field, `${value(change.from)} → ${value(change.to)}`]]));
    });
    return row;
  });
  trailHost.replaceChildren(...(trail.length ? trail : [el("li", "muted", "No endpoint differences observed in the retained window.")]));
  disclosureHost.textContent = model.disclosure;
  renderCurrentTarget(selectionState);
  restoreView(view);
}

function renderCurrentTarget(state) {
  if (!currentTargetHost || !currentTargetSummaryHost || !nativeInputForm) return;
  if (!nativeSelectionController) {
    currentTargetHost.hidden = true;
    return;
  }
  currentTargetHost.hidden = false;
  const target = state?.currentTarget;
  if (!target) {
    currentTargetSummaryHost.textContent = "No current target selected.";
    nativeInputForm.hidden = true;
    return;
  }
  const identity = [target.agentNickname, target.name].filter((value) => typeof value === "string");
  currentTargetSummaryHost.textContent = identity.length
    ? `Current target: ${identity.join(" · ")}` : "Current target selected.";
  nativeInputForm.hidden = false;
  syncNativeInput(target);
}

const attemptFor = (state, id) => state.attempts.find((attempt) => attempt.id === id) ?? null;

function messageRow(message) {
  const row = el("li", "record");
  const identity = el("div", "record__summary");
  identity.append(el("strong", null, `${message.sequence}. ${message.kind}`), badge(message.status, "State"));
  row.append(identity, el("p", null, message.text));
  if (message.result) row.append(el("pre", "output", message.result));
  return row;
}

function attemptRow(attempt, currentId) {
  const row = el("li", "record");
  const summary = el("div", "record__summary");
  summary.append(el("code", null, attempt.id), badge(attempt.status, "State"),
    el("span", "muted", `generation ${attempt.generation}${attempt.id === currentId ? " · current" : ""}`));
  row.append(summary);
  if (attempt.thread_id) row.append(el("div", "muted", `thread ${attempt.thread_id}`));
  if (attempt.turn_id) row.append(el("div", "muted", `turn ${attempt.turn_id}`));
  if (attempt.error) row.append(el("p", "error", attempt.error));
  if (attempt.stale_output) row.append(el("strong", "stale-label", "Stale output — visible, not accepted"),
    el("pre", "output output--stale", attempt.stale_output));
  return row;
}

function roleCard(state, role) {
  const card = el("article", "role-card");
  const heading = el("header", "role-card__header");
  const title = el("div");
  title.append(el("h2", null, role.name), el("div", "muted", `${role.id} · generation ${role.generation}`));
  heading.append(title, badge(role.status, "State"));
  card.append(heading);
  const checkpoint = el("section", "inset");
  checkpoint.append(el("h3", null, "Accepted checkpoint"),
    el("p", null, role.checkpoint.latest_correction ?? "No accepted correction yet."));
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
    try { await request(`/api/workbench/roles/${role.id}/messages`, { text: input.value, kind: "message" }); input.value = ""; await refresh(); }
    catch (error) { reportError(error); }
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
    const addAction = (label, style, path, body = () => ({})) => {
      const button = el("button", style, label);
      button.type = "button";
      button.addEventListener("click", async () => {
        try { await request(path, body()); await refresh(); } catch (error) { reportError(error); }
      });
      actions.append(button);
    };
    if (["running", "waiting"].includes(current.status)) {
      addAction("Redirect", "button", `/api/workbench/attempts/${current.id}/redirect`, () => ({ text: correction.value }));
      addAction("Stop", "button button--danger", `/api/workbench/attempts/${current.id}/stop`);
    }
    if (["stopped", "stale", "failed", "unknown"].includes(current.status)) {
      addAction("Replace from checkpoint", "button button--primary", `/api/workbench/attempts/${current.id}/replace`);
    }
    controls.append(correction, actions);
    card.append(controls);
  }
  const records = (headingText, items) => {
    const section = el("section");
    section.append(el("h3", null, headingText));
    const list = el("ul", "list");
    list.append(...(items.length ? items : [el("li", "muted", "No records yet.")]));
    section.append(list);
    return section;
  };
  card.append(records("Durable messages", state.messages.filter((item) => item.role_id === role.id).map(messageRow)),
    records("Attempt identity and history", state.attempts.filter((item) => item.role_id === role.id).reverse()
      .map((item) => attemptRow(item, role.current_attempt_id))));
  return card;
}

function captureDrafts() {
  const values = new Map();
  let focused = null;
  rolesHost.querySelectorAll("textarea[data-draft-key]").forEach((input) => {
    values.set(input.dataset.draftKey, input.value);
    if (input === document.activeElement) focused = { key: input.dataset.draftKey, start: input.selectionStart,
      end: input.selectionEnd, direction: input.selectionDirection };
  });
  return { values, focused };
}

function renderLegacy(model) {
  const drafts = captureDrafts();
  nativeSurface.hidden = true;
  rolesHost.hidden = false;
  eyebrowHost.textContent = "Experimental local prototype";
  descriptionHost.textContent = "Direct conversation with two durable roles, with exact attempt identity and truthful state.";
  headlineFactsHost.textContent = "One Work · two roles · flat JSON/JSONL · Codex app-server";
  rolesHost.replaceChildren(...Object.values(model.roles).map((role) => roleCard(model, role)));
  rolesHost.querySelectorAll("textarea[data-draft-key]").forEach((input) => {
    const key = input.dataset.draftKey;
    if (drafts.values.has(key)) input.value = drafts.values.get(key);
    if (drafts.focused?.key === key) {
      input.focus({ preventScroll: true });
      input.setSelectionRange(drafts.focused.start, drafts.focused.end, drafts.focused.direction);
    }
  });
}

const treeHost = document.querySelector("#tree");
const trailHost = document.querySelector("#trail");
const observerHost = document.querySelector("#observer");
const disclosureHost = document.querySelector("#disclosure");
const errorHost = document.querySelector("#error");
const rolesHost = document.querySelector("#roles");
const nativeSurface = document.querySelector("#native-surface");
const eyebrowHost = document.querySelector("#eyebrow");
const descriptionHost = document.querySelector("#description");
const headlineFactsHost = document.querySelector("#headline-facts");
const currentTargetHost = document.querySelector("#current-target");
const currentTargetSummaryHost = document.querySelector("#current-target-summary");
const nativeInputForm = document.querySelector("#native-input-form");
const nativeInput = document.querySelector("#native-input");
const nativeInputSubmit = document.querySelector("#native-input-submit");
const nativeInputOutcomeHost = document.querySelector("#native-input-outcome");
const nativeSelectionController = window.SwitchstandNativeSelection
  ? window.SwitchstandNativeSelection.createController({
    resolve: (_pair, snapshot) => snapshot,
    storage: window.localStorage,
    onChange: () => { if (lastModel?.mode === "native") renderNative(lastModel); },
  }) : null;
const nativeInputDrafts = new Map();
const nativeInputOutcomes = new Map();
const nativeInputRequests = new Map();
let visibleInputTargetKey = null;
let nextInputRequest = 0;
let selectionEpoch = 0;
let refreshEpoch = 0;

function pairKey(pair) {
  return pair ? `${pair.observationRunRef}\u0000${pair.agentRef}` : null;
}

function samePair(left, right) {
  return left?.observationRunRef === right?.observationRunRef
    && left?.agentRef === right?.agentRef;
}

function syncNativeInput(target) {
  const key = pairKey(target);
  if (visibleInputTargetKey !== key) {
    if (visibleInputTargetKey) nativeInputDrafts.set(visibleInputTargetKey, nativeInput.value);
    visibleInputTargetKey = key;
    nativeInput.value = nativeInputDrafts.get(key) ?? "";
  }
  nativeInputOutcomeHost.textContent = nativeInputOutcomes.get(key) ?? "";
  nativeInputSubmit.disabled = nativeInputRequests.has(key);
}

async function selectAgent(agentRef) {
  const epoch = ++selectionEpoch;
  nativeSelectionController?.clear();
  try {
    const seam = await request(
      "/api/native-selection/resolve",
      { agentRef },
      "native-selection-v1",
    );
    if (epoch !== selectionEpoch) return;
    nativeSelectionController?.select(seam);
  } catch (_error) {
    if (epoch === selectionEpoch) nativeSelectionController?.clear();
  }
}

async function revalidateSelection() {
  const candidate = nativeSelectionController?.getState().candidate;
  if (!candidate) return;
  const epoch = ++selectionEpoch;
  try {
    const seam = await request(
      "/api/native-selection/resolve",
      { agentRef: candidate.agentRef },
      "native-selection-v1",
    );
    if (epoch !== selectionEpoch
      || !samePair(candidate, nativeSelectionController?.getState().candidate)) return;
    nativeSelectionController.supplySeam(seam);
  } catch (_error) {
    if (epoch === selectionEpoch) {
      nativeSelectionController?.clear();
    }
  }
}

nativeInput?.addEventListener("input", () => {
  if (visibleInputTargetKey) nativeInputDrafts.set(visibleInputTargetKey, nativeInput.value);
});
nativeInputForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const target = nativeSelectionController?.getState().currentTarget;
  if (!target) return;
  const key = pairKey(target);
  if (nativeInputRequests.has(key)) return;
  const text = nativeInput.value;
  nativeInputDrafts.set(key, text);
  const requestRef = ++nextInputRequest;
  nativeInputRequests.set(key, requestRef);
  nativeInputSubmit.disabled = true;
  let result = null;
  try {
    result = await request("/api/native-input", {
      version: "native-input-v1",
      observationRunRef: target.observationRunRef,
      agentRef: target.agentRef,
      text,
    }, "native-input-v1");
  } catch (_error) {
    result = null;
  }
  if (nativeInputRequests.get(key) !== requestRef) return;
  nativeInputRequests.delete(key);
  const sent = result?.code === "input_sent" && result.outcome === "sent"
    && ["start", "steer"].includes(result.mode);
  const outcome = sent ? `sent · ${result.mode}` : "not sent";
  nativeInputOutcomes.set(key, outcome);
  if (sent) nativeInputDrafts.set(key, "");
  const candidate = nativeSelectionController?.getState().candidate;
  if (samePair(candidate, target)) {
    if (sent) nativeInput.value = "";
    nativeInputOutcomeHost.textContent = outcome;
  }
  const current = nativeSelectionController?.getState().currentTarget;
  if (current) syncNativeInput(current);
});

function reportError(value) {
  errorHost.textContent = value instanceof Error ? value.message : String(value);
  errorHost.hidden = false;
}
let lastModel = null;
async function refresh() {
  const epoch = ++refreshEpoch;
  try {
    const model = await request("/api/workbench");
    if (epoch !== refreshEpoch) return;
    lastModel = model;
    errorHost.hidden = true;
    if (lastModel.mode === "native") {
      renderNative(lastModel);
      void revalidateSelection();
    } else renderLegacy(lastModel);
  } catch (_error) {
    if (epoch !== refreshEpoch) return;
    selectionEpoch += 1;
    nativeSelectionController?.clear();
    errorHost.textContent = lastModel?.mode === "native"
      ? "Observation request failed. The displayed board is a historical snapshot."
      : "Workbench request failed. Existing input has been retained.";
    errorHost.hidden = false;
    if (lastModel?.mode === "native") renderNative({ ...lastModel, observation: { ...lastModel.observation, connected: false, available: false,
      historical: true, errorCode: "board_request_failed", passAgeSeconds: null } });
  }
}

refresh();
const timer = window.setInterval(refresh, 1000);
window.addEventListener("pagehide", () => window.clearInterval(timer), { once: true });
