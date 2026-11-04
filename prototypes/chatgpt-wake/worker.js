// Observe delivery IDs only; mailbox content remains in Switchstand.
function validConversation(value) {
  try {
    const url = new URL(value);
    return url.origin === 'https://chatgpt.com' && !url.username && !url.password &&
      /^\/(?:g\/[a-zA-Z0-9-]+\/)?c\/[a-zA-Z0-9-]+\/?$/.test(url.pathname);
  } catch { return false; }
}
let serial = Promise.resolve();
let stopAll = false;
const stopEndpoints = new Set();
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
async function nudge(expectedUrl) {
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
  if (draft().trim() || editor.querySelector('img') ||
      document.querySelector('[data-testid="remove-file-button"]')) return 'draft-present';
  const text = 'Process your mailbox';
  editor.focus();
  if (textarea) {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, text);
    editor.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
  } else if (!document.execCommand('insertText', false, text)) return 'insert-failed';
  const form = editor.closest('form');
  let send;
  for (let attempt = 0; attempt < 15; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
    send = form?.querySelector('[data-testid="send-button"], [data-testid="composer-submit-button"], #composer-submit-button, button[type="submit"], button[aria-label="Send prompt"]');
    if (send && !send.disabled && !/voice|dictat/i.test(send.getAttribute('aria-label') || '')) break;
  }
  if (location.href !== expectedUrl || draft().trim() !== text ||
      !send || send.disabled || /voice|dictat/i.test(send.getAttribute('aria-label') || '') ||
      document.querySelector('[data-testid="stop-button"]')) {
    const controls = [...(form?.querySelectorAll('button') || [])].slice(0, 5)
      .map(button => `id=${button.id},testid=${button.getAttribute('data-testid') || ''},type=${button.type},label=${button.getAttribute('aria-label') || ''},disabled=${button.disabled}`);
    return `inserted-not-submitted: no click; editor=${editor.tagName}[role=${editor.getAttribute('role') || ''}]; form=${Boolean(form)}; controls: ${controls.join('; ') || 'none'}`;
  }
  send.click();
  return 'click-attempted'; // A click is not a provider acceptance receipt.
}
const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let reportToken;
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
async function record(binding, outcome, ids = []) {
  binding.status = outcome;
  binding.checkedAt = new Date().toISOString();
  const kind = outcome.split(':')[0];
  if (['idle', 'armed', 'click-attempted', 'stopped'].includes(kind)) return;
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
    binding.report = 'logged; escalation unconfigured';
    binding.lastReport = reportKey;
  } catch (error) { binding.report = 'backend report unavailable: ' + error.message; }
}
async function checkBinding(binding, save) {
  if (!binding.armed || stopRequested(binding)) { binding.armed = false; return record(binding, 'stopped'); }
  let observation;
  try { observation = await mailbox(binding.name); }
  catch (error) { return record(binding, 'source-unavailable: ' + error.message); }
  if (observation.endpoint !== binding.endpoint || observation.generation !== binding.generation) {
    binding.armed = false;
    return record(binding, 'mailbox-binding-changed');
  }
  const pending = observation.delivery_ids.filter(id => !binding.seen.includes(id));
  binding.pending = pending;
  let tab;
  try { tab = await chrome.tabs.get(binding.tabId); }
  catch { binding.availability = 'closed'; return record(binding, 'bound-tab-missing', pending); }
  binding.availability = 'open';
  if (binding.halted) return record(binding, 'attempt-outcome-unknown', pending);
  if (!pending.length) return record(binding, 'idle');
  if (tab.url !== binding.url) return record(binding, 'wrong-conversation', pending);
  if (tab.discarded) return record(binding, 'tab-discarded', pending);
  if (tab.frozen) return record(binding, 'tab-frozen', pending);
  if (stopRequested(binding)) { binding.armed = false; return record(binding, 'stopped'); }
  // Persist a halt before DOM mutation. A crash cannot silently retry these IDs.
  binding.halted = true;
  binding.status = 'attempt-outcome-unknown';
  binding.attemptIds = pending;
  await save();
  if (stopRequested(binding)) {
    binding.armed = false; binding.halted = false; binding.attemptIds = [];
    return record(binding, 'stopped');
  }
  try {
    const [result] = await chrome.scripting.executeScript({target: {tabId: binding.tabId}, func: nudge, args: [binding.url]});
    const outcome = result?.result || 'attempt-outcome-unknown';
    const safe = ['busy', 'draft-present', 'wrong-conversation', 'insert-failed'].includes(outcome) || outcome.startsWith('composer-unavailable');
    if (safe) binding.halted = false;
    if (outcome === 'click-attempted') {
      binding.seen.push(...pending);
      binding.pending = [];
      binding.halted = false;
    }
    await record(binding, outcome, pending);
  } catch (error) { await record(binding, 'attempt-outcome-unknown: ' + error.message, pending); }
}
async function check() {
  const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
  const save = () => chrome.storage.local.set({wakeBindings});
  for (const binding of wakeBindings) {
    await checkBinding(binding, save);
    await save();
  }
  return status(wakeBindings.length ? 'checked; escalation unconfigured' : 'no V1 pairings; escalation unconfigured');
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return;
  // Stop intent preempts asynchronous checks before its persisted update is queued.
  if (message.type === 'disarm') {
    if (message.endpoint) stopEndpoints.add(message.endpoint);
    else stopAll = true;
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
      if (wakeBindings.some(item => item.endpoint !== endpoint && (item.url === url || item.tabId === tabId))) throw new Error('This chat is already paired with another mailbox.');
      const prior = wakeBindings.find(item => item.endpoint === endpoint);
      if (prior?.halted) throw new Error('Previous attempt is unresolved; reconcile before rearming.');
      if (prior && prior.generation !== generation) throw new Error('Mailbox generation changed; operator reconciliation required.');
      stopAll = false;
      stopEndpoints.delete(endpoint);
      const binding = {endpoint, generation, name: observed.name, tabId, url, armed: true,
        seen: prior?.seen || [], pending: [], status: 'armed', report: 'escalation unconfigured'};
      if (prior) wakeBindings[wakeBindings.indexOf(prior)] = binding;
      else wakeBindings.push(binding);
      await chrome.storage.local.set({wakeBindings});
      return status('armed; escalation unconfigured');
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
  for (const binding of wakeBindings) if (binding.tabId === tabId) {
    binding.availability = 'closed';
    await record(binding, 'bound-tab-missing', binding.pending || []);
  }
  await chrome.storage.local.set({wakeBindings});
}).catch(error => status(error.message)));
chrome.alarms.get('mailbox-wake').then(alarm => {
  if (!alarm) return chrome.alarms.create('mailbox-wake', {periodInMinutes: 0.5});
});
