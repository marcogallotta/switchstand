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
    {status:'ok',name:'mail',endpoint:id,generation:1,delivery_ids:pendingIds, available_delivery_ids: pendingIds}));
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
    <button type="button" data-testid="send-button" disabled>Send</button></form><script>
    window.submissions=[];window.stops=0;
    const editor=document.querySelector('textarea'), button=document.querySelector('button');
    button.onclick=()=>{submissions.push(editor.value);editor.value='';button.disabled=true};
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
  await evaluate(workerSession, 'chrome.action.openPopup()');
  const addPopup = (await command('Target.getTargets')).targetInfos.find(t => t.url.endsWith('/popup.html') && t.targetId !== popup.targetId);
  assert.ok(addPopup, 'real toolbar popup opened for Add chat');
  const addPopupSession = await attach(addPopup.targetId);
  for (let attempt=0;attempt<50 && !await evaluate(addPopupSession, "Boolean(document.getElementById('add')?.onclick)");attempt++) await pause(50);
  assert.equal(await evaluate(addPopupSession, "chrome.extension.getViews({type:'popup'}).includes(window)"), true, 'Add chat runs in the real toolbar popup');
  await evaluate(addPopupSession,`document.getElementById('name').value='mail';document.getElementById('add').click()`);
  for(let attempt=0;attempt<50;attempt++) {
    if(await evaluate(workerSession,`load().then(bs=>bs.length)`)) break;
    await pause(50);
  }
  const bindings = await evaluate(workerSession,`load()`);
  assert.equal(bindings.length,1,'one Add chat click verifies and stores pairing');
  assert.equal(await evaluate(addPopupSession, `document.getElementById('status').textContent`),'Add chat: mail saved');
  assert.ok(await evaluate(addPopupSession, 'innerWidth >= 600'), 'actual action popup is wider');
  await evaluate(addPopupSession,`document.getElementById('add').click()`);
  await pause(100);
  assert.match(await evaluate(addPopupSession,`document.getElementById('status').textContent`),/already added/);
  assert.equal(await evaluate(workerSession,`load().then(bs=>bs.length)`),1,'duplicate Add cannot overwrite pairing');
  await command('Target.closeTarget', {targetId:addPopup.targetId});
  await evaluate(workerSession,`chrome.alarms.create('poll',{when:Date.now()+100})`);
  for(let attempt=0;attempt<100;attempt++) {
    if((await evaluate(session,'submissions.length'))===1) break;
    await pause(50);
  }
  assert.deepEqual(await evaluate(session,'submissions'),['Process your mailbox'],'initial alarm dispatches exactly one wake');
  assert.equal(await evaluate(session,'stops'),0,'wake never clicks Stop');
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
  assert.equal(await evaluate(restartedSession,`load().then(bs=>bs[0].retry.attempts)`),1,'reservation survived worker restart');
  assert.equal(await evaluate(session,'submissions.length'),before,'worker restart did not repeat the wake');
  // Exercise the actual serialized function against hostile DOM controls.
  const stopControl = attributes => `<button type="button" class="stop" data-testid="stop-button" ${attributes}>Stop</button>`;
  const sendControl = attributes => `<button type="button" class="send" data-testid="send-button" ${attributes}>Send</button>`;
  const controlCases = [
    ['no Stop','',sendControl(''),'send','click-attempted',0,1],
    ['one Stop',stopControl(''),sendControl(''),'prepare','busy',0,0],
    ['hidden Stop',stopControl('style="visibility:hidden"'),sendControl(''),'send','click-attempted',0,1],
    ['multiple Stops',stopControl('')+stopControl(''),sendControl(''),'prepare','busy',0,0],
    ...['disabled','aria-disabled="true"'].map(a=>['Stop '+a,stopControl(a),sendControl(''),'prepare','busy',0,0]),
    ['multiple Sends','',sendControl('')+sendControl(''),'send','waiting',0,0],
    ...['disabled','aria-disabled="true"','style="visibility:hidden"'].map(a=>['Send '+a,'',sendControl(a),'send','waiting',0,0]),
    ['unrelated draft',stopControl(''),sendControl(''),'prepare','draft-present',0,0,'KEEP MY DRAFT'],
    ['attachment',stopControl(''),sendControl(''),'prepare','draft-present',0,0,'Process your mailbox','<button data-testid="remove-file-button">file</button>'],
    ['wrong URL',stopControl(''),sendControl(''),'send','wrong-conversation',0,0]
  ];
  const missingEditorCases = [['enabled','', 'busy',0],['hidden','style="visibility:hidden"','composer-unavailable',0],
    ['multiple',null,'composer-unavailable',0],['disabled','disabled','busy',0],['aria-disabled','aria-disabled="true"','busy',0]];
  for (const kind of ['textarea','contenteditable','missing']) {
    const cases = kind === 'missing' ? missingEditorCases.map(([name,a,result,stops])=>[name, a===null?stopControl('')+stopControl(''):stopControl(a),'','prepare',result,stops,0]) : controlCases;
    for (const [name,stops,sends,action,result,stopCount,sendCount,draft='Process your mailbox',attachment=''] of cases) {
      const editor = kind==='missing'?'':kind==='textarea'?`<textarea id="prompt-textarea">${draft}</textarea>`:`<div id="prompt-textarea" contenteditable="true">${draft}</div>`;
      await evaluate(session,`document.body.innerHTML=${JSON.stringify('<form data-testid="composer">'+editor+stops+sends+attachment+'</form>')};window.effects={stop:0,send:0};document.querySelectorAll('.stop,.send').forEach(b=>b.onclick=()=>effects[b.className]++)`);
      const observed = await evaluate(restartedSession,`(async()=>{const tab=(await chrome.tabs.query({})).find(t=>t.url===${JSON.stringify(url)});return (await chrome.scripting.executeScript({target:{tabId:tab.id},func:composer,args:[${JSON.stringify(name==='wrong URL'?url+'-other':url)},${JSON.stringify(action)}]}))[0].result})()`);
      assert.equal(observed,result,`${kind}: ${name} outcome`);
      assert.deepEqual(await evaluate(session,'effects'),{stop:stopCount,send:sendCount},`${kind}: ${name} effects`);
      if (kind!=='missing') assert.equal(await evaluate(session,`(()=>{const e=document.getElementById('prompt-textarea');return e.value??e.textContent})()`),draft,`${kind}: ${name} preserves draft`);
      if (attachment) assert.equal(await evaluate(session,`Boolean(document.querySelector('[data-testid="remove-file-button"]'))`),true);
    }
  }
  await evaluate(session,`document.body.innerHTML='<form><textarea></textarea></form>'`);
  await evaluate(session,`document.querySelector('form').outerHTML='<div id="composer-wrap" data-testid="composer"><textarea></textarea><button data-testid="send-button">Send</button></div>'`);
  const compose = action => evaluate(restartedSession,`(async()=>{const tab=(await chrome.tabs.query({})).find(t=>t.url===${JSON.stringify(url)});return (await chrome.scripting.executeScript({target:{tabId:tab.id},func:composer,args:[${JSON.stringify(url)},${JSON.stringify(action)}]}))[0].result})()`);
  assert.equal(await compose('prepare'),'waiting','id-less textarea without form is found through its unique Send control');
  await evaluate(session,`document.getElementById('composer-wrap').removeAttribute('data-testid');document.querySelector('textarea').value=''`);
  assert.equal(await compose('prepare'),'composer-unavailable','unidentified form is not mistaken for the ChatGPT composer');
  await evaluate(session,`document.getElementById('composer-wrap').setAttribute('data-testid','composer')`);
  await evaluate(session,`document.getElementById('composer-wrap').innerHTML='<button data-testid="stop-button">Stop</button>';window.stops=0;document.querySelector('button').onclick=()=>stops++`);
  assert.equal(await compose('prepare'),'busy','missing editor with one visible Stop is treated as busy');
  assert.equal(await evaluate(session,'stops'),0);
  await evaluate(session,`document.getElementById('composer-wrap').outerHTML=${JSON.stringify('<div id="thread-bottom-container"></div><div data-composer-body style="display:none"><div role="textbox" contenteditable="true"></div></div><div data-composer-body><div role="textbox" contenteditable="true"></div><button type="button" aria-label="Send">Send</button></div>')}`);
  await evaluate(session,`document.querySelector('button[aria-label="Send"]').onclick=()=>{const editor=document.querySelectorAll('[role="textbox"]')[1];submissions.push(editor.textContent);editor.textContent=''}`);
  assert.equal(await compose('prepare'),'waiting','current ChatGPT contenteditable composer accepts wake text');
  assert.equal(await compose('wait'),'ready','current ChatGPT Send control becomes ready');
  assert.equal(await compose('send'),'click-attempted','current ChatGPT Send control is clicked');
  assert.equal((await evaluate(session,'submissions')).at(-1),'Process your mailbox');
  // A same-binding empty poll retires a validated saved batch without claiming pickup.
  pendingIds = [];
  await evaluate(restartedSession,`load().then(check)`);
  const rearmed = (await evaluate(restartedSession,'load()'))[0];
  assert.equal(rearmed.ids,undefined);
  assert.equal(rearmed.sendBudget,undefined);
  assert.equal(rearmed.seen.includes(id),true);
  const sendsBeforeRearm = await evaluate(session,'submissions.length');
  await evaluate(restartedSession,'chrome.action.openPopup()');
  const actualPopup=(await command('Target.getTargets')).targetInfos.find(t=>t.url.endsWith('/popup.html') && t.targetId!==popup.targetId);
  assert.ok(actualPopup);
  const actualPopupSession=await attach(actualPopup.targetId);
  for(let attempt=0;attempt<50;attempt++) {
    if(await evaluate(actualPopupSession,`Boolean(document.getElementById('add')?.onclick)`)) break;
    await pause(50);
  }
  assert.equal(await evaluate(actualPopupSession,`chrome.extension.getViews({type:'popup'}).includes(window)`),true,'actual action-popup identity');
  assert.equal(await evaluate(actualPopupSession,`[...document.querySelectorAll('#bindings button')].some(b=>b.textContent==='Re-arm')`),false,'retired batch needs no confirmation');
  assert.equal(await evaluate(session,'submissions.length'),sendsBeforeRearm);
  assert.equal(JSON.parse(await readFile(join(production, 'manifest.json'), 'utf8')).action.default_popup, 'popup.html');
  await evaluate(restartedSession, `(async()=>{const bs=await load();bs.push({...bs[0],id:'other',armed:false,uncertain:true,alert:{retained:true}});bs.push({...bs[0],id:'cancelled',armed:false,uncertain:true});bs.push({...bs[0],id:'removed',removed:true,armed:false});await save(bs);cancelled.add(bindingKey('cancelled',bs[0].incarnation))})()`);
  const beforeAll = await evaluate(restartedSession, 'load()');
  await evaluate(actualPopupSession, "document.getElementById('rearm-all').onclick()");
  const afterAll = await evaluate(restartedSession, 'load()');
  assert.deepEqual(afterAll, beforeAll.map(b => b.removed || b.id === 'cancelled' ? b : {...b,armed:true}), 'popup Re-arm all preserves budget and uncertainty');
  assert.equal(await evaluate(actualPopupSession, "document.getElementById('status').textContent"), 'Re-arm all saved');
  assert.equal(await evaluate(session, 'submissions.length'), sendsBeforeRearm, 'Re-arm all sends nothing inline');
  await evaluate(actualPopupSession, "document.getElementById('rearm-all').onclick()");
  assert.deepEqual(await evaluate(restartedSession, 'load()'), afterAll, 'repeated popup click is idempotent');
  await evaluate(restartedSession, `load().then(bs=>save(bs.filter(b=>b.id===${JSON.stringify(id)})))`);
  await evaluate(restartedSession, `globalThis.originalSet=chrome.storage.local.set;chrome.storage.local.set=async()=>{throw Error('injected save failure')}`);
  await evaluate(actualPopupSession, `[...document.querySelectorAll('#bindings button')].find(b=>b.textContent==='Remove').click()`);
  await pause(100);
  assert.match(await evaluate(actualPopupSession, `document.getElementById('status').textContent`), /Remove not saved:.*may resume after restart/);
  assert.ok(await evaluate(actualPopupSession, `document.querySelector('#bindings p').textContent.includes('mail')`), 'failed Remove leaves row visible');
  await evaluate(restartedSession, 'chrome.storage.local.set=originalSet');
  await command('Target.closeTarget',{targetId:actualPopup.targetId});
  const rearmVersion = versions.find(v=>v.scriptURL===worker.url && v.runningStatus==='running');
  assert.ok(rearmVersion);
  const oldRearmTarget = replacement.targetId;
  await command('Target.detachFromTarget',{sessionId:restartedSession});
  await command('ServiceWorker.stopWorker',{versionId:rearmVersion.versionId},session);
  for(let attempt=0;attempt<50 && !versions.some(v=>v.versionId===rearmVersion.versionId && v.runningStatus==='stopped');attempt++) await pause(100);
  assert.ok(versions.some(v=>v.versionId===rearmVersion.versionId && v.runningStatus==='stopped'));
  const duplicateRearm = await evaluate(popupSession,`chrome.runtime.sendMessage({type:'rearm',id:${JSON.stringify(id)},expectedIncarnation:${JSON.stringify(rearmed.incarnation)},expectedAttempt:${JSON.stringify(rearmed.wakeAttempt.id)}})`);
  assert.match(duplicateRearm.error,/Uncertain wake changed/,'duplicate confirmation cannot clear another attempt');
  for(let attempt=0;attempt<50;attempt++) {
    replacement=(await command('Target.getTargets')).targetInfos.find(t=>t.type==='service_worker' && t.url===worker.url);
    if(replacement && replacement.targetId!==oldRearmTarget) break;
    await pause(100);
  }
  assert.ok(replacement && replacement.targetId!==oldRearmTarget);
  restartedSession=await attach(replacement.targetId);
  assert.equal((await evaluate(restartedSession,'load()'))[0].uncertain,undefined,'real worker restart retains retired batch');
  pendingIds=['22222222-2222-4222-8222-222222222222'];
  await evaluate(restartedSession,'load().then(check)');
  assert.equal(await evaluate(session,'submissions.length'),sendsBeforeRearm+1,'new batch wakes after restart');
  await evaluate(restartedSession,'load().then(check)');
  assert.equal(await evaluate(session,'submissions.length'),sendsBeforeRearm+1,'new uncertain send is fenced');
  await command('Target.closeTarget',{targetId:page.targetId});
  await evaluate(restartedSession,`load().then(check)`);
  assert.equal(reports.length,1,'closed exhausted binding sends immediate HTTP alert');
  assert.equal(reports[0].incident.reason,'bound-tab-missing');
  assert.ok(observations.some(p => p.observation.reason === 'bound-tab-missing'));
  assert.ok(observations.some(p => p.observation.attempt?.detail === 'click-dispatched'));
  assert.ok(observations.every(p => !p.incident && p.delivery_ids.length === 0));
  await evaluate(restartedSession,`load().then(check)`);
  assert.equal(reports.length,1,'HTTP alert dedupe persisted');
  const replacementPage = await command('Target.createTarget', {url: 'about:blank'});
  const replacementSession = await attach(replacementPage.targetId);
  await command('Fetch.enable',{patterns:[{urlPattern:'*'}]},replacementSession);
  await command('Page.navigate',{url},replacementSession);
  for(let attempt=0;attempt<50 && !await evaluate(replacementSession,'typeof submissions!=="undefined"');attempt++) await pause(50);
  await evaluate(restartedSession, 'chrome.action.openPopup()');
  const removePopup=(await command('Target.getTargets')).targetInfos.find(t=>t.url.endsWith('/popup.html') && t.targetId!==popup.targetId);
  const removeSession=await attach(removePopup.targetId);
  await evaluate(removeSession, `show().then(()=>[...document.querySelectorAll('#bindings button')].find(b=>b.textContent==='Remove').onclick())`);
  assert.equal(await evaluate(removeSession, `document.getElementById('status').textContent`), 'Remove: mail saved');
  assert.equal(await evaluate(removeSession, `document.querySelectorAll('#bindings p').length`), 0, 'saved Remove hides row');
  assert.equal((await evaluate(restartedSession,'load()'))[0].removed,true,'tombstone persists');
  const resumed = await evaluate(popupSession,`chrome.runtime.sendMessage({type:'resume-all'})`);
  assert.deepEqual(resumed,{});
  assert.equal((await evaluate(restartedSession,'load()'))[0].alertDone,true,'Resume all preserves active alert');
  await evaluate(removeSession, "document.getElementById('name').value='mail';document.getElementById('add').onclick()");
  assert.equal(await evaluate(removeSession, "document.getElementById('status').textContent"), 'Add chat: mail saved');
  await evaluate(restartedSession,'load().then(check)');
  assert.deepEqual(await evaluate(replacementSession,'submissions'), ['Process your mailbox'], 'real popup replacement dispatches next pending mail');
  await command('Target.closeTarget', {targetId:removePopup.targetId});
  const rows = await evaluate(popupSession,`(async()=>{await chrome.storage.local.set({minimalBindings:[
    {id:'z',name:'Zulu',url:'https://chatgpt.com/c/z',armed:true},
    {id:'a',name:'Alpha',url:'https://chatgpt.com/c/a',armed:true}]});await show();
    return [...document.querySelectorAll('#bindings p')].map(row=>({text:row.textContent,buttons:[...row.querySelectorAll('button')].map(b=>b.textContent)}))})()`);
  assert.deepEqual(rows.map(row=>row.text.split(':')[0]),['Alpha','Zulu'],'saved chats are alphabetical');
  assert.deepEqual(rows[0].buttons,['Resume','Remove'],'Stop is absent from the popup');
  await evaluate(popupSession, `(async()=>{await chrome.storage.local.set({minimalBindings:[{id:${JSON.stringify(id)},name:'mail',generation:1,url:${JSON.stringify(url)},armed:true,ids:[${JSON.stringify(id)}],sendBudget:{batchId:${JSON.stringify(id)},ids:[${JSON.stringify(id)}],auto:3,manualUsed:false,lastAt:Date.now()}}]});await show()})()`);
  assert.equal(await evaluate(popupSession, `[...document.querySelectorAll('#bindings button')].some(b=>b.textContent==='Send one extra wake')`),
    false, 'obsolete manual extra Send control removed after cap retirement');
  console.log('PASS isolated MV3 fixture: storage, alarm/poll, serialized composer guards, restart reservation, alert dedupe, no obsolete manual extra. Installed reload and live ChatGPT acceptance/pickup: NOT_RUN.');
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
