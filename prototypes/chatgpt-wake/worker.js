const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const cancelled = new Set();
const bindingKey = (id, incarnation = null) => JSON.stringify([id, incarnation]);
const COOLDOWN_MS = 2 * 60 * 1000;
const LATER_RETRY_MS = 4 * 60 * 1000;
const validIds = ids => Array.isArray(ids) && ids.length > 0 && ids.every(id => typeof id === 'string' && UUID.test(id));
function retireBatch(b) {
  if (!validIds(b.ids)) return false;
  b.seen = [...new Set([...(b.seen || []), ...b.ids])];
  delete b.ids; delete b.sendBudget; delete b.uncertain;
  return true;
}
function migrateRetry(b) {
  if (b.retry) return false;
  const legacy = b.sendBudget;
  const valid = legacy && validIds(b.ids) && validIds(legacy.ids) &&
    legacy.ids.every(id => b.ids.includes(id)) &&
    Number.isInteger(legacy.auto) && legacy.auto >= 0 && legacy.auto <= 3;
  // Both the prior minimal format and old wakeBindings may contain an uncertain
  // Send with no reliable timestamp. Preserve evidence without fencing new mail.
  const possibleSend = Boolean(b.uncertain || b.wakeAttempt || b.legacySendEvidence || (legacy && !valid));
  const attempts = !legacy ? (possibleSend ? 1 : 0) :
    valid ? Math.max(legacy.auto, possibleSend ? 1 : 0) : 3;
  const validTime = valid && Number.isFinite(legacy.lastAt) && legacy.lastAt > 0;
  // One persisted migration-time guard avoids an immediate duplicate on upgrade.
  b.retry = {attempts, lastAt: validTime ? legacy.lastAt : attempts ? Date.now() : 0};
  if (legacy && !valid) b.uncertain = true;
  if (possibleSend) b.priorUncertain = true; // Historical outcome remains UNKNOWN.
  return true;
}
function budgetBlock(b) {
  const retry = b.retry;
  const delay = (retry?.attempts || 0) >= 2 ? LATER_RETRY_MS : COOLDOWN_MS;
  return retry?.lastAt && Date.now() - retry.lastAt < delay ? 'Wake cooling down' : null;
}
async function reserveSend(b, bindings) {
  const block = budgetBlock(b);
  if (block) return block;
  b.retry ||= {attempts: 0, lastAt: 0};
  b.retry.previousLastAt = b.retry.lastAt;
  b.retry.lastAt = Date.now();
  b.retry.attempts = Math.min(3, b.retry.attempts + 1);
  await save(bindings);
  return null;
}
async function refundSend(b, bindings) {
  if (!b.retry) return;
  b.retry.attempts = Math.max(0, b.retry.attempts - 1);
  b.retry.lastAt = b.retry.previousLastAt || 0;
  delete b.retry.previousLastAt;
  await save(bindings);
}
const loggedObservations = new Map();
const observationReasons = {'Armed': 'idle', 'Chat not open': 'bound-tab-missing',
  'Chat suspended': 'tab-suspended', 'Wake outcome unknown': 'uncertain-wake',
  'Needs manual reconciliation': 'manual-reconciliation', 'Already nudged': 'already-nudged',
  'Mailbox service unavailable': 'source-unavailable', 'Invalid mailbox identity': 'source-unavailable',
  'Mailbox identity changed': 'mailbox-binding-changed'};
const actionOutcomes = ['busy', 'waiting', 'ready', 'draft-present', 'wrong-conversation',
  'composer-unavailable', 'insert-failed', 'click-attempted'];
