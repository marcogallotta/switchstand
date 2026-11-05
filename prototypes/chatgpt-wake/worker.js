// Exact verified pairings; manual and alarm checks share the same durable wake reservation.
const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let serial = Promise.resolve();
let writes = Promise.resolve();
let stopVersion = 0;
const endpointStops = new Map();
class PairingError extends Error {}
const releaseId = '57d98f58-9106-49c7-8cd1-c12153474457';
const bootId = crypto.randomUUID();
let reportToken;
const unresolved = binding => binding.halted || binding.attemptIds?.length || binding.pendingSend ||
  binding.stopSendAttempt || (binding.diagnostic &&
    (!binding.diagnostic.finished || binding.diagnostic.outcome !== 'click-attempted'));
const diagnosticSnapshot = binding => ({release: releaseId, boot: bootId,
  legacy_halt: Boolean(binding.halted && !binding.diagnostic), attempt: binding.diagnostic || null,
  pair: binding.pairDiagnostic || null, current_control: binding.currentControl || null});
async function diagnosticReport(binding) {
  const matches = live => live?.endpoint === binding.endpoint && live.generation === binding.generation && live.url === binding.url;
  const live = (await chrome.storage.local.get('wakeBindings')).wakeBindings?.find(matches);
  if (!live) return;
  const diagnostics = diagnosticSnapshot(live);
  const identity = JSON.stringify(diagnostics);
  if (live.diagnosticAck?.identity === identity && !live.diagnosticAck.failure) return;
  let failure = 'report-unavailable';
  try {
    if (!reportToken) {
      const config = await fetch(SOURCE + '/config', {signal: AbortSignal.timeout(3000), cache: 'no-store'});
      if (!config.ok) throw Error();
      const value = await config.json();
      if (value.status !== 'ok' || typeof value.report_token !== 'string') throw Error();
      reportToken = value.report_token;
    }
    const response = await fetch(SOURCE + '/report', {method: 'POST',
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + reportToken},
      body: JSON.stringify({name: live.name, delivery_ids: [], outcome: 'attempt-outcome-unknown',
        diagnostics: {...diagnostics, report_failure: live.diagnosticAck?.failure || null}}),
      signal: AbortSignal.timeout(3000)});
    if (response.ok && (await response.json()).status === 'logged') failure = null;
  } catch { /* Reporting cannot authorize or repeat a wake. */ }
  await persist(async () => {
    const current = await chrome.storage.local.get('wakeBindings');
    const latest = current.wakeBindings?.find(matches);
    if (!latest || JSON.stringify(diagnosticSnapshot(latest)) !== identity) return;
    latest.diagnosticAck = {identity, failure};
    await chrome.storage.local.set(current); // Merge only metadata; retain concurrent Stop.
  });
}
function diagnosticResult(binding, outcome, receipt, reason, error) {
  const admitted = new Set(['busy', 'draft-present', 'stopped', 'wrong-conversation', 'composer-unavailable',
    'insert-failed', 'inserted-not-submitted', 'click-attempted', 'attempt-outcome-unknown']);
  const value = outcome.split(':')[0];
  Object.assign(binding.diagnostic, {finished: new Date().toISOString(),
    outcome: admitted.has(value) ? value : 'attempt-outcome-unknown',
    reason: reason || receipt?.reason || 'result', facts: receipt?.facts || {},
    exception: error ? (['Error', 'TypeError', 'ReferenceError', 'DOMException'].includes(error.name) ? error.name : 'Other')
      : receipt?.exception || null});
}
function persist(action) {
  const result = writes.then(action);
  writes = result.catch(() => {});
  return result;
}
function validConversation(value) {
  try {
    const url = new URL(value);
    return url.origin === 'https://chatgpt.com' && !url.username && !url.password &&
      /^\/(?:g\/[a-zA-Z0-9-]+\/)?c\/[a-zA-Z0-9-]+\/?$/.test(url.pathname);
  } catch { return false; }
}
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
    const form = editor.closest('form');
    const stops = (root = form) => [...(root?.querySelectorAll('[data-testid="stop-button"], button[aria-label="Stop streaming" i], button[aria-label="Stop" i]') || [])]
      .filter(button => { const rect = button.getBoundingClientRect(); return rect.width > 0 && rect.height > 0 && getComputedStyle(button).visibility === 'visible'; });
    const enabled = send => send && !send.disabled && !send.matches(':disabled') &&
      send.getAttribute('aria-disabled')?.toLowerCase() !== 'true' &&
      !/voice|dictat/i.test(send.getAttribute('aria-label') || '');
    facts.busy = Boolean(stops(document).length);
    const draft = () => textarea ? editor.value : editor.textContent;
    const text = 'Process your mailbox';
    const attached = () => editor.querySelector('img') || document.querySelector('[data-testid="remove-file-button"]');
    facts.draftPresent = Boolean(draft().trim() || attached());
    const recover = attempt?.recover === true;
    if (facts.busy && !attempt?.prepare) return 'busy';
    const exactDraft = recover || attempt?.final || attempt?.insert;
    if (attached() || (exactDraft ? draft() && draft() !== text : facts.draftPresent) ||
        ((recover || attempt?.final) && draft() !== text)) return 'draft-present';
    const insert = () => {
      editor.focus();
      if (textarea) {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, text);
        editor.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
      } else if (!document.execCommand('insertText', false, text)) return false;
      return draft() === text;
    };
    if (attempt?.prepare) {
      const controls = stops();
      if (!controls.length && facts.busy) return 'busy';
      if (controls.length > 1) return 'composer-unavailable: ambiguous Stop';
      const stopReady = controls.length === 1 && Boolean(enabled(controls[0]));
      if (attempt.stop && stopReady) controls[0].click();
      if (!controls.length && attempt.insert && !draft() && !insert()) return 'insert-failed';
      const send = !controls.length && selectControl(form);
      Object.assign(facts, {controlPresent: Boolean(send), controlEnabled: Boolean(enabled(send)),
        inserted: Boolean(attempt.insert && !recover && draft() === text)});
      return {outcome: controls.length ? 'busy' : 'idle', stopReady,
        prepareReady: Boolean(form && !recover && !draft() && !facts.busy && facts.controlSelection !== 'ambiguous'),
        sendReady: Boolean(send && (!(recover || attempt.insert) || enabled(send))),
        inserted: Boolean(attempt.insert && !recover && draft() === text)};
    }
    if (facts.busy) return 'busy';
    if (observeOnly) return 'idle';
    const sendControl = () => selectControl(form);
    if (!attempt?.final) {
      if (!recover && !insert()) return 'insert-failed';
      for (let tick = 0; tick < 15; tick++) {
        await new Promise(resolve => setTimeout(resolve, 100));
        if (enabled(sendControl())) break;
      }
    }
    facts.inserted = attempt?.final ? Boolean(attempt.inserted) : !recover;
    let send;
    send = sendControl(); // Recheck uniqueness and state after the render wait.
    facts.controlPresent = Boolean(send);
    facts.controlEnabled = Boolean(enabled(send));
    if (location.href !== expectedUrl || draft().trim() !== text ||
        !enabled(send) ||
        stops(document).length || attached()) {
      const controls = [...(form?.querySelectorAll('button') || [])].slice(0, 5)
        .map(button => `id=${button.id},testid=${button.getAttribute('data-testid') || ''},type=${button.type},label=${button.getAttribute('aria-label') || ''},disabled=${button.disabled}`);
      const outcome = `inserted-not-submitted: no click; editor=${editor.tagName}[role=${editor.getAttribute('role') || ''}]; form=${Boolean(form)}; controls: ${controls.join('; ') || 'none'}`;
      return outcome;
    }
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
async function mailbox(name) {
  const response = await fetch(SOURCE + '/poll?name=' + encodeURIComponent(name),
    {signal: AbortSignal.timeout(3000), cache: 'no-store'});
  if (!response.ok) throw new PairingError('Mailbox service unavailable.');
  const observed = await response.json();
  if (observed.status !== 'ok' || !UUID.test(observed.endpoint) ||
      !Number.isInteger(observed.generation) || observed.generation < 1 ||
      typeof observed.name !== 'string' || !Array.isArray(observed.delivery_ids) ||
      !observed.delivery_ids.every(id => typeof id === 'string' && UUID.test(id))) throw new PairingError('Invalid mailbox identity.');
  return observed;
}
async function action(message, version, endpointVersion) {
  if (message.type === 'verify') return mailbox(message.name);
  if (message.type === 'check') return check(version);
  if (message.type !== 'bind') throw new PairingError('Unknown action.');
  const {tabId, url, name, endpoint, generation} = message;
  let pairReason = 'invalid-url';
  try {
  if (!validConversation(url) || !Number.isInteger(tabId)) throw new PairingError('Open an existing ChatGPT conversation.');
  pairReason = 'tab-moved';
  if ((await chrome.tabs.get(tabId)).url !== url) throw new PairingError('Tab moved; verify again.');
  pairReason = 'mailbox-unavailable';
  const observed = await mailbox(name);
  pairReason = 'identity-changed';
  if (observed.endpoint !== endpoint || observed.generation !== generation) throw new PairingError('Mailbox changed; verify again.');
  if ((await chrome.tabs.get(tabId)).url !== url) throw new PairingError('Tab moved; verify again.');
  return await persist(async () => {
    const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
    const prior = wakeBindings.find(binding => binding.endpoint === endpoint);
    pairReason = 'conversation-already-bound';
    if (wakeBindings.some(binding => binding.endpoint !== endpoint && binding.url === url)) {
      throw new PairingError('Conversation already belongs to another mailbox.');
    }
    pairReason = 'generation-changed';
    if (prior && (prior.generation !== generation || prior.url !== url)) throw new PairingError('Existing pairing identity differs.');
    pairReason = 'unresolved-attempt';
    if (prior && unresolved(prior)) {
      throw new PairingError('Unresolved attempt preserved; pairing unchanged.');
    }
    pairReason = 'stop-requested';
    if (stopVersion !== version || endpointStops.get(endpoint) !== endpointVersion) throw new PairingError('Stop requested during pairing.');
    const binding = {...prior, endpoint, generation, name: observed.name, tabId, url,
      armed: true, seen: prior?.seen || [], pending: observed.delivery_ids.slice(), status: 'paired',
      pairDiagnostic: {id: crypto.randomUUID(), release: releaseId, boot: bootId, at: new Date().toISOString(),
        result: 'accepted', reason: 'accepted', attempt: prior?.diagnostic?.id || null}};
    const index = wakeBindings.indexOf(prior);
    if (index < 0) wakeBindings.push(binding); else wakeBindings[index] = binding;
    await chrome.storage.local.set({wakeBindings});
    return 'Paired. Check manually to attempt one wake.';
  });
  } catch (error) {
    await persist(async () => {
      const current = await chrome.storage.local.get('wakeBindings');
      const live = current.wakeBindings?.find(item => item.endpoint === endpoint);
      if (!live) return;
      live.pairDiagnostic = {id: crypto.randomUUID(), release: releaseId, boot: bootId,
        at: new Date().toISOString(), result: 'refused', reason: pairReason, attempt: live.diagnostic?.id || null};
      await chrome.storage.local.set(current);
    }).catch(() => {});
    throw error;
  }
}
async function check(version) {
  const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
  const results = [];
  for (const binding of wakeBindings) {
    try { if (binding.armed) results.push(await checkBinding(binding, version)); } catch (error) {
      if (wakeBindings.length === 1) throw error;
      results.push(error instanceof PairingError ? error.message : 'Mailbox observation unavailable.');
    } finally { await diagnosticReport(binding).catch(() => {}); }
  }
  return results.join('\n') || 'No active pairing.';
}
async function conversationTab(binding) {
  const tabs = (await chrome.tabs.query({url: 'https://chatgpt.com/*'}))
    .filter(tab => Number.isInteger(tab.id) && tab.url === binding.url);
  return tabs.find(tab => tab.id === binding.tabId) || tabs.sort((a, b) => a.id - b.id)[0];
}
async function availability(binding, open) {
  return persist(async () => {
    const current = await chrome.storage.local.get('wakeBindings');
    const live = current.wakeBindings?.find(item => item.endpoint === binding.endpoint);
    if (live?.generation !== binding.generation || live?.url !== binding.url) return;
    live.availability = open ? 'open' : 'closed';
    await chrome.storage.local.set(current);
  });
}
async function checkBinding(binding, version) {
  const endpointVersion = endpointStops.get(binding.endpoint);
  if (!validConversation(binding.url)) return 'Saved conversation is invalid; no wake attempted.';
  const tab = await conversationTab(binding);
  await availability(binding, Boolean(tab));
  if (unresolved(binding)) {
    const control = {release: releaseId, boot: bootId, reason: 'unavailable'};
    if (tab && !tab.discarded && !tab.frozen) {
      try {
        const receipt = (await chrome.scripting.executeScript({target: {tabId: tab.id}, func: nudge,
          args: [binding.url, 'control', null, true]}))[0]?.result;
        control.reason = receipt?.reason === 'dom-exception' ? 'unavailable'
          : receipt?.facts?.controlSelection || (receipt?.outcome === 'wrong-conversation' ? 'url-mismatch' : 'editor-unavailable');
      } catch { /* Read-only snapshot failure never changes the protected attempt. */ }
    }
    await persist(async () => {
      const current = await chrome.storage.local.get('wakeBindings');
      const live = current.wakeBindings?.find(item => item.endpoint === binding.endpoint);
      if (live?.generation !== binding.generation || live.url !== binding.url) return;
      live.currentControl = control;
      await chrome.storage.local.set(current);
    });
    return 'Previous attempt needs attention; it was not repeated.';
  }
  const matching = observed => observed.endpoint === binding.endpoint && observed.generation === binding.generation;
  const cancelled = () => stopVersion !== version || endpointStops.get(binding.endpoint) !== endpointVersion;
  const observed = await mailbox(binding.name);
  if (!matching(observed)) throw new PairingError('Mailbox changed; no wake attempted.');
  const ids = observed.delivery_ids.filter(id => !binding.seen.includes(id));
  if (!ids.length) return 'No new pending mail.';
  if (!tab || tab.discarded || tab.frozen) return 'Open the paired conversation; no wake attempted.';
  if (cancelled()) return 'Stopped.';
  const invoke = async attempt => (await chrome.scripting.executeScript({
    target: {tabId: tab.id}, func: nudge, args: [binding.url, false, attempt, true]
  }))[0]?.result;
  let ready = await invoke({prepare: true});
  if (!ready?.sendReady && !ready?.stopReady && !ready?.prepareReady) return 'Chat not ready; existing draft left unchanged.';
  await persist(async () => {
    const current = await chrome.storage.local.get('wakeBindings');
    const live = current.wakeBindings?.find(item => item.endpoint === binding.endpoint);
    if (!live?.armed || cancelled() || live.halted ||
        live.endpoint !== binding.endpoint || live.generation !== binding.generation || live.url !== binding.url) {
      throw new PairingError('Pairing stopped or changed; no wake attempted.');
    }
    Object.assign(live, {halted: true, attemptIds: ids, status: 'attempt-outcome-unknown',
      diagnostic: {id: crypto.randomUUID(), boot: bootId, release: releaseId,
        endpoint: binding.endpoint, generation: binding.generation, delivery_ids: ids.slice(0, 50),
        started: new Date().toISOString(), finished: null, outcome: null, reason: 'dispatch-start', facts: {}}});
    await chrome.storage.local.set(current); // Must succeed before Stop, insertion or Send.
  });
  const complete = (outcome, receipt = ready, reason, error) => persist(async () => {
    const current = await chrome.storage.local.get('wakeBindings');
    const live = current.wakeBindings?.find(item => item.endpoint === binding.endpoint);
    if (!live || live.generation !== binding.generation || live.url !== binding.url) return 'Pairing changed.';
    live.status = outcome;
    diagnosticResult(live, outcome, receipt, reason, error);
    if (outcome === 'click-attempted') {
      live.seen = [...new Set([...live.seen, ...ids])];
      live.halted = false;
      delete live.attemptIds;
    }
    await chrome.storage.local.set(current);
    return outcome === 'click-attempted' ? 'Wake click attempted; acceptance is not confirmed.' : 'Wake attempt needs attention; it was not repeated.';
  });
  try {
    const needsStop = ready.stopReady;
    let inserted = false;
    for (let tick = 0; tick < 100; tick++) {
      if (cancelled()) return complete('stopped', ready, 'stopped-before-dispatch');
      ready = await invoke({prepare: true, insert: true, stop: needsStop && tick === 0});
      inserted ||= Boolean(ready?.inserted);
      if (inserted && ready?.facts) ready.facts.inserted = true;
      if (ready?.sendReady) break;
      if (typeof ready?.sendReady !== 'boolean') return complete('readiness-refused');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (!ready?.sendReady) return complete('readiness-timeout');
    const current = await mailbox(binding.name);
    if (!matching(current) || !current.delivery_ids.some(id => ids.includes(id))) return complete('pending-changed');
    if (cancelled()) return complete('stopped', ready, 'stopped-before-dispatch');
    const receipt = await invoke({final: true, inserted}); // Final DOM checks and click contain no wait.
    return complete(receipt?.outcome === 'click-attempted' ? 'click-attempted' : 'attempt-outcome-unknown', receipt, receipt ? undefined : 'missing-result');
  } catch (error) { return complete('attempt-outcome-unknown', ready, 'execution-failed', error); }
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return;
  const version = stopVersion;
  const endpointVersion = endpointStops.get(message.endpoint);
  let result;
  if (message.type === 'disarm') {
    if (message.endpoint && !UUID.test(message.endpoint)) { reply({error: 'Invalid mailbox identity.'}); return; }
    if (message.endpoint) endpointStops.set(message.endpoint, (endpointVersion || 0) + 1);
    else ++stopVersion;
    // Storage writes retain order, but a held backend request never delays Stop.
    result = persist(async () => {
      const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
      for (const binding of wakeBindings) {
        if (!message.endpoint || binding.endpoint === message.endpoint) binding.armed = false;
      }
      await chrome.storage.local.set({wakeBindings});
      return 'Stopped.';
    });
  } else {
    result = serial.then(() => action(message, version, endpointVersion));
    serial = result.catch(() => {});
  }
  result.then(status => reply({status}), error => reply({error: error instanceof PairingError
    ? error.message : 'Could not update pairing. Check the mailbox connection and try again.'}));
  return true;
});

// Alarms only request the ordinary check; they never renew a consumed attempt.
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name !== 'mailbox-wake') return;
  const version = stopVersion;
  const result = serial.then(() => check(version));
  serial = result.catch(() => {});
});
chrome.alarms.get('mailbox-wake').then(alarm => {
  if (!alarm) return chrome.alarms.create('mailbox-wake', {periodInMinutes: 0.5});
}).catch(() => console.warn('Automatic mailbox watch unavailable.'));

// Closing a tab only records availability; exact matching tabs are never opened here.
chrome.tabs.onRemoved.addListener(() => {
  const result = serial.then(async () => {
    const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
    for (const binding of wakeBindings) await availability(binding, Boolean(await conversationTab(binding)));
  });
  serial = result.catch(() => {});
});
