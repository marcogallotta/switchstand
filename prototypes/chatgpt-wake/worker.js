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
async function nudge(expectedUrl, observeOnly = false, attempt) {
  if (location.href !== expectedUrl) return 'wrong-conversation';
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
    const candidates = [...document.querySelectorAll('textarea, [contenteditable]')]
      .filter(element => element.getClientRects().length).slice(0, 5)
      .map(element => `${element.tagName}#${element.id}[role=${element.getAttribute('role') || ''},editable=${element.isContentEditable},form=${Boolean(element.closest('form'))}]`);
    return `composer-unavailable: missing #prompt-textarea; visible editors: ${candidates.join(', ') || 'none'}`;
  }
  const textarea = editor instanceof HTMLTextAreaElement;
  if (!textarea && !editor.isContentEditable) return `composer-unavailable: ${editor.tagName} is not editable`;
  if (!editor.getClientRects().length) return 'composer-unavailable: #prompt-textarea is hidden';
  if (textarea && (editor.disabled || editor.readOnly)) return 'composer-unavailable: textarea is disabled or read-only';
  if (document.querySelector('[data-testid="stop-button"], button[aria-label="Stop streaming"]')) return 'busy';
  const draft = () => textarea ? editor.value : editor.textContent;
  const text = 'Process your mailbox';
  const attached = () => editor.querySelector('img') || document.querySelector('[data-testid="remove-file-button"]');
  if (!attempt?.retry && (draft().trim() || attached())) return 'draft-present';
  if (observeOnly) return 'idle';
  const form = editor.closest('form');
  const sendControl = () => form?.querySelector('[data-testid="send-button"], [data-testid="composer-submit-button"], #composer-submit-button, button[type="submit"], button[aria-label="Send prompt"]');
  const enabled = send => send && !send.disabled && !/voice|dictat/i.test(send.getAttribute('aria-label') || '');
  const owned = witness => witness && witness.token === attempt?.token && witness.editor === editor &&
    witness.url === expectedUrl && witness.valid && editor.isConnected && location.href === expectedUrl &&
    draft() === text && !attached() && !witness.observer.takeRecords().length;
  if (attempt?.retry) {
    const witness = globalThis.switchstandWakeWitness;
    const send = sendControl();
    const canClick = owned(witness) && enabled(send);
    witness?.retire();
    if (!canClick) return 'retry-exhausted: ownership or send control unavailable';
    send.click();
    return 'click-attempted';
  }
  editor.focus();
  if (textarea) {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, text);
    editor.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
  } else if (!document.execCommand('insertText', false, text)) return 'insert-failed';
  let witness;
  if (attempt?.token) {
    globalThis.switchstandWakeWitness?.retire();
    const events = ['input', 'beforeinput', 'paste', 'change', 'keydown', 'click', 'submit'];
    const invalidate = () => witness.retire();
    witness = {token: attempt.token, editor, url: expectedUrl, valid: true,
      observer: new MutationObserver(invalidate), retire() {
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
  if (location.href !== expectedUrl || draft().trim() !== text ||
      !send || send.disabled || /voice|dictat/i.test(send.getAttribute('aria-label') || '') ||
      document.querySelector('[data-testid="stop-button"]')) {
    const controls = [...(form?.querySelectorAll('button') || [])].slice(0, 5)
      .map(button => `id=${button.id},testid=${button.getAttribute('data-testid') || ''},type=${button.type},label=${button.getAttribute('aria-label') || ''},disabled=${button.disabled}`);
    const outcome = `inserted-not-submitted: no click; editor=${editor.tagName}[role=${editor.getAttribute('role') || ''}]; form=${Boolean(form)}; controls: ${controls.join('; ') || 'none'}`;
    if (owned(witness)) return {outcome, noClickToken: attempt.token};
    witness?.retire();
    return outcome;
  }
  // User actions during the wait for a send control revoke ownership permanently.
  if (attempt && !owned(witness)) { witness?.retire(); return 'attempt-outcome-unknown: ownership changed'; }
  witness?.retire();
  send.click();
  return 'click-attempted'; // A click is not a provider acceptance receipt.
}
const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let reportToken;
const bootId = crypto.randomUUID();
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
async function checkBinding(binding, save) {
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
  try { tab = await chrome.tabs.get(binding.tabId); }
  catch { binding.availability = 'closed'; return note('bound-tab-missing'); }
  binding.availability = 'open';
  if (binding.halted && !binding.retry) return note('attempt-outcome-unknown');
  const retry = binding.retry;
  if (retry && (retry.boot !== bootId || retry.stop !== stopVersion ||
      retry.endpoint !== binding.endpoint || retry.generation !== binding.generation ||
      retry.tabId !== binding.tabId || retry.url !== binding.url || tab.url !== binding.url ||
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
      const [result] = await chrome.scripting.executeScript({target: {tabId: binding.tabId}, func: nudge, args: [binding.url, true]});
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
  await save();
  if (stopRequested(binding) || stopVersion !== attemptStop) {
    binding.armed = false; binding.halted = false; binding.attemptIds = [];
    return note('stopped');
  }
  try {
    const [result] = await chrome.scripting.executeScript({target: {tabId: binding.tabId}, func: nudge, args: [binding.url, false, attempt]});
    const receipt = result?.result;
    const outcome = (typeof receipt === 'string' ? receipt : receipt?.outcome) || 'attempt-outcome-unknown';
    if (!retry && receipt?.noClickToken === attempt.token && outcome.startsWith('inserted-not-submitted:') &&
        !stopRequested(binding) && stopVersion === attemptStop) {
      binding.retry = {token: attempt.token, boot: bootId, stop: attemptStop, endpoint: binding.endpoint,
        generation: binding.generation, tabId: binding.tabId, url: binding.url, ids: pending.slice()};
    }
    const safe = ['busy', 'draft-present', 'wrong-conversation', 'insert-failed'].includes(outcome) || outcome.startsWith('composer-unavailable');
    if (safe && !retry) binding.halted = false;
    if (outcome === 'click-attempted') {
      binding.seen.push(...pending);
      binding.pending = [];
      binding.halted = false;
    }
    await note(outcome);
  } catch (error) { await note('attempt-outcome-unknown: ' + error.message); }
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
      if (!validConversation(url)) throw new Error('Cannot arm this URL; open an existing ChatGPT conversation.');
      if (!Number.isInteger(tabId)) throw new Error('Active chat tab is unavailable.');
      const tab = await chrome.tabs.get(tabId);
      if (tab.url !== url) throw new Error('Tab moved; verify pairing again.');
      const observed = await mailbox(name);
      if (observed.endpoint !== endpoint || observed.generation !== generation) throw new Error('Mailbox changed; verify again.');
      const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
      if (wakeBindings.some(item => item.endpoint !== endpoint && item.url === url)) throw new Error('This chat is already paired with another mailbox.');
      const prior = wakeBindings.find(item => item.endpoint === endpoint);
      if (prior?.halted) throw new Error('Previous attempt is unresolved; reconcile before rearming.');
      if (prior && prior.generation !== generation) throw new Error('Mailbox generation changed; operator reconciliation required.');
      // Explicit rearm may clear only intents already present when it arrived.
      if (stopAll === stopSnapshot.all) stopAll = 0;
      if (stopEndpoints.get(endpoint) === stopSnapshot.endpoint) stopEndpoints.delete(endpoint);
      const binding = {endpoint, generation, name: observed.name, tabId, url, armed: true,
        seen: prior?.seen || [], pending: [], status: 'armed', report: 'not checked', incident: prior?.incident};
      if (prior) wakeBindings[wakeBindings.indexOf(prior)] = binding;
      else wakeBindings.push(binding);
      await chrome.storage.local.set({wakeBindings});
      return status('armed');
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
  for (const binding of wakeBindings) if (binding.tabId === tabId && binding.armed && !stopRequested(binding)) {
    binding.availability = 'closed';
    await record(binding, 'bound-tab-missing', binding.observedPending || binding.pending || [], save);
  }
  await chrome.storage.local.set({wakeBindings});
}).catch(error => status(error.message)));
chrome.alarms.get('mailbox-wake').then(alarm => {
  if (!alarm) return chrome.alarms.create('mailbox-wake', {periodInMinutes: 0.5});
});
