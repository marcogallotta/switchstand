// Real MV3/browser boundary, intercepted fixture only. No ChatGPT network calls.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';

const extension = dirname(fileURLToPath(import.meta.url));
const profile = await mkdtemp(join(tmpdir(), 'switch-wake-fixture-'));
const browser = spawn(process.env.BROWSER || '/snap/brave/current/opt/brave.com/brave/brave', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--no-first-run', '--disable-dev-shm-usage',
  '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
  `--disable-extensions-except=${extension}`, `--load-extension=${extension}`, 'about:blank'
], {stdio: ['ignore', 'ignore', 'pipe']});
let stderr = '';
browser.stderr.on('data', data => { stderr += data; });
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let socket;
try {
  let lines;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { lines = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).trim().split('\n'); break; }
    catch { if (browser.exitCode !== null) throw new Error(stderr); await pause(100); }
  }
  if (!lines) throw new Error(`browser startup timeout: ${stderr}`);
  socket = new WebSocket(`ws://127.0.0.1:${lines[0]}${lines[1]}`);
  await once(socket, 'open');
  let sequence = 0;
  const calls = new Map();
  const listeners = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const callback = calls.get(message.id);
      calls.delete(message.id);
      message.error ? callback.reject(new Error(JSON.stringify(message.error))) : callback.resolve(message.result);
    } else listeners.get(message.method)?.(message.params, message.sessionId);
  });
  function command(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      calls.set(id, {resolve, reject});
      socket.send(JSON.stringify({id, method, params, sessionId}));
    });
  }
  async function attach(targetId) {
    return (await command('Target.attachToTarget', {targetId, flatten: true})).sessionId;
  }
  async function evaluate(session, expression) {
    const answer = await command('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}, session);
    if (answer.exceptionDetails) throw new Error(JSON.stringify(answer.exceptionDetails));
    return answer.result.value;
  }
  let worker;
  for (let attempt = 0; attempt < 50; attempt++) {
    worker = (await command('Target.getTargets')).targetInfos.find(item => item.type === 'service_worker' && item.url.startsWith('chrome-extension://'));
    if (worker) break;
    await pause(100);
  }
  assert.ok(worker, 'MV3 worker loaded');
  const page = await command('Target.createTarget', {url: 'about:blank'});
  const session = await attach(page.targetId);
  const fixture = `<!doctype html><form><div id="prompt-textarea" contenteditable=""></div>
    <button type="button" data-testid="voice-button">Voice</button></form><script>
    window.submissions=[];
    const currentEditor=()=>document.querySelector('#prompt-textarea') || document.querySelector('form [role="textbox"]');
    const text=()=>currentEditor().tagName==='TEXTAREA'?currentEditor().value:currentEditor().textContent;
    const send=document.querySelector('button');
    document.addEventListener('input',()=>setTimeout(()=>{send.disabled=!text().trim();send.dataset.testid=text().trim()?'send-button':'voice-button'},30));
    send.onclick=()=>{submissions.push(text());currentEditor().value='';currentEditor().textContent='';send.disabled=true;send.dataset.testid='voice-button'};
    </script>`;
  listeners.set('Fetch.requestPaused', (params, sessionId) => {
    command('Fetch.fulfillRequest', {requestId: params.requestId, responseCode: 200,
      responseHeaders: [{name: 'Content-Type', value: 'text/html'}], body: Buffer.from(fixture).toString('base64')}, sessionId).catch(console.error);
  });
  await command('Fetch.enable', {patterns: [{urlPattern: '*'}]}, session);
  const url = 'https://chatgpt.com/c/11111111-2222-4333-8444-555555555555';
  await command('Page.navigate', {url}, session);
  for (let attempt = 0; attempt < 50; attempt++) {
    if (await evaluate(session, 'typeof submissions !== "undefined"')) break;
    await pause(100);
  }
  const workerSession = await attach(worker.targetId);
  const nudge = () => evaluate(workerSession, `(async()=>{const tab=(await chrome.tabs.query({})).find(t=>t.url===${JSON.stringify(url)});globalThis.fixtureTabId=tab.id;return (await chrome.scripting.executeScript({target:{tabId:tab.id},func:nudge,args:[${JSON.stringify(url)}]}))[0].result})()`);
  const diagnostic = await evaluate(workerSession, `(async()=>{const tab=(await chrome.tabs.query({})).find(t=>t.url===${JSON.stringify(url)});return (await chrome.scripting.executeScript({target:{tabId:tab.id},func:nudge,args:[${JSON.stringify(url)},false,undefined,true]}))[0].result})()`);
  assert.equal(diagnostic.outcome, 'click-attempted');
  assert.deepEqual(diagnostic.facts, {urlMatched: true, editorFound: true, busy: false,
    draftPresent: false, inserted: true, controlPresent: true, controlEnabled: true, clickDispatched: true});
  // Original cases below still receive string outcomes through the unchanged default call.
  await evaluate(session, 'submissions=[]');
  assert.equal(await nudge(), 'click-attempted');
  assert.deepEqual(await evaluate(session, 'submissions'), ['Process your mailbox']);
  await evaluate(session, `document.querySelector('#prompt-textarea').textContent='my draft'`);
  assert.equal(await nudge(), 'draft-present');
  assert.equal(await evaluate(session, `document.querySelector('#prompt-textarea').textContent`), 'my draft');
  await evaluate(session, `document.querySelector('#prompt-textarea').textContent='';document.querySelector('button').dataset.testid='stop-button'`);
  assert.equal(await nudge(), 'busy');
  await evaluate(session, `document.querySelector('button').dataset.testid='voice-button';document.querySelector('#prompt-textarea').outerHTML='<div role="textbox" contenteditable="true"><span role="textbox" contenteditable="true"></span></div>'`);
  assert.equal(await nudge(), 'click-attempted', 'unique outer no-id composer, delayed voice-to-send');
  await evaluate(session, `document.querySelector('form').insertAdjacentHTML('beforeend','<div id="extra" role="textbox" contenteditable="true"></div>')`);
  assert.match(await nudge(), /2 editable form textboxes.*ambiguous/);
  await evaluate(session, `document.querySelector('#extra').remove();document.querySelector('form [role="textbox"]').outerHTML='<textarea id="prompt-textarea">my textarea draft</textarea>'`);
  assert.equal(await nudge(), 'draft-present');
  await evaluate(session, `document.querySelector('textarea').value=''`);
  assert.equal(await nudge(), 'click-attempted');
  assert.equal(await evaluate(session, 'submissions.length'), 3);
  await evaluate(session, `history.pushState({},'', '/c/other-chat')`);
  // Execute with the original URL, independent of tab lookup after navigation.
  const wrong = await evaluate(workerSession, `(async()=>{return (await chrome.scripting.executeScript({target:{tabId:globalThis.fixtureTabId},func:nudge,args:[${JSON.stringify(url)}]}))[0].result})()`);
  assert.equal(wrong, 'wrong-conversation');
  await evaluate(session, `history.pushState({},'', ${JSON.stringify(url)});document.querySelector('button').remove()`);
  assert.match(await nudge(), /inserted-not-submitted: no click/);
  assert.equal(await evaluate(session, 'submissions.length'), 3);
  assert.equal(await evaluate(session, `document.querySelector('textarea').value`), 'Process your mailbox');
  console.log('PASS isolated MV3 composer: delayed voice-to-send, draft/busy, no-ID editor, ambiguity, textarea, exact URL and retained unsent draft. Real ChatGPT/background operation: NOT_RUN.');
} catch (error) {
  console.error(error);
  throw error;
} finally {
  socket?.close();
  const ended = browser.exitCode === null ? once(browser, 'exit') : Promise.resolve();
  browser.kill('SIGTERM');
  await ended;
  await rm(profile, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
}
