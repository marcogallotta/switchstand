// This first feature verifies and saves one pairing; it never touches chat content.
const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let serial = Promise.resolve();
let writes = Promise.resolve();
let stopVersion = 0;
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
async function action(message, version) {
  if (message.type === 'verify') return mailbox(message.name);
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
    if (wakeBindings.length > 1 || wakeBindings.some(binding => binding.endpoint !== endpoint)) {
      throw new PairingError('This version supports one saved pairing. Existing pairings are preserved.');
    }
    if (prior && (prior.generation !== generation || prior.url !== url)) throw new PairingError('Existing pairing identity differs.');
    if (prior?.halted || prior?.pendingSend || prior?.attemptIds?.length || prior?.diagnostic || prior?.stopSendAttempt) {
      throw new PairingError('Unresolved attempt preserved; pairing unchanged.');
    }
    if (stopVersion !== version) throw new PairingError('Stop requested during pairing.');
    const binding = {...prior, endpoint, generation, name: observed.name, tabId, url,
      armed: true, seen: prior?.seen || [], pending: observed.delivery_ids.slice(), status: 'paired'};
    await chrome.storage.local.set({wakeBindings: [binding]});
    return 'Paired. This version does not send wakes.';
  });
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return;
  const version = stopVersion;
  let result;
  if (message.type === 'disarm') {
    ++stopVersion;
    // Storage writes retain order, but a held backend request never delays Stop.
    result = persist(async () => {
      const {wakeBindings = []} = await chrome.storage.local.get('wakeBindings');
      for (const binding of wakeBindings) binding.armed = false;
      await chrome.storage.local.set({wakeBindings});
      return 'Stopped.';
    });
  } else {
    result = serial.then(() => action(message, version));
    serial = result.catch(() => {});
  }
  result.then(status => reply({status}), error => reply({error: error instanceof PairingError
    ? error.message : 'Could not update pairing. Check the mailbox connection and try again.'}));
  return true;
});
