const $ = id => document.getElementById(id);
async function request(message) {
  const reply = await chrome.runtime.sendMessage(message);
  if (reply.error) throw Error(reply.error);
  return reply;
}
async function show() {
  const {minimalBindings = []} = await chrome.storage.local.get('minimalBindings');
  $('bindings').replaceChildren();
  for (const b of minimalBindings.filter(b => !b.removed).sort((a, b) => a.name.localeCompare(b.name) || a.url.localeCompare(b.url))) {
    const row = document.createElement('p');
    row.textContent = `${b.name}: ${b.status || 'Armed'} ${b.report || ''} `;
    for (const type of ['resume', ...(b.uncertain ? ['rearm'] : []), 'remove']) {
      const button = document.createElement('button');
      button.textContent = {resume: 'Resume', rearm: 'Re-arm', remove: 'Remove'}[type];
      const sendAction = async () => {
        await request({type, id: b.id, ...(type === 'rearm' ? {expectedAttempt: b.wakeAttempt?.id || null} : {})});
        await show();
      };
      button.onclick = () => act(async () => {
        if (type !== 'rearm') return sendAction();
        button.disabled = true;
        const warning = document.createElement('span');
        warning.style.display = 'block';
        warning.textContent = `Re-arm ${b.name} for future mail? An earlier wake may already have been sent. This does not prove delivery or mailbox processing, and sends nothing now. `;
        const accept = document.createElement('button'), cancel = document.createElement('button');
        accept.textContent = 'Confirm re-arm'; cancel.textContent = 'Cancel';
        accept.onclick = () => act(sendAction);
        cancel.onclick = () => act(show);
        warning.append(accept, cancel);
        row.append(warning);
      });
      row.append(button);
    }
    $('bindings').append(row);
  }
}
async function act(action) {
  try { await action(); $('status').textContent = ''; }
  catch (e) { $('status').textContent = e.message; }
}
$('add').onclick = () => act(async () => {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  await request({type: 'add', name: $('name').value.trim(), tabId: tab?.id, url: tab?.url});
  await show();
});
$('resume-all').onclick = () => act(async () => { await request({type: 'resume-all'}); await show(); });
$('rearm-all').onclick = () => act(async () => { await request({type: 'rearm-all'}); await show(); });
show().catch(e => { $('status').textContent = e.message; });
