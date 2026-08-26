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

const rolesHost = document.querySelector("#roles");
const errorHost = document.querySelector("#error");
function reportError(value) {
  errorHost.textContent = value instanceof Error ? value.message : String(value);
  errorHost.hidden = false;
}
async function refresh() {
  try {
    const state = await request("/api/workbench");
    errorHost.hidden = true;
    rolesHost.replaceChildren(...Object.values(state.roles).map((role) => roleCard(state, role, refresh, reportError)));
  } catch (error) { reportError(error); }
}

refresh();
const timer = window.setInterval(refresh, 1000);
window.addEventListener("pagehide", () => window.clearInterval(timer), { once: true });
