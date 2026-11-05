// This first feature verifies and saves one pairing; it never touches chat content.
const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let serial = Promise.resolve();
let stopVersion = 0;
function validConversation(value) {
  try {
    const url = new URL(value);
    return url.origin === 'https://chatgpt.com' && !url.username && !url.password &&
      /^\/(?:g\/[a-zA-Z0-9-]+\/)?c\/[a-zA-Z0-9-]+\/?$/.test(url.pathname);
  } catch { return false; }
}
async function mailbox(name) {
  const response = await fetch(SOURCE + '/poll?name=' + encodeURIComponent(name),
    {signal: AbortSignal.timeout(3000)});
  if (!response.ok) throw new Error('Mailbox service unavailable.');
  const observed = await response.json();
  if (observed.status !== 'ok' || !UUID.test(observed.endpoint) ||
      !Number.isInteger(observed.generation) || observed.generation < 1 ||
      typeof observed.name !== 'string' || !Array.isArray(observed.delivery_ids) ||
      !observed.delivery_ids.every(id => UUID.test(id))) throw new Error('Invalid mailbox identity.');
  return observed;
}
async function action(message, version) {
  if (message.type === 'verify') return mailbox(message.name);
  const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
  if (message.type === 'disarm') {
    for (const binding of wakeBindings) binding.armed = false;
    await chrome.storage.local.set({wakeBindings});
    return 'Stopped.';
  }
  if (message.type !== 'bind') throw new Error('Unknown action.');
  const {tabId, url, name, endpoint, generation} = message;
  if (!validConversation(url) || !Number.isInteger(tabId)) throw new Error('Open an existing ChatGPT conversation.');
  if ((await chrome.tabs.get(tabId)).url !== url) throw new Error('Tab moved; verify again.');
  const observed = await mailbox(name);
  if (observed.endpoint !== endpoint || observed.generation !== generation) throw new Error('Mailbox changed; verify again.');
  const prior = wakeBindings.find(binding => binding.endpoint === endpoint);
  if (wakeBindings.length > 1 || wakeBindings.some(binding => binding.endpoint !== endpoint)) {
    throw new Error('This version supports one saved pairing. Existing pairings are preserved.');
  }
  if (prior && (prior.generation !== generation || prior.url !== url)) throw new Error('Existing pairing identity differs.');
  if (prior?.halted || prior?.pendingSend || prior?.attemptIds?.length || prior?.diagnostic || prior?.stopSendAttempt) {
    throw new Error('Unresolved attempt preserved; pairing unchanged.');
  }
  if ((await chrome.tabs.get(tabId)).url !== url) throw new Error('Tab moved; verify again.');
  if (stopVersion !== version) throw new Error('Stop requested during pairing.');
  const binding = {...prior, endpoint, generation, name: observed.name, tabId, url,
    armed: true, seen: prior?.seen || [], pending: observed.delivery_ids.slice(), status: 'paired'};
  await chrome.storage.local.set({wakeBindings: [binding]});
  return 'Paired. This version does not send wakes.';
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return;
  if (message.type === 'disarm') ++stopVersion;
  const version = stopVersion;
  const result = serial.then(() => action(message, version));
  serial = result.catch(() => {});
  result.then(status => reply({status}), error => reply({error: error.message}));
  return true;
});
