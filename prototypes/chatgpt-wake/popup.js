const output = document.querySelector('#status');
const pair = document.querySelector('#pairing');
const arm = document.querySelector('#bind');
let verified;
function plainStatus(binding) {
  if (!binding.armed) return 'Stopped';
  if (binding.availability === 'closed') return 'Chat not open';
  if (binding.halted) return 'Needs attention';
  const state = (binding.status || '').split(':')[0];
  return ({busy: 'Waiting for response', 'draft-present': 'Draft needs attention',
    'click-attempted': 'Wake attempted', 'source-unavailable': 'Connection unavailable',
    'wrong-conversation': 'Open paired chat', 'mailbox-binding-changed': 'Pair again',
    'composer-unavailable': 'Chat unavailable'})[state] || 'Armed';
}
async function show() {
  const state = await chrome.storage.local.get(['status', 'wakeBindings', 'escalation', 'alertRecipient']);
  document.querySelector('#escalation').textContent = state.escalation === 'configured'
    ? `Alerts configured to ${state.alertRecipient || 'the fixed recipient'}.`
    : `Alerts ${state.escalation || 'not checked yet'}.`;
  output.textContent = state.escalation === 'unavailable' ? 'Cannot connect to the mailbox service.' : '';
  const list = document.querySelector('#bindings');
  list.replaceChildren();
  for (const binding of state.wakeBindings || []) {
    const row = document.createElement('p');
    row.style.overflowWrap = 'anywhere';
    row.textContent = `${binding.name}: ${plainStatus(binding)}`;
    row.append(document.createElement('br'));
    for (const [label, type] of [['Stop', 'disarm'], ['Remove', 'remove']]) {
      const button = document.createElement('button');
      button.textContent = label;
      button.onclick = async () => {
        const result = await chrome.runtime.sendMessage({type, endpoint: binding.endpoint});
        if (result.error) output.textContent = 'Could not update this chat. Try again.';
        else await show();
      };
      row.append(button);
    }
    list.append(row);
  }
}
document.querySelector('#name').oninput = () => { verified = undefined; arm.disabled = true; pair.textContent = ''; };
document.querySelector('#verify').onclick = async () => {
  arm.disabled = true;
  const result = await chrome.runtime.sendMessage({type: 'verify', name: document.querySelector('#name').value.trim()});
  if (result.error) { output.textContent = 'Could not verify this chat. Check its mailbox name and connection.'; return; }
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  verified = {...result.status, tabId: tab?.id, url: tab?.url};
  pair.textContent = `Confirm ${verified.name} (generation ${verified.generation}) belongs to this chat: ${verified.url}`;
  arm.disabled = false;
};
arm.onclick = async () => {
  const result = await chrome.runtime.sendMessage({type: 'bind', ...verified});
  if (result.error) output.textContent = 'Could not pair this chat. Verify it again.';
  else { arm.disabled = true; await show(); }
};
for (const type of ['check', 'disarm']) document.getElementById(type).onclick = async () => {
  const result = await chrome.runtime.sendMessage({type});
  if (result.error) output.textContent = 'Cannot connect to the mailbox service.';
  else await show();
};
show();
