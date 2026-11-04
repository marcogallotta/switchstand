const output = document.querySelector('#status');
const pair = document.querySelector('#pairing');
const arm = document.querySelector('#bind');
let verified;
async function show() {
  const state = await chrome.storage.local.get(['status', 'wakeBindings']);
  output.textContent = state.status || 'No V1 pairings. Existing mock bindings are not imported.';
  const list = document.querySelector('#bindings');
  list.replaceChildren();
  for (const binding of state.wakeBindings || []) {
    const row = document.createElement('p');
    row.style.overflowWrap = 'anywhere';
    row.textContent = `${binding.name}: ${binding.armed ? 'armed' : 'stopped'}${binding.halted ? '; HALTED — reconcile attempt before retry' : ''}\n${binding.url}\n${binding.status}; last checked ${binding.checkedAt || 'never'}\n${binding.report || 'escalation unconfigured'}`;
    const stop = document.createElement('button');
    stop.textContent = 'Stop this chat';
    stop.onclick = async () => { await chrome.runtime.sendMessage({type: 'disarm', endpoint: binding.endpoint}); await show(); };
    row.append(document.createElement('br'), stop);
    list.append(row);
  }
}
document.querySelector('#name').oninput = () => { verified = undefined; arm.disabled = true; pair.textContent = ''; };
document.querySelector('#verify').onclick = async () => {
  arm.disabled = true;
  const result = await chrome.runtime.sendMessage({type: 'verify', name: document.querySelector('#name').value.trim()});
  if (result.error) { output.textContent = result.error; return; }
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  verified = {...result.status, tabId: tab?.id, url: tab?.url};
  pair.textContent = `Confirm ${verified.name} (generation ${verified.generation}) belongs to this chat: ${verified.url}`;
  arm.disabled = false;
};
arm.onclick = async () => {
  const result = await chrome.runtime.sendMessage({type: 'bind', ...verified});
  if (result.error) output.textContent = result.error;
  else { arm.disabled = true; await show(); }
};
for (const type of ['check', 'disarm']) document.getElementById(type).onclick = async () => {
  const result = await chrome.runtime.sendMessage({type});
  if (result.error) output.textContent = result.error;
  else await show();
};
show();
