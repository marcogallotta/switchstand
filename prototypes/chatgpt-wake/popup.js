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
    for (const type of ['resume', ...(b.uncertain ? ['rearm'] : []), ...(b.sendBudget?.auto === 3 && !b.sendBudget.manualUsed ? ['manual-send'] : []), 'remove']) {
      const button = document.createElement('button');
      button.textContent = {resume: 'Resume', rearm: 'Re-arm', 'manual-send': 'Send one extra wake', remove: 'Remove'}[type];
      const sendAction = async () => {
        const receipt = await request({type, id: b.id, expectedIncarnation: b.incarnation ?? null,
          ...(type === 'rearm' ? {expectedAttempt: b.wakeAttempt?.id || null} : {}),
          ...(type === 'manual-send' ? {expectedBatch: b.sendBudget.batchId} : {})});
        await show();
        return type === 'manual-send' ? `${b.name}: ${receipt.result}` : `${button.textContent}: ${b.name} saved`;
      };
      button.onclick = () => act(sendAction);
      row.append(button);
    }
    $('bindings').append(row);
  }
}
async function act(action) {
  try { $('status').textContent = await action() || ''; }
  catch (e) { $('status').textContent = e.message; }
}
$('add').onclick = () => act(async () => {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  await request({type: 'add', name: $('name').value.trim(), tabId: tab?.id, url: tab?.url});
  await show();
  return `Add chat: ${$('name').value.trim()} saved`;
});
$('resume-all').onclick = () => act(async () => { await request({type: 'resume-all'}); await show(); return 'Resume all saved'; });
$('rearm-all').onclick = () => act(async () => { await request({type: 'rearm-all'}); await show(); return 'Re-arm all saved'; });
show().catch(e => { $('status').textContent = e.message; });
