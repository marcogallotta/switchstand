// Exact verified pairings; manual and alarm checks share the same durable wake reservation.
const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let serial = Promise.resolve();
let writes = Promise.resolve();
let stopVersion = 0;
const endpointStops = new Map();
class PairingError extends Error {}
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
async function nudge(expectedUrl, observeOnly = false, attempt = {}, diagnostic = false) {
  const result = outcome => diagnostic ? {outcome} : outcome;
  if (location.href !== expectedUrl) return result('wrong-conversation');
  const editor = document.getElementById('prompt-textarea');
  const form = editor?.closest('form');
  if (!form || !editor.getClientRects().length) return result('composer-unavailable');
  const textarea = editor instanceof HTMLTextAreaElement;
  if ((!textarea && !editor.isContentEditable) || editor.disabled || editor.readOnly) return result('composer-unavailable');
  const text = 'Process your mailbox';
  const draft = () => textarea ? editor.value : editor.textContent;
  const attached = () => editor.querySelector('img') || document.querySelector('[data-testid="remove-file-button"]');
  const exact = attempt.insert || attempt.final;
  if (attached() || (exact ? draft() && draft() !== text : draft().trim())) return result('draft-present');
  const visible = selector => [...form.querySelectorAll(selector)].filter(button => {
    const rect = button.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && getComputedStyle(button).visibility === 'visible';
  });
  const enabled = button => button && !button.disabled && !button.matches(':disabled') &&
    button.getAttribute('aria-disabled')?.toLowerCase() !== 'true';
  const stops = visible('[data-testid="stop-button"],button[aria-label="Stop" i],button[aria-label="Stop streaming" i]');
  if (stops.length > 1) return result('composer-unavailable');
  if (attempt.prepare && stops.length) {
    const stopReady = enabled(stops[0]);
    if (attempt.stop && stopReady) stops[0].click();
    return {outcome: 'busy', stopReady, sendReady: false, prepareReady: false};
  }
  if (stops.length) return result('busy');
  if (attempt.prepare && attempt.insert && !draft()) {
    editor.focus();
    if (textarea) {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, text);
      editor.dispatchEvent(new InputEvent('input', {bubbles: true, data: text, inputType: 'insertText'}));
    } else if (!document.execCommand('insertText', false, text)) return result('insert-failed');
  }
  const sends = visible('[data-testid="send-button"],[data-testid="composer-submit-button"],#composer-submit-button,button[aria-label="Send prompt"],button[aria-label="Send" i]');
  const send = sends.length === 1 ? sends[0] : undefined;
  if (attempt.prepare) return {outcome: 'idle', stopReady: false,
    prepareReady: !draft() && sends.length < 2, sendReady: Boolean(send && (!attempt.insert || enabled(send))),
    inserted: Boolean(attempt.insert && draft() === text)};
  if (observeOnly) return result('idle');
  if (!attempt.final || location.href !== expectedUrl || draft() !== text || attached() || !enabled(send)) {
    return result('inserted-not-submitted');
  }
  send.click();
  return result('click-attempted');
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
  if (!validConversation(url) || !Number.isInteger(tabId)) throw new PairingError('Open an existing ChatGPT conversation.');
  if ((await chrome.tabs.get(tabId)).url !== url) throw new PairingError('Tab moved; verify again.');
  const observed = await mailbox(name);
  if (observed.endpoint !== endpoint || observed.generation !== generation) throw new PairingError('Mailbox changed; verify again.');
  if ((await chrome.tabs.get(tabId)).url !== url) throw new PairingError('Tab moved; verify again.');
  return persist(async () => {
    const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
    const prior = wakeBindings.find(binding => binding.endpoint === endpoint);
    if (wakeBindings.some(binding => binding.endpoint !== endpoint && binding.url === url)) {
      throw new PairingError('Conversation already belongs to another mailbox.');
    }
    if (prior && (prior.generation !== generation || prior.url !== url)) throw new PairingError('Existing pairing identity differs.');
    if (prior?.halted || prior?.pendingSend || prior?.attemptIds?.length || prior?.diagnostic || prior?.stopSendAttempt) {
      throw new PairingError('Unresolved attempt preserved; pairing unchanged.');
    }
    if (stopVersion !== version || endpointStops.get(endpoint) !== endpointVersion) throw new PairingError('Stop requested during pairing.');
    const binding = {...prior, endpoint, generation, name: observed.name, tabId, url,
      armed: true, seen: prior?.seen || [], pending: observed.delivery_ids.slice(), status: 'paired'};
    const index = wakeBindings.indexOf(prior);
    if (index < 0) wakeBindings.push(binding); else wakeBindings[index] = binding;
    await chrome.storage.local.set({wakeBindings});
    return 'Paired. Check manually to attempt one wake.';
  });
}
async function check(version) {
  const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
  const results = [];
  for (const binding of wakeBindings) {
    if (!binding.armed) continue;
    try { results.push(await checkBinding(binding, version)); } catch (error) {
      if (wakeBindings.length === 1) throw error;
      results.push(error instanceof PairingError ? error.message : 'Mailbox observation unavailable.');
    }
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
  if (binding.halted || binding.attemptIds?.length || binding.pendingSend || binding.diagnostic || binding.stopSendAttempt) {
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
    Object.assign(live, {halted: true, attemptIds: ids, status: 'attempt-outcome-unknown'});
    await chrome.storage.local.set(current); // Must succeed before Stop, insertion or Send.
  });
  const complete = outcome => persist(async () => {
    const current = await chrome.storage.local.get('wakeBindings');
    const live = current.wakeBindings?.find(item => item.endpoint === binding.endpoint);
    if (!live || live.generation !== binding.generation || live.url !== binding.url) return 'Pairing changed.';
    live.status = outcome;
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
      if (cancelled()) return complete('stopped');
      ready = await invoke({prepare: true, insert: true, stop: needsStop && tick === 0});
      inserted ||= Boolean(ready?.inserted);
      if (ready?.sendReady) break;
      if (typeof ready?.sendReady !== 'boolean') return complete('readiness-refused');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (!ready?.sendReady) return complete('readiness-timeout');
    const current = await mailbox(binding.name);
    if (!matching(current) || !current.delivery_ids.some(id => ids.includes(id))) return complete('pending-changed');
    if (cancelled()) return complete('stopped');
    const receipt = await invoke({final: true, inserted}); // Final DOM checks and click contain no wait.
    return complete(receipt?.outcome === 'click-attempted' ? 'click-attempted' : 'attempt-outcome-unknown');
  } catch { return complete('attempt-outcome-unknown'); }
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
