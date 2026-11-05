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
    for (const type of ['resume', 'remove']) {
      const button = document.createElement('button');
      button.textContent = type === 'resume' ? 'Resume' : 'Remove';
      button.onclick = () => act(async () => { await request({type, id: b.id}); await show(); });
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
show().catch(e => { $('status').textContent = e.message; });
