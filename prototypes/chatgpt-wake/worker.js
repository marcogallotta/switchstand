// Observe delivery IDs only; mailbox content remains in Switchstand.
function validConversation(value) {
  try {
    const url = new URL(value);
    return url.origin === 'https://chatgpt.com' && !url.username && !url.password &&
      /^\/(?:g\/[a-zA-Z0-9-]+\/)?c\/[a-zA-Z0-9-]+\/?$/.test(url.pathname);
  } catch { return false; }
}
let serial = Promise.resolve();
let stopAll = 0;
let stopVersion = 0;
const stopEndpoints = new Map();
const stopRequested = binding => stopAll || stopEndpoints.has(binding.endpoint);
function exclusive(action) {
  const result = serial.then(action);
  serial = result.catch(() => {});
  return result;
}
async function status(value) {
  await chrome.storage.local.set({status: value});
  return value;
}
// Isolated extension world: check exact identity immediately before editing.
async function nudge(expectedUrl, observeOnly = false, attempt, diagnostic = false) {
  const facts = {};
  const selectControl = form => {
      const explicit = [...(form?.querySelectorAll('[data-testid="send-button"], [data-testid="composer-submit-button"], #composer-submit-button, button[aria-label="Send prompt"], button[aria-label="Send" i]') || [])];
      const controls = explicit.filter(control => {
          const rect = control.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && getComputedStyle(control).visibility === 'visible';
        });
      facts.controlSelection = !form ? 'missing-form' : !explicit.length ? 'no-explicit'
        : !controls.length ? 'all-hidden' : controls.length > 1 ? 'ambiguous' : 'unique';
      // Count before checking enabled state: two visible Send controls are ambiguous.
      return controls.length === 1 ? controls[0] : undefined;
    };
  async function run() {
    facts.urlMatched = location.href === expectedUrl;
    if (!facts.urlMatched) return 'wrong-conversation';
    let editor = document.getElementById('prompt-textarea');
    if (!editor) {
      const visible = element => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth;
      };
      const textboxes = [...document.querySelectorAll('form [role="textbox"]')]
        .filter(element => element.isContentEditable && visible(element));
      const roots = textboxes.filter(element => !textboxes.some(other => other !== element && other.contains(element)));
      if (roots.length === 1) editor = roots[0];
      else if (roots.length > 1) return `composer-unavailable: ${roots.length} editable form textboxes; refusing ambiguous target`;
    }
    if (!editor) {
      facts.editorFound = false;
      const candidates = [...document.querySelectorAll('textarea, [contenteditable]')]
        .filter(element => element.getClientRects().length).slice(0, 5)
        .map(element => `${element.tagName}#${element.id}[role=${element.getAttribute('role') || ''},editable=${element.isContentEditable},form=${Boolean(element.closest('form'))}]`);
      return `composer-unavailable: missing #prompt-textarea; visible editors: ${candidates.join(', ') || 'none'}`;
    }
    facts.editorFound = true;
    const textarea = editor instanceof HTMLTextAreaElement;
    if (!textarea && !editor.isContentEditable) return `composer-unavailable: ${editor.tagName} is not editable`;
    if (!editor.getClientRects().length) return 'composer-unavailable: #prompt-textarea is hidden';
    if (textarea && (editor.disabled || editor.readOnly)) return 'composer-unavailable: textarea is disabled or read-only';
    if (observeOnly === 'control') { selectControl(editor.closest('form')); return 'idle'; }
    facts.busy = Boolean(document.querySelector('[data-testid="stop-button"], button[aria-label="Stop streaming"]'));
    if (facts.busy) return 'busy';
    const draft = () => textarea ? editor.value : editor.textContent;
    const text = 'Process your mailbox';
    const attached = () => editor.querySelector('img') || document.querySelector('[data-testid="remove-file-button"]');
    facts.draftPresent = Boolean(draft().trim() || attached());
    if (!attempt?.retry && facts.draftPresent) return 'draft-present';
    if (observeOnly) return 'idle';
    const form = editor.closest('form');
    const sendControl = () => selectControl(form);
    const enabled = send => send && !send.disabled && !send.matches(':disabled') &&
      send.getAttribute('aria-disabled')?.toLowerCase() !== 'true' &&
      !/voice|dictat/i.test(send.getAttribute('aria-label') || '');
    const owned = witness => witness && witness.token === attempt?.token && witness.editor === editor &&
      witness.url === expectedUrl && witness.valid && editor.isConnected && location.href === expectedUrl &&
      draft() === text && !attached() && !witness.observer.takeRecords().length;
    if (attempt?.retry) {
      const witness = globalThis.switchstandWakeWitness;
      const send = sendControl();
      facts.controlPresent = Boolean(send);
      facts.controlEnabled = Boolean(enabled(send));
      const canClick = owned(witness) && enabled(send);
      witness?.retire();
      if (!canClick) return 'attempt-outcome-unknown: retry exhausted; ownership or send control unavailable';
      facts.clickDispatched = true;
      send.click();
      return 'click-attempted';
    }
    editor.focus();
    if (textarea) {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, text);
      editor.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
    } else if (!document.execCommand('insertText', false, text)) return 'insert-failed';
    facts.inserted = draft() === text;
    let witness;
    if (attempt?.token) {
      globalThis.switchstandWakeWitness?.retire();
      const events = ['input', 'beforeinput', 'paste', 'change', 'keydown', 'click', 'submit'];
      const invalidate = event => witness.retire(!event?.type ? 'editor-mutation'
        : ['pagehide', 'popstate'].includes(event.type) ? 'navigation' : 'form-event');
      witness = {token: attempt.token, editor, url: expectedUrl, valid: true,
        observer: new MutationObserver(invalidate), retire(cause = 'none-unknown') {
          if (this.valid) this.invalidator = cause;
          this.valid = false; this.observer.disconnect();
          for (const event of events) (form || editor).removeEventListener(event, invalidate, true);
          window.removeEventListener('pagehide', invalidate);
          window.removeEventListener('popstate', invalidate);
        }};
      witness.observer.observe(editor, {childList: true, characterData: true, subtree: true});
      for (const event of events) (form || editor).addEventListener(event, invalidate, true);
      window.addEventListener('pagehide', invalidate);
      window.addEventListener('popstate', invalidate);
      globalThis.switchstandWakeWitness = witness;
    }
    let send;
    for (let attempt = 0; attempt < 15; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 100));
      send = sendControl();
      if (enabled(send)) break;
    }
    send = sendControl(); // Recheck uniqueness and state after the render wait.
    facts.controlPresent = Boolean(send);
    facts.controlEnabled = Boolean(enabled(send));
    if (location.href !== expectedUrl || draft().trim() !== text ||
        !enabled(send) ||
        document.querySelector('[data-testid="stop-button"]')) {
      const controls = [...(form?.querySelectorAll('button') || [])].slice(0, 5)
        .map(button => `id=${button.id},testid=${button.getAttribute('data-testid') || ''},type=${button.type},label=${button.getAttribute('aria-label') || ''},disabled=${button.disabled}`);
      const outcome = `inserted-not-submitted: no click; editor=${editor.tagName}[role=${editor.getAttribute('role') || ''}]; form=${Boolean(form)}; controls: ${controls.join('; ') || 'none'}`;
      if (owned(witness)) return {outcome, noClickToken: attempt.token};
      witness?.retire();
      return outcome;
    }
    // User actions during the wait for a send control revoke ownership permanently.
    if (attempt && !owned(witness)) {
      facts.ownershipInvalidator = witness?.invalidator || 'none-unknown';
      witness?.retire(); return 'attempt-outcome-unknown: ownership changed';
    }
    witness?.retire();
    facts.clickDispatched = true;
    send.click();
    return 'click-attempted'; // A click is not a provider acceptance receipt.
  }
  try {
    const receipt = await run();
    if (!diagnostic) return receipt;
    return typeof receipt === 'string' ? {outcome: receipt, facts} : {...receipt, facts};
  } catch (error) {
    if (!diagnostic) throw error;
    return {outcome: 'attempt-outcome-unknown: ' + error.message, facts, reason: 'dom-exception',
      exception: ['Error', 'TypeError', 'ReferenceError', 'DOMException'].includes(error.name) ? error.name : 'Other'};
  }
}
const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let reportToken;
const bootId = crypto.randomUUID();
// Release ID is mapped to exact artifact hashes by the immutable activation proof.
const releaseId = 'd4f1f4bf-c720-4e4e-a5a4-b67c3a60d03e';
async function diagnosticReport(binding, save) {
  const diagnostics = {release: releaseId, boot: bootId, legacy_halt: Boolean(binding.halted && !binding.diagnostic),
    attempt: binding.diagnostic || null, pair: binding.pairDiagnostic || null, current_control: binding.currentControl || null,
    block: binding.status === 'attempt-outcome-unknown: retry evidence no longer current'
      ? 'retry-evidence-not-current' : null};
  if (!diagnostics.attempt && !diagnostics.pair && !diagnostics.legacy_halt) return;
  const identity = JSON.stringify(diagnostics);
  if (binding.diagnosticAck?.identity === identity) return;
  diagnostics.report_failure = binding.diagnosticAck?.failure || null;
  try {
    if (!reportToken) reportToken = (await source('/config')).report_token;
    const response = await fetch(SOURCE + '/report', {method: 'POST',
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + reportToken},
      body: JSON.stringify({name: binding.name, delivery_ids: [],
        outcome: 'attempt-outcome-unknown', diagnostics}), signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error('http');
    if ((await response.json()).status !== 'logged') throw new Error('receipt');
    // Ack the exact snapshot, never a newer receipt produced while awaiting the host.
    binding.diagnosticAck = {identity, failure: null};
    await save();
  } catch {
    binding.diagnosticAck = {identity: binding.diagnosticAck?.identity || null, failure: 'report-unavailable'};
    try { await save(); } catch { /* No diagnostic failure changes wake decisions. */ }
  }
}
function attemptResult(binding, outcome, receipt, reason, error) {
  Object.assign(binding.diagnostic, {finished: new Date().toISOString(),
    outcome: outcome.split(':')[0], reason: reason || receipt?.reason || (outcome === 'attempt-outcome-unknown: ownership changed' ? 'ownership-changed' : 'result'),
    facts: receipt?.facts || {}, exception: error
      ? (['Error', 'TypeError', 'ReferenceError', 'DOMException'].includes(error.name) ? error.name : 'Other')
      : receipt?.exception || null});
}

async function source(path) {
  const response = await fetch(SOURCE + path, {signal: AbortSignal.timeout(3000), cache: 'no-store'});
  if (!response.ok) throw new Error(`backend HTTP ${response.status}`);
  const value = await response.json();
  if (value.status !== 'ok') throw new Error('backend observation unavailable');
  return value;
}
async function mailbox(name) {
  const value = await source('/poll?name=' + encodeURIComponent(name));
  if (typeof value.name !== 'string' || !UUID.test(value.endpoint) ||
      !Number.isInteger(value.generation) || value.generation < 1 ||
      !Array.isArray(value.delivery_ids) || value.delivery_ids.some(id => typeof id !== 'string' || !UUID.test(id))) {
    throw new Error('invalid mailbox metadata');
  }
  return value;
}
async function incidentReport(binding, kind, ids, save, fresh) {
  const now = Date.now();
  const blocked = ids.length && ['draft-present', 'bound-tab-missing'].includes(kind);
  if (blocked) {
    if (binding.blockBoot !== bootId || binding.blockReason !== kind || now - (binding.blockChecked || 0) > 60000) binding.blockSince = now;
    binding.blockBoot = bootId;
    binding.blockReason = kind;
    binding.blockChecked = now;
    if (!binding.incident && (kind === 'bound-tab-missing' || now - binding.blockSince >= 60000)) {
      const evidence = {incident_id: crypto.randomUUID(), phase: 'blocked', name: binding.name,
        endpoint: binding.endpoint, generation: binding.generation, delivery_ids: ids.slice(0, 50),
        reason: kind, started_at: new Date(binding.blockSince).toISOString(), observed_at: new Date(now).toISOString()};
      binding.incident = {evidence, state: 'pending'};
    }
  } else {
    delete binding.blockSince; delete binding.blockReason; delete binding.blockChecked;
    // UNKNOWN, explicit STOP and observation errors are not evidence of recovery.
    const clear = ['idle', 'click-attempted', 'busy'].includes(kind) || (fresh && kind === 'bound-tab-missing' && !ids.length);
    if (binding.incident?.state === 'stale' && clear) delete binding.incident;
    if (binding.incident?.state === 'queued' && clear) {
      binding.incident.state = 'recovery-pending';
    }
  }
  const incident = binding.incident;
  if (!incident || !['pending', 'recovery-pending'].includes(incident.state) || kind === 'stopped') return;
  const phase = incident.state === 'pending' ? 'blocked' : 'recovered';
  // Reserve immutable arguments in persistent binding state BEFORE any possible mail effect.
  await save();
  if (stopRequested(binding)) return;
  try {
    if (!reportToken) reportToken = (await source('/config')).report_token;
    const response = await fetch(SOURCE + '/report', {method: 'POST',
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + reportToken},
      body: JSON.stringify({name: binding.name, delivery_ids: ids.slice(0, 50), outcome: kind,
        incident: {...incident.evidence, phase}}), signal: AbortSignal.timeout(3000)});
    if (!response.ok) { reportToken = undefined; throw new Error(`HTTP ${response.status}`); }
    const receipt = await response.json();
    if (receipt.alert?.status === 'queued') {
      binding.report = `${receipt.recipient || "Configured recipient"} ${phase} alert queued; delivery ${receipt.alert.delivery_id}`;
      if (phase === 'blocked') incident.state = 'queued';
      else { binding.lastIncident = incident.evidence.incident_id; delete binding.incident; }
    } else {
      binding.report = `alert ${receipt.alert?.status || receipt.escalation || 'unknown'}: ${receipt.alert?.reason || 'not queued'}`;
      // Definitive stale evidence is not ambiguous acceptance; do not recreate it under a new ID.
      if (receipt.alert?.status === 'stale') incident.state = 'stale';
      if (receipt.alert?.status === 'denied' && receipt.alert.reason === 'observation_expired') {
        // The host proved this exact operation uncommitted under its send lock.
        binding.lastRetiredIncident = incident.evidence.incident_id;
        delete binding.incident; delete binding.blockSince; delete binding.blockReason; delete binding.blockChecked;
      }
    }
  } catch (error) { binding.report = 'alert outcome unknown; same identity retained: ' + error.message; }
}
async function record(binding, outcome, ids = [], save, fresh = false) {
  binding.status = outcome;
  binding.checkedAt = new Date().toISOString();
  const kind = outcome.split(':')[0];
  const hadIncident = Boolean(binding.incident);
  if (save) await incidentReport(binding, kind, ids, save, fresh);
  if (hadIncident || binding.incident || ['idle', 'armed', 'click-attempted', 'stopped'].includes(kind)) return;
  const reportKey = JSON.stringify([outcome, ids.slice(0, 50)]);
  if (binding.lastReport === reportKey && binding.report === 'logged; escalation unconfigured') return;
  try {
    if (!reportToken) reportToken = (await source('/config')).report_token;
    const response = await fetch(SOURCE + '/report', {method: 'POST',
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + reportToken},
      body: JSON.stringify({name: binding.name, delivery_ids: ids.slice(0, 50), outcome: kind}),
      signal: AbortSignal.timeout(3000)});
    if (!response.ok) { reportToken = undefined; throw new Error(`HTTP ${response.status}`); }
    const receipt = await response.json();
    if (receipt.status !== 'logged') throw new Error('missing log receipt');
    binding.report = `logged; escalation ${receipt.escalation || 'unconfigured'}`;
    binding.lastReport = reportKey;
  } catch (error) { binding.report = 'backend report unavailable: ' + error.message; }
}
// The registered conversation is durable; tab IDs are only current delivery routes.
async function conversationTab(binding) {
  const tabs = (await chrome.tabs.query({url: 'https://chatgpt.com/*'}))
    .filter(tab => Number.isInteger(tab.id) && tab.url === binding.url);
  // Page-owned retries cannot move, even when a preferred/duplicate tab exists.
  return tabs.find(tab => tab.id === binding.retry?.tabId) ||
    tabs.find(tab => tab.id === binding.tabId) || tabs.sort((a, b) => a.id - b.id)[0];
}
async function checkBinding(binding, save) {
  if (binding.halted) binding.currentControl = {release: releaseId, boot: bootId, reason: 'unavailable'};
  let observation;
  const note = (outcome, ids = observation?.delivery_ids || []) => record(binding, outcome, ids, save, true);
  if (!binding.armed || stopRequested(binding)) { binding.armed = false; return note('stopped'); }
  try { observation = await mailbox(binding.name); }
  catch (error) { return note('source-unavailable: ' + error.message); }
  if (observation.endpoint !== binding.endpoint || observation.generation !== binding.generation) {
    binding.armed = false;
    return note('mailbox-binding-changed');
  }
  const pending = observation.delivery_ids.filter(id => !binding.seen.includes(id));
  binding.pending = pending;
  binding.observedPending = observation.delivery_ids;
  let tab;
  try { tab = await conversationTab(binding); }
  catch (error) { return note('composer-unavailable: tab lookup failed: ' + error.message); }
  if (!tab) {
    delete binding.retry; // No current page can retain this page-owned allowance.
    binding.availability = 'closed';
    return note('bound-tab-missing');
  }
  binding.availability = 'open';
  if (binding.halted && !binding.retry) {
    if (!tab.discarded && !tab.frozen) {
      try {
        const [result] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: nudge,
          args: [binding.url, 'control', null, true]});
        binding.currentControl.reason = !result?.result || result.result.reason === 'dom-exception' ? 'unavailable'
          : result.result.facts?.controlSelection || (result.result.outcome === 'wrong-conversation' ? 'url-mismatch' : 'editor-unavailable');
      } catch { /* Unavailable replaces the prior snapshot. */ }
    }
    return note(binding.status?.startsWith('attempt-outcome-unknown') ||
      binding.status?.startsWith('inserted-not-submitted') ? binding.status : 'attempt-outcome-unknown');
  }
  const retry = binding.retry;
  if (retry && (retry.boot !== bootId || retry.stop !== stopVersion ||
      retry.endpoint !== binding.endpoint || retry.generation !== binding.generation ||
      retry.tabId !== tab.id || retry.url !== binding.url || tab.url !== binding.url ||
      JSON.stringify(retry.ids) !== JSON.stringify(pending))) {
    delete binding.retry;
    return note('attempt-outcome-unknown: retry evidence no longer current');
  }
  if (!observation.delivery_ids.length) return note('idle');
  if (tab.url !== binding.url) return note('wrong-conversation');
  if (tab.discarded) return note('tab-discarded');
  if (tab.frozen) return note('tab-frozen');
  if (stopRequested(binding)) { binding.armed = false; return note('stopped'); }
  if (!pending.length) {
    try {
      const [result] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: nudge, args: [binding.url, true]});
      return note(result?.result || 'composer-unavailable: observation returned no result');
    } catch (error) { return note('composer-unavailable: observation failed: ' + error.message); }
  }
  // Persist a halt before DOM mutation. A crash cannot silently retry these IDs.
  binding.halted = true;
  binding.status = 'attempt-outcome-unknown';
  binding.attemptIds = pending;
  const attempt = retry ? {token: retry.token, retry: true} : {token: crypto.randomUUID()};
  const attemptStop = stopVersion;
  // Consume the sole retry durably before dispatch. Any crash thereafter stays halted.
  delete binding.retry;
  binding.diagnostic = {id: attempt.token, boot: bootId, release: releaseId,
    endpoint: binding.endpoint, generation: binding.generation, delivery_ids: pending.slice(0, 50),
    started: new Date().toISOString(), finished: null, outcome: null, reason: 'dispatch-start', facts: {}};
  await save();
  if (stopRequested(binding) || stopVersion !== attemptStop) {
    binding.armed = false; binding.halted = false; binding.attemptIds = [];
    attemptResult(binding, 'stopped', null, 'stopped-before-dispatch');
    return note('stopped');
  }
  try {
    const [result] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: nudge, args: [binding.url, false, attempt, true]});
    const receipt = result?.result;
    const outcome = (typeof receipt === 'string' ? receipt : receipt?.outcome) || 'attempt-outcome-unknown';
    attemptResult(binding, outcome, receipt, receipt ? undefined : 'missing-result');
    if (!retry && receipt?.noClickToken === attempt.token && outcome.startsWith('inserted-not-submitted:') &&
        !stopRequested(binding) && stopVersion === attemptStop) {
      binding.retry = {token: attempt.token, boot: bootId, stop: attemptStop, endpoint: binding.endpoint,
        generation: binding.generation, tabId: tab.id, url: binding.url, ids: pending.slice()};
    }
    const safe = ['busy', 'draft-present', 'wrong-conversation', 'insert-failed'].includes(outcome) || outcome.startsWith('composer-unavailable');
    if (safe && !retry) binding.halted = false;
    if (outcome === 'click-attempted') {
      binding.seen.push(...pending);
      binding.pending = [];
      binding.halted = false;
    }
    await note(outcome);
  } catch (error) {
    const outcome = 'attempt-outcome-unknown: ' + error.message;
    if (!binding.diagnostic.finished) attemptResult(binding, outcome, null, 'execution-failed', error);
    await note(outcome);
  }
}
async function check() {
  try {
    const config = await source('/config');
    reportToken = config.report_token;
    await chrome.storage.local.set({escalation: config.escalation, alertRecipient: config.recipient});
  } catch { await chrome.storage.local.set({escalation: 'unavailable'}); }
  const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
  const save = () => chrome.storage.local.set({wakeBindings});
  for (const binding of wakeBindings) {
    await checkBinding(binding, save);
    await save();
    await diagnosticReport(binding, save);
  }
  return status(wakeBindings.length ? 'checked' : 'no V1 pairings');
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return;
  const stopSnapshot = {all: stopAll, endpoint: stopEndpoints.get(message.endpoint)};
  // Stop intent preempts asynchronous checks before its persisted update is queued.
  if (message.type === 'disarm') {
    const version = ++stopVersion;
    if (message.endpoint) stopEndpoints.set(message.endpoint, version);
    else stopAll = version;
  }
  exclusive(async () => {
    if (message.type === 'verify') return mailbox(message.name);
    if (message.type === 'bind') {
      const {tabId, url, name, endpoint, generation} = message;
      const {wakeBindings: registered = []} = await chrome.storage.local.get('wakeBindings');
      const existing = registered.find(item => item.endpoint === endpoint);
      let pairReason = 'invalid-url';
      try {
        if (!validConversation(url)) throw new Error('Cannot arm this URL; open an existing ChatGPT conversation.');
        pairReason = 'invalid-tab';
        if (!Number.isInteger(tabId)) throw new Error('Active chat tab is unavailable.');
        pairReason = 'tab-unavailable';
        const tab = await chrome.tabs.get(tabId);
        pairReason = 'tab-moved';
        if (tab.url !== url) throw new Error('Tab moved; verify pairing again.');
        pairReason = 'mailbox-unavailable';
        const observed = await mailbox(name);
        pairReason = 'identity-changed';
        if (observed.endpoint !== endpoint || observed.generation !== generation) throw new Error('Mailbox changed; verify again.');
        const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
        pairReason = 'conversation-already-bound';
        if (wakeBindings.some(item => item.endpoint !== endpoint && item.url === url)) throw new Error('This chat is already paired with another mailbox.');
        const prior = wakeBindings.find(item => item.endpoint === endpoint);
        pairReason = 'generation-changed';
        if (prior && prior.generation !== generation) throw new Error('Mailbox generation changed; operator reconciliation required.');
        pairReason = 'unresolved-attempt';
        if (prior?.halted && observed.delivery_ids.length) throw new Error('Previous attempt is unresolved; mailbox must be empty before explicitly rearming.');
        pairReason = 'stop-requested';
        if (stopAll !== stopSnapshot.all || stopEndpoints.get(endpoint) !== stopSnapshot.endpoint) {
          throw new Error('Stop requested during pairing; verify again.');
        }
        // Explicit rearm may clear only intents already present when it arrived.
        if (stopAll === stopSnapshot.all) stopAll = 0;
        if (stopEndpoints.get(endpoint) === stopSnapshot.endpoint) stopEndpoints.delete(endpoint);
        const binding = {endpoint, generation, name: observed.name, tabId, url, armed: true,
          seen: prior?.seen || [], pending: [], status: 'armed', report: 'not checked', incident: prior?.incident,
          lastReconciledAttempt: prior?.halted ? {outcome: 'attempt-outcome-unknown',
            status: prior.status, delivery_ids: prior.attemptIds || null, checkedAt: prior.checkedAt || null}
            : prior?.lastReconciledAttempt,
          diagnostic: prior?.diagnostic, diagnosticAck: prior?.diagnosticAck,
          pairDiagnostic: {id: crypto.randomUUID(), boot: bootId, release: releaseId,
            at: new Date().toISOString(), result: 'accepted', reason: 'accepted', attempt: prior?.diagnostic?.id || null}};
        if (prior) wakeBindings[wakeBindings.indexOf(prior)] = binding;
        else wakeBindings.push(binding);
        await chrome.storage.local.set({wakeBindings});
        return status('armed');
      } catch (error) {
        if (existing) {
          existing.pairDiagnostic = {id: crypto.randomUUID(), boot: bootId, release: releaseId,
            at: new Date().toISOString(), result: 'refused', reason: pairReason, attempt: existing.diagnostic?.id || null};
          await chrome.storage.local.set({wakeBindings: registered});
        }
        throw error;
      }
    }
    if (message.type === 'disarm') {
      const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
      for (const binding of wakeBindings) if (!message.endpoint || binding.endpoint === message.endpoint) binding.armed = false;
      await chrome.storage.local.set({wakeBindings});
      return status('stopped; delivery history retained');
    }
    if (message.type === 'check') return check();
    throw new Error('unknown action');
  }).then(value => reply({status: value}), error => reply({error: error.message}));
  return true;
});
chrome.alarms.onAlarm.addListener(() => exclusive(check).catch(error => status('source-unavailable: ' + error.message)));
chrome.tabs.onRemoved.addListener(tabId => exclusive(async () => {
  const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
  const save = () => chrome.storage.local.set({wakeBindings});
  for (const binding of wakeBindings) if (binding.armed && !stopRequested(binding)) {
    if (binding.retry?.tabId === tabId) delete binding.retry;
    let tab;
    try { tab = await conversationTab(binding); }
    catch { continue; } // Failed lookup is not evidence the conversation is closed.
    if (tab) { binding.availability = 'open'; continue; }
    binding.availability = 'closed';
    await record(binding, 'bound-tab-missing', binding.observedPending || binding.pending || [], save);
  }
  await chrome.storage.local.set({wakeBindings});
}).catch(error => status(error.message)));
chrome.alarms.get('mailbox-wake').then(alarm => {
  if (!alarm) return chrome.alarms.create('mailbox-wake', {periodInMinutes: 0.5});
});
