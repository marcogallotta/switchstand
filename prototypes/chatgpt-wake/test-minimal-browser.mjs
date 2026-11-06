// Real MV3/browser boundary, intercepted fixture only. No ChatGPT network calls.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
import {createServer} from 'node:http';
import {createHash} from 'node:crypto';

const production = dirname(fileURLToPath(import.meta.url));
const extension = await mkdtemp(join(tmpdir(), 'switch-wake-extension-'));
const reports = [], observations = [];
const id = '11111111-2222-4333-8444-555555555555';
let pendingIds = [id];
const server = createServer(async (request, response) => {
  let input = '';
  for await (const chunk of request) input += chunk;
  const path = new URL(request.url, 'http://fixture').pathname;
  if (path.endsWith('/report')) {
    const payload = JSON.parse(input);
    (payload.observation ? observations : reports).push(payload);
  }
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify(path.endsWith('/config') ? {status:'ok',report_token:'isolated-fixture'} :
    path.endsWith('/report') ? {status:'logged',alert:{status:'queued'}} :
    {status:'ok',name:'mail',endpoint:id,generation:1,delivery_ids:pendingIds}));
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const port = server.address().port;
for (const file of ['manifest.json', 'popup.html', 'popup.js', 'worker.js']) {
  const original = await readFile(join(production, file), 'utf8');
  const fixture = file === 'worker.js' ? original.replace('127.0.0.1:18766', `127.0.0.1:${port}`) : original;
  assert.equal(fixture.replace(`127.0.0.1:${port}`, '127.0.0.1:18766'), original);
  await writeFile(join(extension, file), fixture);
  console.log('Production SHA256', file, createHash('sha256').update(original).digest('hex'));
}
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
  const fixture = `<!doctype html><form><textarea id="prompt-textarea"></textarea>
    <button type="button" data-testid="stop-button" aria-label="Stop">Stop</button></form><script>
    window.submissions=[];window.stops=0;
    const editor=document.querySelector('textarea'), button=document.querySelector('button');
    button.onclick=()=>{if(button.dataset.testid==='stop-button'){stops++;setTimeout(()=>{button.dataset.testid='send-button';button.disabled=true;button.removeAttribute('aria-label')},30)}
      else {submissions.push(editor.value);editor.value='';button.disabled=true}};
    editor.oninput=()=>setTimeout(()=>{button.disabled=!editor.value.trim()},30);
    </script>`;
  const url = 'https://chatgpt.com/c/11111111-2222-4333-8444-555555555555';
  const workerSession = await attach(worker.targetId);
  listeners.set('Fetch.requestPaused', (params, sessionId) => {
    command('Fetch.fulfillRequest', {requestId: params.requestId, responseCode: 200,
      responseHeaders: [{name:'Content-Type',value:'text/html'}],
      body:Buffer.from(fixture).toString('base64')},sessionId).catch(console.error);
  });
  await command('Fetch.enable',{patterns:[{urlPattern:'*'}]},session);
  await command('Page.navigate',{url},session);
  for(let attempt=0;attempt<50;attempt++) {
    if(await evaluate(session,'typeof submissions!=="undefined"')) break;
    await pause(50);
  }
  const extensionId = new URL(worker.url).host;
  const popup = await command('Target.createTarget',{url:`chrome-extension://${extensionId}/popup.html`});
  const popupSession = await attach(popup.targetId);
  for(let attempt=0;attempt<50;attempt++) {
    if(await evaluate(popupSession,`Boolean(document.getElementById('add')?.onclick)`)) break;
    await pause(50);
  }
  // Popup's actual active-tab lookup must select the exact intercepted conversation.
  await command('Target.activateTarget',{targetId:page.targetId});
  await evaluate(popupSession,`document.getElementById('name').value='mail';document.getElementById('add').click()`);
  for(let attempt=0;attempt<50;attempt++) {
    if(await evaluate(workerSession,`load().then(bs=>bs.length)`)) break;
    await pause(50);
  }
  const bindings = await evaluate(workerSession,`load()`);
  assert.equal(bindings.length,1,'one Add chat click verifies and stores pairing');
  await evaluate(popupSession,`document.getElementById('add').click()`);
  await pause(100);
  assert.match(await evaluate(popupSession,`document.getElementById('status').textContent`),/already added/);
  assert.equal(await evaluate(workerSession,`load().then(bs=>bs.length)`),1,'duplicate Add cannot overwrite pairing');
  await evaluate(workerSession,`chrome.alarms.create('poll',{when:Date.now()+100})`);
  for(let attempt=0;attempt<100;attempt++) {
    if((await evaluate(session,'submissions.length'))===1) break;
    await pause(50);
  }
  assert.deepEqual(await evaluate(session,'submissions'),['Process your mailbox']);
  assert.equal(await evaluate(session,'stops'),1,'one Stop before enabled Send');
  await evaluate(session,`document.querySelector('textarea').value='protected draft'`);
  await evaluate(workerSession,`load().then(check)`);
  assert.equal(await evaluate(session,`document.querySelector('textarea').value`),'protected draft');
  await evaluate(session,`document.querySelector('textarea').value=''`);
  await evaluate(workerSession,`load().then(check)`);
  const before = await evaluate(session,'submissions.length');
  await evaluate(workerSession,`load().then(check)`);
  assert.equal(await evaluate(session,'submissions.length'),before,'uncertain click is not sent again');
  // Stop the actual MV3 worker process, then wake its replacement through the popup.
  // A fresh execution context must reload the persisted uncertain click fence.
  await evaluate(workerSession,`globalThis.__restartFenceProbe = true`);
  let versions = [];
  listeners.set('ServiceWorker.workerVersionUpdated', params => { versions = params.versions; });
  await command('ServiceWorker.enable', {}, session);
  for (let attempt = 0; attempt < 50 && !versions.some(v => v.scriptURL === worker.url && v.runningStatus === 'running'); attempt++) await pause(100);
  const version = versions.find(v => v.scriptURL === worker.url && v.runningStatus === 'running');
  assert.ok(version, 'running MV3 worker version found');
  await command('Target.detachFromTarget', {sessionId: workerSession});
  await command('ServiceWorker.stopWorker', {versionId: version.versionId}, session);
  for (let attempt = 0; attempt < 50 && !versions.some(v => v.versionId === version.versionId && v.runningStatus === 'stopped'); attempt++) await pause(100);
  assert.ok(versions.some(v => v.versionId === version.versionId && v.runningStatus === 'stopped'), 'MV3 worker stopped before restart');
  const resumedAfterRestart = await evaluate(popupSession,`chrome.runtime.sendMessage({type:'resume-all'})`);
  assert.deepEqual(resumedAfterRestart, {}, 'popup wakes the restarted extension worker');
  let replacement;
  for (let attempt = 0; attempt < 50; attempt++) {
    replacement = (await command('Target.getTargets')).targetInfos.find(item => item.type === 'service_worker' && item.url === worker.url);
    if (replacement && replacement.targetId !== worker.targetId) break;
    await pause(100);
  }
  assert.ok(replacement && replacement.targetId !== worker.targetId, 'real MV3 worker restarted with a new target');
  let restartedSession = await attach(replacement.targetId);
  assert.equal(await evaluate(restartedSession,`Boolean(globalThis.__restartFenceProbe)`),false,'restart created a fresh execution context');
  assert.equal(await evaluate(restartedSession,`load().then(bs=>bs[0].uncertain)`),true,'uncertain fence survived worker restart');
  assert.equal(await evaluate(session,'submissions.length'),before,'worker restart did not repeat the wake');
  await evaluate(session,`document.querySelector('form').outerHTML='<div id="composer-wrap" data-testid="composer"><textarea></textarea><button data-testid="send-button">Send</button></div>'`);
  const compose = action => evaluate(restartedSession,`(async()=>{const tab=(await chrome.tabs.query({})).find(t=>t.url===${JSON.stringify(url)});return (await chrome.scripting.executeScript({target:{tabId:tab.id},func:composer,args:[${JSON.stringify(url)},${JSON.stringify(action)}]}))[0].result})()`);
  assert.equal(await compose('prepare'),'waiting','id-less textarea without form is found through its unique Send control');
  await evaluate(session,`document.getElementById('composer-wrap').removeAttribute('data-testid');document.querySelector('textarea').value=''`);
  assert.equal(await compose('prepare'),'composer-unavailable','unidentified form is not mistaken for the ChatGPT composer');
  await evaluate(session,`document.getElementById('composer-wrap').setAttribute('data-testid','composer')`);
  await evaluate(session,`document.getElementById('composer-wrap').innerHTML='<button data-testid="stop-button">Stop</button>';window.stops=0;document.querySelector('button').onclick=()=>stops++`);
  assert.equal(await compose('prepare'),'busy','missing editor with one visible Stop is treated as busy');
  assert.equal(await evaluate(session,'stops'),1);
  await evaluate(session,`document.getElementById('composer-wrap').outerHTML=${JSON.stringify('<div id="thread-bottom-container"></div><div data-composer-body style="display:none"><div role="textbox" contenteditable="true"></div></div><div data-composer-body><div role="textbox" contenteditable="true"></div><button type="button" aria-label="Send">Send</button></div>')}`);
  await evaluate(session,`document.querySelector('button[aria-label="Send"]').onclick=()=>{const editor=document.querySelectorAll('[role="textbox"]')[1];submissions.push(editor.textContent);editor.textContent=''}`);
  assert.equal(await compose('prepare'),'waiting','current ChatGPT contenteditable composer accepts wake text');
  assert.equal(await compose('wait'),'ready','current ChatGPT Send control becomes ready');
  assert.equal(await compose('send'),'click-attempted','current ChatGPT Send control is clicked');
  assert.equal((await evaluate(session,'submissions')).at(-1),'Process your mailbox');
  // Execute the registered toolbar handler in the real MV3 worker. No UI modal.
  assert.equal(await evaluate(restartedSession,'chrome.action.onClicked.hasListener(rearmAll)'),true);
  assert.equal(JSON.parse(await readFile(join(production,'manifest.json'),'utf8')).action.default_popup,undefined);
  const priorRearm = (await evaluate(restartedSession,'load()'))[0];
  const sendsBeforeRearm = await evaluate(session,'submissions.length');
  await evaluate(restartedSession,`(async()=>{const bs=await load();bs.push({...bs[0],id:'other',armed:false,uncertain:true,alert:{retained:true}});bs.push({...bs[0],id:'removed',removed:true,armed:false});await save(bs);cancelled.add('other')})()`);
  const beforeAll=await evaluate(restartedSession,'load()');
  await evaluate(restartedSession,'rearmAll()');
  const afterAll=await evaluate(restartedSession,'load()');
  assert.deepEqual(afterAll,beforeAll.map(b=>b.removed?b:{...b,uncertain:false,armed:true,status:'Armed'}),'every active binding reset, all other history preserved');
  assert.equal(await evaluate(restartedSession,"cancelled.has('other')"),false);
  assert.equal(await evaluate(session,'submissions.length'),sendsBeforeRearm,'toolbar action does not send inline');
  await evaluate(restartedSession,'rearmAll()');
  assert.deepEqual(await evaluate(restartedSession,'load()'),afterAll,'repeated click is idempotent');
  await evaluate(restartedSession,`load().then(bs=>save(bs.filter(b=>b.id===${JSON.stringify(id)})))`);
  // Same still-pending batch can now wake; automatic repeat is fenced again.
  await evaluate(restartedSession,'load().then(check)');
  assert.equal(await evaluate(session,'submissions.length'),sendsBeforeRearm+1,'explicit re-arm recovers pending mail');
  await evaluate(restartedSession,'load().then(check)');
  assert.equal(await evaluate(session,'submissions.length'),sendsBeforeRearm+1,'automatic repeats remain fenced');
  await evaluate(restartedSession,'rearmAll()');
  const rearmVersion = versions.find(v=>v.scriptURL===worker.url && v.runningStatus==='running');
  assert.ok(rearmVersion);
  const oldRearmTarget = replacement.targetId;
  await command('Target.detachFromTarget',{sessionId:restartedSession});
  await command('ServiceWorker.stopWorker',{versionId:rearmVersion.versionId},session);
  for(let attempt=0;attempt<50 && !versions.some(v=>v.versionId===rearmVersion.versionId && v.runningStatus==='stopped');attempt++) await pause(100);
  assert.ok(versions.some(v=>v.versionId===rearmVersion.versionId && v.runningStatus==='stopped'));
  await evaluate(popupSession,`chrome.runtime.sendMessage({type:'not-a-request'})`);
  for(let attempt=0;attempt<50;attempt++) {
    replacement=(await command('Target.getTargets')).targetInfos.find(t=>t.type==='service_worker' && t.url===worker.url);
    if(replacement && replacement.targetId!==oldRearmTarget) break;
    await pause(100);
  }
  assert.ok(replacement && replacement.targetId!==oldRearmTarget);
  restartedSession=await attach(replacement.targetId);
  assert.equal((await evaluate(restartedSession,'load()'))[0].uncertain,false,'real worker restart retains re-arm');
  pendingIds=['22222222-2222-4222-8222-222222222222'];
  await evaluate(restartedSession,'load().then(check)');
  assert.equal(await evaluate(session,'submissions.length'),sendsBeforeRearm+2,'new batch wakes after restart');
  await evaluate(restartedSession,'load().then(check)');
  assert.equal(await evaluate(session,'submissions.length'),sendsBeforeRearm+2,'new uncertain send is fenced');
  await command('Target.closeTarget',{targetId:page.targetId});
  await evaluate(restartedSession,`load().then(check)`);
  assert.equal(reports.length,1,'closed exhausted binding sends immediate HTTP alert');
  assert.equal(reports[0].incident.reason,'bound-tab-missing');
  assert.ok(observations.some(p => p.observation.reason === 'bound-tab-missing'));
  assert.ok(observations.some(p => p.observation.attempt?.detail === 'click-dispatched'));
  assert.ok(observations.every(p => !p.incident && p.delivery_ids.length === 0));
  await evaluate(restartedSession,`load().then(check)`);
  assert.equal(reports.length,1,'HTTP alert dedupe persisted');
  const remove = await evaluate(popupSession,`chrome.runtime.sendMessage({type:'remove',id:${JSON.stringify(id)}})`);
  assert.match(remove.error,/Pending mail or alert/,'Remove cannot discard unresolved evidence');
  const resumed = await evaluate(popupSession,`chrome.runtime.sendMessage({type:'resume-all'})`);
  assert.deepEqual(resumed,{});
  assert.equal((await evaluate(restartedSession,'load()'))[0].alertDone,true,'Resume all preserves active alert');
  const rows = await evaluate(popupSession,`(async()=>{await chrome.storage.local.set({minimalBindings:[
    {id:'z',name:'Zulu',url:'https://chatgpt.com/c/z',armed:true},
    {id:'a',name:'Alpha',url:'https://chatgpt.com/c/a',armed:true}]});await show();
    return [...document.querySelectorAll('#bindings p')].map(row=>({text:row.textContent,buttons:[...row.querySelectorAll('button')].map(b=>b.textContent)}))})()`);
  assert.deepEqual(rows.map(row=>row.text.split(':')[0]),['Alpha','Zulu'],'saved chats are alphabetical');
  assert.deepEqual(rows[0].buttons,['Resume','Remove'],'Stop is absent from the popup');
  console.log('PASS isolated real MV3: one-click Add, duplicate rejection, Chrome storage, alarm/poll, Stop→Send, real worker restart/no repeat, protected draft, composer fallback, HTTP alert/dedupe, guarded Remove and Resume all. User browser/live backend: NOT_RUN.');
} catch (error) {
  console.error(error);
  throw error;
} finally {
  socket?.close();
  const ended = browser.exitCode === null ? once(browser, 'exit') : Promise.resolve();
  browser.kill('SIGTERM');
  await ended;
  server.close();
  await rm(extension, {recursive:true, force:true});
  await rm(profile, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
}
