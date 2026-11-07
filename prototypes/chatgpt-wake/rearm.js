const button = document.getElementById('rearm');
const status = document.getElementById('status');
button.onclick = async () => {
  button.disabled = true;
  try {
    const reply = await chrome.runtime.sendMessage({type: 'rearm-all'});
    if (reply.error) throw Error(reply.error);
    status.textContent = `Re-armed ${reply.count} chats for the next check.`;
  } catch (e) { status.textContent = `Re-arm not confirmed: ${e.message}`; }
  finally { button.disabled = false; }
};
