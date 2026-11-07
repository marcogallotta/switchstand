const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const cancelled = new Set();
const bindingKey = (id, incarnation = null) => JSON.stringify([id, incarnation]);
const loggedObservations = new Map();
const HEARTBEAT_MS = 5 * 60 * 1000;
const observationReasons = {'Armed': 'idle', 'Chat not open': 'bound-tab-missing',
  'Chat suspended': 'tab-suspended', 'Wake outcome unknown': 'uncertain-wake',
  'Needs manual reconciliation': 'manual-reconciliation', 'Already nudged': 'already-nudged',
  'Mailbox service unavailable': 'source-unavailable', 'Invalid mailbox identity': 'source-unavailable',
  'Mailbox identity changed': 'mailbox-binding-changed'};
const actionOutcomes = ['busy', 'waiting', 'ready', 'draft-present', 'wrong-conversation',
  'composer-unavailable', 'insert-failed', 'click-attempted'];
async function diagnose(b, failure, trace) {
  const reason = failure || observationReasons[b.status] ||
    (actionOutcomes.includes(b.status) ? b.status : 'unknown-outcome');
  const observation = {endpoint: b.id, generation: b.generation, pending_count: b.pending?.length ?? null,
    reason, uncertain: Boolean(b.uncertain), attempt: b.wakeAttempt || null};
  // Timing is useful in a receipt, but cannot be part of a state-change signature.
  if (trace) observation.trace = trace;
  const signature = JSON.stringify({...observation, trace: trace && {
    phase: trace.phase, tab: trace.tab, tab_count: trace.tab_count}});
  const key = bindingKey(b.id, b.incarnation), previous = loggedObservations.get(key);
  const heartbeat = previous?.signature === signature;
  if ((heartbeat && Date.now() - previous.at < HEARTBEAT_MS) || cancelled.has(key)) return;
  try {
    const config = await source('/config');
    if (cancelled.has(bindingKey(b.id, b.incarnation))) return;
    const response = await fetch(SOURCE + '/report', {method: 'POST', signal: AbortSignal.timeout(3000),
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + config.report_token},
      body: JSON.stringify({name: b.name, delivery_ids: [], outcome: 'attempt-outcome-unknown',
        observation: {...observation, kind: heartbeat ? 'heartbeat' : 'transition', observed_at: new Date().toISOString()}})});
    if (response.ok && (await response.json()).status === 'logged') loggedObservations.set(key, {signature, at: Date.now()});
    else console.warn('browser_wake diagnostic rejected', {reason, http_status: response.status});
  } catch { console.warn('browser_wake diagnostic unavailable', {reason}); }
}
let queue = Promise.resolve();
const exclusive = fn => { const result = queue.then(fn); queue = result.catch(() => {}); return result; };
const save = minimalBindings => chrome.storage.local.set({minimalBindings});
async function load() {
  const state = await chrome.storage.local.get(['minimalBindings', 'wakeBindings']);
  if (Array.isArray(state.minimalBindings)) return state.minimalBindings;
  const bindings = (state.wakeBindings || []).map(b => {
    const ids = b.retryBudget?.ids || b.attemptIds;
    return {id: b.endpoint, name: b.name, generation: b.generation, url: b.url, armed: Boolean(b.armed),
      ids, seen: b.seen, uncertain: Boolean(b.halted || b.pendingSend || b.diagnostic || b.stopSendAttempt || b.attemptIds?.length || b.retryBudget?.usedRetries), status: b.status, alert: b.incident?.evidence,
      alertDone: ['queued', 'recovery-pending'].includes(b.incident?.state)};
  });
  await save(bindings);
  return bindings;
}
function validUrl(url) { return /^https:\/\/chatgpt\.com\/(?:g\/[a-zA-Z0-9-]+\/)?c\/[a-zA-Z0-9-]+\/?$/.test(url || ''); }
function clearConfirmedAlert(b) {
  if (!b.alertDone) return;
  delete b.alert; delete b.alertDone; delete b.recovering; delete b.checked; delete b.since; delete b.report;
}
async function source(path) {
  const response = await fetch(SOURCE + path, {cache: 'no-store', signal: AbortSignal.timeout(3000)});
  const value = await response.json();
  if (!response.ok || value.status !== 'ok') throw Error('Mailbox service unavailable');
  return value;
}
async function poll(name) {
  const p = await source('/poll?name=' + encodeURIComponent(name));
  if (p.name !== name || !UUID.test(p.endpoint) || !Number.isInteger(p.generation) || p.generation < 1 ||
      !Array.isArray(p.delivery_ids) || p.delivery_ids.some(id => !UUID.test(id))) throw Error('Invalid mailbox identity');
  return p;
}
function composer(url, action, diagnostic = false) {
  const result = (outcome, detail) => diagnostic ? {outcome, detail} : outcome;
  if (location.href !== url) return result('wrong-conversation', 'url-mismatch');
  const isVisible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
  const stopSelector = '[data-testid="stop-button"],button[aria-label="Stop" i],button[aria-label="Stop streaming" i]';
  const sendSelector = '[data-testid="send-button"],[data-testid="composer-submit-button"],#composer-submit-button,button[aria-label="Send prompt"],button[aria-label="Send" i]';
  const container = ['[data-testid="composer"]', '#composer-background', '#thread-bottom-container', '[data-composer-body]']
    .map(selector => [...document.querySelectorAll(selector)].filter(isVisible).filter(node => {
      const editors = [...node.querySelectorAll('textarea, [contenteditable="true"]')].filter(isVisible);
      const stops = [...node.querySelectorAll(stopSelector)].filter(isVisible);
      return editors.length === 1 || (!editors.length && stops.length === 1);
    }))
    .find(matches => matches.length === 1)?.[0];
  const stopsWithoutEditor = container ? [...container.querySelectorAll(stopSelector)].filter(isVisible) : [];
  const preferred = document.getElementById('prompt-textarea');
  const alternatives = container ? [...container.querySelectorAll('textarea, [contenteditable="true"]')].filter(isVisible) : [];
  const editor = preferred && isVisible(preferred) ? preferred : alternatives.length === 1 ? alternatives[0] : null;
  let form = editor?.closest('form') || editor?.closest('[data-testid="composer"], [data-composer-body]');
  for (let scope = editor?.parentElement, depth = 0; !form && scope && depth < 6; scope = scope.parentElement, depth++) {
    if ([...scope.querySelectorAll(sendSelector)].filter(isVisible).length === 1) form = scope;
  }
  if (!form || !editor) {
    if (!editor && stopsWithoutEditor.length === 1) {
      const stopping = action === 'prepare' && !stopsWithoutEditor[0].disabled && stopsWithoutEditor[0].getAttribute('aria-disabled') !== 'true';
      if (stopping) stopsWithoutEditor[0].click();
      return result('busy', stopping ? 'stop-requested' : 'busy');
    }
    return result('composer-unavailable', !editor ? 'editor-missing' : 'form-missing');
  }
  const textarea = editor instanceof HTMLTextAreaElement;
  const text = 'Process your mailbox';
  const draft = textarea ? editor.value : editor.textContent;
  if ((!textarea && !editor.isContentEditable) || editor.disabled || editor.readOnly) return result('composer-unavailable', 'editor-disabled');
  if (editor.querySelector('img') || document.querySelector('[data-testid="remove-file-button"]')) return result('draft-present', 'attachment-present');
  if (draft && draft !== text) return result('draft-present', 'unrelated-draft');
  const visible = selector => [...form.querySelectorAll(selector)].filter(b => b.getClientRects().length && getComputedStyle(b).visibility !== 'hidden');
  const enabled = b => b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
  const stops = visible('[data-testid="stop-button"],button[aria-label="Stop" i],button[aria-label="Stop streaming" i]');
  if (stops.length) {
    const stopping = action === 'prepare' && stops.length === 1 && enabled(stops[0]);
    if (stopping) stops[0].click();
    return result('busy', stopping ? 'stop-requested' : 'busy');
  }
  if (action !== 'send' && !draft) {
    editor.focus();
    if (textarea) {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, text);
      editor.dispatchEvent(new InputEvent('input', {bubbles: true, data: text, inputType: 'insertText'}));
    } else if (!document.execCommand('insertText', false, text)) return result('insert-failed', 'insert-failed');
    return result('waiting', 'prompt-inserted');
  }
  if (draft !== text) return result('draft-present', 'unrelated-draft');
  const sends = visible(sendSelector);
  if (sends.length !== 1 || !enabled(sends[0])) return result('waiting',
    !sends.length ? 'send-control-missing' : sends.length > 1 ? 'send-control-ambiguous' : 'send-control-disabled');
  if (action === 'send') { sends[0].click(); return result('click-attempted', 'click-dispatched'); }
  return result('ready', 'send-ready');
}
async function report(b, bindings) {
  if (cancelled.has(bindingKey(b.id, b.incarnation)) || (!b.pending?.length && !b.recovering) || (b.alertDone && !b.recovering)) return;
  const now = Date.now(), missing = b.status === 'Chat not open', draft = b.status === 'draft-present';
  if (!b.recovering) {
    if (!b.checked || now - b.checked > 60000) b.since = now;
    b.checked = now;
    if (!b.alert && (missing || now - b.since >= 60000)) {
      b.alert = {incident_id: crypto.randomUUID(), phase: 'blocked', name: b.name, endpoint: b.id, generation: b.generation,
        delivery_ids: b.pending.slice(0, 50), reason: missing ? 'bound-tab-missing' : draft ? 'draft-present' : 'pending-mail',
        started_at: new Date(b.since).toISOString(), observed_at: new Date(now).toISOString()};
    }
  }
  if (!b.alert) return;
  await save(bindings);
  try {
    const config = await source('/config');
    if (cancelled.has(bindingKey(b.id, b.incarnation))) return;
    const recovery = b.recovering && b.alertDone;
    const incident = recovery ? {...b.alert, phase: 'recovered'} : b.alert;
    const response = await fetch(SOURCE + '/report', {method: 'POST', signal: AbortSignal.timeout(3000),
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + config.report_token},
      body: JSON.stringify({name: b.name, delivery_ids: b.alert.delivery_ids, outcome: 'attempt-outcome-unknown', incident})});
    const receipt = await response.json();
    const queued = response.ok && receipt.alert?.status === 'queued';
    if (recovery && queued) { clearConfirmedAlert(b); b.report = 'Recovery reported'; }
    else if (queued) { b.alertDone = true; b.report = 'Coordinator alerted'; }
    else {
      if (!recovery && ['observation_expired', 'pending_evidence_changed'].includes(receipt.alert?.reason)) {
        delete b.alert; delete b.recovering; delete b.checked;
      }
      b.report = 'Alert not confirmed';
    }
  } catch { b.report = 'Alert outcome unknown; same incident retained'; }
}
async function check(bindings, onlyId) {
  for (const b of bindings) {
    if (!b.armed || b.removed || cancelled.has(bindingKey(b.id, b.incarnation)) || (onlyId && b.id !== onlyId)) continue;
    b.pending = null;
    let stage = 'source-unavailable', failure;
    const trace = {phase: 'poll', tab: 'unchecked'};
    try {
      const pollStarted = Date.now();
      const p = await poll(b.name);
      trace.poll_ms = Math.max(0, Math.min(60000, Date.now() - pollStarted));
      if (p.endpoint !== b.id || p.generation !== b.generation) throw Error('Mailbox identity changed');
      b.pending = p.delivery_ids;
      if (!p.delivery_ids.length) {
        trace.phase = 'idle';
        delete b.checked; delete b.since; b.status = b.uncertain ? 'Needs manual reconciliation' : 'Armed';
        if (b.alert) b.recovering = true;
        else delete b.alertDone;
        continue;
      }
      stage = 'execution-failed';
      trace.phase = 'tabs';
      const tabs = await chrome.tabs.query({url: 'https://chatgpt.com/*'});
      trace.tab_count = tabs.length;
      const tab = tabs.find(t => t.url === b.url);
      if (!tab) { trace.tab = 'missing'; b.status = 'Chat not open'; continue; }
      trace.tab = tab.discarded ? 'discarded' : tab.frozen ? 'frozen' : 'active';
      if (tab.discarded || tab.frozen) { b.status = 'Chat suspended'; continue; }
      if (b.uncertain) { trace.phase = 'fenced'; b.status = 'Wake outcome unknown'; continue; }
      const freshIds = p.delivery_ids.filter(id => !b.seen?.includes(id));
      if (!freshIds.length) { trace.phase = 'seen'; b.status = 'Already nudged'; continue; }
      if (b.ids?.length && !freshIds.some(id => b.ids.includes(id))) b.ids = freshIds.slice();
      b.ids ||= freshIds.slice();
      await save(bindings);
      let executed = false;
      for (let tick = 0; tick < 50 && !cancelled.has(bindingKey(b.id, b.incarnation)); tick++) {
        const action = b.status === 'ready' ? 'send' : b.status === 'busy' ? 'wait' : 'prepare';
        if (action === 'send') {
          trace.phase = 'send-preflight';
          stage = 'source-unavailable';
          const fresh = await poll(b.name);
          stage = 'execution-failed';
          if (fresh.endpoint !== b.id || fresh.generation !== b.generation || !fresh.delivery_ids.some(id => b.ids.includes(id)) || cancelled.has(bindingKey(b.id, b.incarnation))) break;
        }
        b.uncertain = true;
        b.wakeAttempt = {id: crypto.randomUUID(), action, started_at: new Date().toISOString()};
        await save(bindings);
        if (cancelled.has(bindingKey(b.id, b.incarnation))) break;
        trace.phase = 'execute';
        const [receipt] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: composer, args: [b.url, action, true]});
        executed = true;
        b.status = receipt?.result?.outcome || 'Unknown outcome';
        if (receipt?.result?.detail) b.wakeAttempt.detail = receipt.result.detail;
        b.wakeAttempt.finished_at = new Date().toISOString();
        b.wakeAttempt.outcome = actionOutcomes.includes(b.status) ? b.status : 'unknown-outcome';
        if (['busy', 'waiting', 'ready', 'draft-present', 'wrong-conversation', 'composer-unavailable', 'insert-failed'].includes(b.status)) delete b.uncertain;
        await save(bindings);
        if (!['busy', 'waiting', 'ready'].includes(b.status)) break;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      if (executed && !cancelled.has(bindingKey(b.id, b.incarnation))) trace.phase = 'complete';
    } catch (e) {
      b.status = e.message;
      failure = observationReasons[b.status] || stage;
      if (b.wakeAttempt && !b.wakeAttempt.finished_at && stage === 'execution-failed') {
        b.wakeAttempt.finished_at = new Date().toISOString();
        b.wakeAttempt.outcome = 'execution-failed';
      }
    }
    finally { await report(b, bindings); await save(bindings); await diagnose(b, failure, trace); }
  }
}
chrome.runtime.onMessage.addListener((m, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return;
  const matches = b => b.id === m.id && (b.incarnation ?? null) === (m.expectedIncarnation ?? null);
  const fence = m.type === 'remove' ? chrome.storage.local.get(['minimalBindings', 'wakeBindings']).then(state => {
    const bs = Array.isArray(state.minimalBindings) ? state.minimalBindings : (state.wakeBindings || []).map(b => ({id: b.endpoint}));
    if (bs.some(matches)) cancelled.add(bindingKey(m.id, m.expectedIncarnation));
  }) : Promise.resolve();
  exclusive(async () => {
    await fence;
    const bindings = await load();
    if (['resume', 'rearm', 'remove'].includes(m.type) && !bindings.some(matches)) throw Error('Chat changed; reopen the popup');
    if (m.type === 'add') {
      if (!validUrl(m.url) || !m.name) throw Error('Open an exact ChatGPT conversation and enter its mailbox');
      if (!Number.isInteger(m.tabId)) throw Error('Current chat tab is unavailable');
      const tab = await chrome.tabs.get(m.tabId);
      if (tab.url !== m.url || tab.discarded || tab.frozen) throw Error('Current chat changed; try Add chat again');
      const p = await poll(m.name);
      const current = await chrome.tabs.get(m.tabId);
      if (current.url !== m.url || current.discarded || current.frozen) throw Error('Current chat changed; try Add chat again');
      const collides = b => b.id === p.endpoint || b.url === m.url;
      if (bindings.some(b => collides(b) && !b.removed)) throw Error('Chat already added; use Resume');
      for (let i = bindings.length - 1; i >= 0; i--) if (collides(bindings[i])) bindings.splice(i, 1);
      bindings.push({id: p.endpoint, name: p.name, generation: p.generation, url: m.url, armed: true, status: 'Armed', incarnation: crypto.randomUUID()});
    } else if (m.type === 'rearm') {
      const b = bindings.find(b => b.id === m.id && !b.removed);
      if (!b?.uncertain || m.expectedAttempt !== (b.wakeAttempt?.id || null)) throw Error('Uncertain wake changed; reopen the popup');
      const p = await poll(b.name);
      if (p.endpoint !== b.id || p.generation !== b.generation) throw Error('Mailbox identity changed');
      if (p.delivery_ids.length || b.alert || cancelled.has(bindingKey(b.id, b.incarnation))) throw Error('Pending mail or alert must clear before Re-arm');
      b.uncertain = false;
      b.armed = true;
      b.status = 'Armed';
    } else if (m.type === 'rearm-all') {
      for (const b of bindings) {
        if (b.removed || cancelled.has(bindingKey(b.id, b.incarnation))) continue;
        b.uncertain = false;
        b.armed = true;
        b.status = 'Armed';
      }
    } else if (m.type === 'resume' || m.type === 'resume-all') {
      if (m.type === 'resume' && !bindings.some(b => b.id === m.id && !b.removed)) throw Error('Chat not found');
      for (const b of bindings) if (!b.removed && !cancelled.has(bindingKey(b.id, b.incarnation)) && (m.type === 'resume-all' || b.id === m.id)) b.armed = true;
      await check(bindings, m.type === 'resume' ? m.id : undefined);
    } else if (m.type === 'remove') {
      const b = bindings.find(b => b.id === m.id);
      if (!b) throw Error('Chat not found');
      b.removed = true;
      b.armed = false;
      b.status = 'Removed';
    } else throw Error('Unknown request');
    await save(bindings);
    return {};
  }).then(reply, e => reply({error: m.type === 'remove' && e.message !== 'Chat changed; reopen the popup' && cancelled.has(bindingKey(m.id, m.expectedIncarnation)) ? `Remove not saved: ${e.message}. Local wakes fenced until restart; saved chat may resume after restart.` : e.message}));
  return true;
});
chrome.alarms.create('poll', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'poll') exclusive(async () => check(await load())); });
