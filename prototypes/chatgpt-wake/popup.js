const output = document.querySelector('#status');
const pairing = document.querySelector('#pairing');
const confirm = document.querySelector('#bind');
let verified;
document.querySelector('#name').oninput = () => { verified = undefined; confirm.disabled = true; pairing.textContent = ''; };
document.querySelector('#verify').onclick = async () => {
  confirm.disabled = true;
  const result = await chrome.runtime.sendMessage({type: 'verify', name: document.querySelector('#name').value.trim()});
  if (result.error) { output.textContent = result.error; return; }
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  verified = {...result.status, tabId: tab?.id, url: tab?.url};
  pairing.textContent = `Confirm ${verified.name} (generation ${verified.generation}) belongs to this chat: ${verified.url}`;
  confirm.disabled = false;
};
confirm.onclick = async () => {
  const result = await chrome.runtime.sendMessage({type: 'bind', ...verified});
  output.textContent = result.error || result.status;
  confirm.disabled = true;
};
for (const type of ['disarm', 'check']) document.getElementById(type).onclick = async () => {
  const result = await chrome.runtime.sendMessage({type});
  output.textContent = result.error || result.status;
};