async function diagnose(b, failure) {
  const reason = failure || observationReasons[b.status] ||
    (actionOutcomes.includes(b.status) ? b.status : 'unknown-outcome');
  const observation = {endpoint: b.id, generation: b.generation, pending_count: b.pending?.length ?? null,
    reason, uncertain: Boolean(b.uncertain), attempt: b.wakeAttempt || null};
  const signature = JSON.stringify(observation);
  if (loggedObservations.get(bindingKey(b.id, b.incarnation)) === signature || cancelled.has(bindingKey(b.id, b.incarnation))) return;
  try {
    const config = await source('/config');
    if (cancelled.has(bindingKey(b.id, b.incarnation))) return;
    const response = await fetch(SOURCE + '/report', {method: 'POST', signal: AbortSignal.timeout(3000),
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + config.report_token},
      body: JSON.stringify({name: b.name, delivery_ids: [], outcome: 'attempt-outcome-unknown',
        observation: {...observation, observed_at: new Date().toISOString()}})});
    if (response.ok && (await response.json()).status === 'logged') loggedObservations.set(bindingKey(b.id, b.incarnation), signature);
    else console.warn('browser_wake diagnostic rejected', {reason, http_status: response.status});
  } catch { console.warn('browser_wake diagnostic unavailable', {reason}); }
}
let queue = Promise.resolve();
const exclusive = fn => { const result = queue.then(fn); queue = result.catch(() => {}); return result; };
const save = minimalBindings => chrome.storage.local.set({minimalBindings});
async function load() {
  const state = await chrome.storage.local.get(['minimalBindings', 'wakeBindings']);
  if (Array.isArray(state.minimalBindings)) {
    let migrated = false;
    for (const binding of state.minimalBindings) migrated = migrateRetry(binding) || migrated;
    if (migrated) await save(state.minimalBindings);
    return state.minimalBindings;
  }
  const bindings = (state.wakeBindings || []).map(b => {
    const ids = b.retryBudget?.ids || b.attemptIds;
    const legacySendEvidence = Boolean(b.pendingSend || b.diagnostic || b.stopSendAttempt ||
      b.attemptIds?.length || b.retryBudget?.usedRetries);
    return {id: b.endpoint, name: b.name, generation: b.generation, url: b.url, armed: Boolean(b.armed),
      ids, seen: b.seen, legacySendEvidence,
      uncertain: Boolean(b.halted || legacySendEvidence), status: b.status, alert: b.incident?.evidence,
      alertDone: ['queued', 'recovery-pending'].includes(b.incident?.state)};
  });
  bindings.forEach(migrateRetry);
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
  if (response.status === 403 && value.status === 'denied' && value.reason === 'mailbox_not_found')
    throw Error('Mailbox not found. Check the exact registered name.');
  if (!response.ok || value.status !== 'ok') throw Error('Mailbox service unavailable');
  return value;
}
async function poll(name) {
  const p = await source('/poll?name=' + encodeURIComponent(name));
  if (typeof p.name !== 'string' || !p.name.trim() || !UUID.test(p.endpoint) || !Number.isInteger(p.generation) || p.generation < 1 ||
      !Array.isArray(p.delivery_ids) || p.delivery_ids.some(id => !UUID.test(id)) ||
      !Array.isArray(p.available_delivery_ids) || p.available_delivery_ids.some(id => !UUID.test(id) || !p.delivery_ids.includes(id)))
    throw Error('Invalid mailbox identity');
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
      return result('busy', 'busy');
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
    return result('busy', 'busy');
  }
  if (action === 'inspect' && !draft) return result('waiting', 'editor-empty');
  if (action !== 'send' && action !== 'inspect' && !draft) {
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
async function check(bindings, onlyId, manual = false) {
  for (const b of bindings) {
    if (!b.armed || b.removed || cancelled.has(bindingKey(b.id, b.incarnation)) || (onlyId && b.id !== onlyId)) continue;
    b.pending = null;
    let stage = 'source-unavailable', failure;
    let currentAttempt = null;
    try {
      const p = await poll(b.name);
      if (p.endpoint !== b.id || p.generation !== b.generation) throw Error('Mailbox identity changed');
      b.pending = p.delivery_ids;
      if (validIds(b.ids) && b.ids.every(id => !p.delivery_ids.includes(id))) retireBatch(b);
      if (!p.available_delivery_ids.length) {
        b.status = p.delivery_ids.length ? 'Already nudged' : 'Armed';
        if (!p.delivery_ids.length) {
          delete b.checked; delete b.since;
          if (b.alert) b.recovering = true;
          else { delete b.alertDone; delete b.report; }
        }
        if (b.retry) b.retry.attempts = 0; // Retain last Send time across new mail.
        continue;
      }
      stage = 'execution-failed';
      const tabs = await chrome.tabs.query({url: 'https://chatgpt.com/*'});
      const tab = tabs.find(t => t.url === b.url);
      if (!tab) { b.status = 'Chat not open'; continue; }
      if (tab.discarded || tab.frozen) { b.status = 'Chat suspended'; continue; }
      const initialBlock = budgetBlock(b);
      if (initialBlock) { b.status = initialBlock; continue; }
      // Observe before mutating: an interrupted earlier prepare may have left a prompt.
      const [probe] = await chrome.scripting.executeScript({target: {tabId: tab.id},
        func: composer, args: [b.url, 'inspect', true]});
      b.status = probe?.result?.outcome || 'Unknown outcome';
      if (!['ready', 'waiting'].includes(b.status)) continue;
      await save(bindings);
      for (let tick = 0; tick < 50 && !cancelled.has(bindingKey(b.id, b.incarnation)); tick++) {
        const action = b.status === 'ready' ? 'send' : b.status === 'busy' ? 'wait' : 'prepare';
        if (action === 'send') {
          stage = 'source-unavailable';
          const fresh = await poll(b.name);
          stage = 'execution-failed';
          if (fresh.endpoint !== b.id || fresh.generation !== b.generation) { b.status = 'Mailbox identity changed'; break; }
          if (!fresh.available_delivery_ids.length) { b.status = 'Already nudged'; break; }
          if (cancelled.has(bindingKey(b.id, b.incarnation))) break;
          const block = await reserveSend(b, bindings);
          if (block) { b.status = block; break; }
        }
        if (action !== 'send') b.uncertain = true;
        b.wakeAttempt = currentAttempt = {id: crypto.randomUUID(), action, started_at: new Date().toISOString()};
        await save(bindings);
        if (cancelled.has(bindingKey(b.id, b.incarnation))) break;
        const [receipt] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: composer, args: [b.url, action, true]});
        b.status = receipt?.result?.outcome || 'Unknown outcome';
        if (receipt?.result?.detail) b.wakeAttempt.detail = receipt.result.detail;
        b.wakeAttempt.finished_at = new Date().toISOString();
        b.wakeAttempt.outcome = actionOutcomes.includes(b.status) ? b.status : 'unknown-outcome';
        if (['busy', 'waiting', 'ready', 'draft-present', 'wrong-conversation', 'composer-unavailable', 'insert-failed'].includes(b.status)) delete b.uncertain;
        if (action === 'send' && ['busy', 'waiting', 'ready', 'draft-present', 'wrong-conversation', 'composer-unavailable', 'insert-failed'].includes(b.status)) await refundSend(b, bindings);
        await save(bindings);
        if (!['busy', 'waiting', 'ready'].includes(b.status)) break;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    } catch (e) {
      b.status = e.message;
      failure = observationReasons[b.status] || stage;
      if (currentAttempt && b.wakeAttempt === currentAttempt && !currentAttempt.finished_at && stage === 'execution-failed') {
        b.wakeAttempt.finished_at = new Date().toISOString();
        b.wakeAttempt.outcome = 'execution-failed';
      }
    }
    finally { await report(b, bindings); await save(bindings); await diagnose(b, failure); }
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
    if (['resume', 'remove'].includes(m.type) && !bindings.some(matches)) throw Error('Chat changed; reopen the popup');
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
    } else if (m.type === 'rearm-all') {
      for (const b of bindings) {
        if (b.removed || cancelled.has(bindingKey(b.id, b.incarnation))) continue;
        b.armed = true;
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
