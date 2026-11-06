import {test as base, expect, chromium} from '@playwright/test';
import {createServer} from 'node:http';
import {mkdtemp, readFile, writeFile, copyFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';

const extension = dirname(dirname(fileURLToPath(import.meta.url)));
const url = 'https://chatgpt.com/c/11111111-2222-4333-8444-555555555555';
const identity = '00000000-0000-4000-8000-000000000001';
const delivery = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

// A small editor model: input changes model state on a later render, not synchronously.
// This models a timing boundary; it does not reproduce ChatGPT's editor or acceptance.
const html = `<!doctype html><form><div id="prompt-textarea" contenteditable="true"><p><br></p></div>
<button type="button" data-testid="send-button" aria-label="Send prompt" disabled>Send</button></form>
<ol id="turns"></ol><script>
window.fixture={delay:50,blocked:false,retain:false,busy:false,insertions:0};
let model=''; const editor=document.querySelector('[contenteditable]'),send=document.querySelector('button');
editor.addEventListener('input',()=>{fixture.insertions++;requestAnimationFrame(()=>requestAnimationFrame(()=>setTimeout(()=>{
model=editor.textContent;send.disabled=fixture.blocked||!model.trim();},fixture.delay)))});
send.addEventListener('click',()=>{if(send.disabled||fixture.busy||model!==editor.textContent)return;
const turn=document.createElement('li');turn.textContent=model;document.querySelector('#turns').append(turn);
if(!fixture.retain){editor.textContent='';model='';send.disabled=true;}});
</script>`;

const test = base.extend({
  wake: async ({}, use, info) => {
    const directory = await mkdtemp(join(tmpdir(), 'wake-playwright-'));
    let context, server;
    try {
    const original = await readFile(join(extension, 'worker.js'), 'utf8');
    const metadata = {status:'ok',name:'Fixture',endpoint:identity,generation:1,delivery_ids:[]};
    const mailboxes = new Map([['Fixture', metadata]]);
    server = createServer(async (request, response) => {
      response.setHeader('Content-Type','application/json');
      if(request.url.includes('/poll?')) {
        const mailbox = mailboxes.get(new URL(request.url, 'http://fixture').searchParams.get('name'));
        mailbox.polls = (mailbox.polls || 0) + 1; response.end(JSON.stringify(mailbox));
      }
      else {response.statusCode=404;response.end('{}');}
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const port=server.address().port;
    // The only test seam: relocate the actual extension's fixed backend to this test's port.
    const literal="const SOURCE = 'http://127.0.0.1:18766/internal/chatgpt-wake';";
    expect(original.split(literal)).toHaveLength(2);
    await writeFile(join(directory,'worker.js'),original.replace(literal,`const SOURCE = 'http://127.0.0.1:${port}/internal/chatgpt-wake';`));
    for(const file of ['manifest.json','popup.html','popup.js']) await copyFile(join(extension,file),join(directory,file));
      context=await chromium.launchPersistentContext(join(directory,'profile'),{...(process.env.WAKE_TEST_BROWSER ? {executablePath:process.env.WAKE_TEST_BROWSER} : {channel:'chromium'}),headless:true,
        args:[`--disable-extensions-except=${directory}`,`--load-extension=${directory}`,'--disable-background-networking']});
      // Deny every external request, including ChatGPT. Fixture documents have no resources.
      await context.route('**/*',route=>route.request().url().startsWith('https://chatgpt.com/')
        ? route.fulfill({contentType:'text/html',body:html})
        : route.request().url().startsWith(`http://127.0.0.1:${port}/internal/chatgpt-wake/`)
          ? route.continue() : route.abort());
      const worker=context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
      const popup=await context.newPage();
      await popup.goto(`chrome-extension://${new URL(worker.url()).host}/popup.html`);
      const message=value=>popup.evaluate(value=>chrome.runtime.sendMessage(value),value);
      const page=await context.newPage();await page.goto(url);
      const tabId=await worker.evaluate(async url=>(await chrome.tabs.query({})).find(tab=>tab.url===url).id,url);
      const verified=await message({type:'verify',name:'Fixture'});
      expect(verified.error).toBeUndefined();
      expect((await message({type:'bind',...verified.status,tabId,url})).error).toBeUndefined();
      const check=async()=>{expect((await message({type:'check'})).error).toBeUndefined();};
      await info.attach('extension-source.json',{body:JSON.stringify({worker_sha256:hash(original),backend_port:port,seam:'SOURCE literal only'}),contentType:'application/json'});
      await use({context,page,check,message,metadata,mailboxes,popup,worker});
      expect(hash(await readFile(join(extension,'worker.js')))).toBe(hash(original));
    } finally {
      try { await context?.close(); }
      finally {
        try { if(server?.listening) await new Promise(resolve=>server.close(resolve)); }
        finally { await rm(directory,{recursive:true,force:true}); }
      }
    }
  }
});

test('actual manual wake inserts before Send exists; pending originals retry twice across restart', async ({wake}) => {
  await wake.page.evaluate(() => {
    const form = document.querySelector('form'), send = form.querySelector('button'); send.remove();
    document.querySelector('#prompt-textarea').addEventListener('input', () => setTimeout(() => form.append(send), 100), {once: true});
  });
  wake.metadata.delivery_ids = [delivery(10)];
  await wake.check(); await wake.check(); await wake.check(); await wake.check();
  await expect(wake.page.locator('#turns li')).toHaveText(['Process your mailbox', 'Process your mailbox', 'Process your mailbox']);
  await wake.page.evaluate(() => { fixture.blocked = true; });
  wake.metadata.delivery_ids = []; await wake.check();
  wake.metadata.delivery_ids = [delivery(13)];
  await wake.check();
  const reserved = await wake.popup.evaluate(() => chrome.storage.local.get('wakeBindings'));
  expect(reserved.wakeBindings[0].halted).toBe(true);
  expect(reserved.wakeBindings[0].attemptIds).toEqual([delivery(13)]);
  const boot = await wake.worker.evaluate(() => globalThis.fixtureBoot = crypto.randomUUID());
  const session = await wake.context.newCDPSession(wake.popup);
  await session.send('ServiceWorker.enable'); await session.send('ServiceWorker.stopAllWorkers'); await session.detach();
  await wake.check();
  const newWorker = wake.context.serviceWorkers().find(worker => worker.url() === wake.worker.url());
  expect(newWorker).toBeDefined();
  expect(await newWorker.evaluate(() => globalThis.fixtureBoot || null)).not.toBe(boot);
  expect((await wake.popup.evaluate(() => chrome.storage.local.get('wakeBindings'))).wakeBindings[0].retryBudget.usedRetries).toBe(1);
  await expect(wake.page.locator('#turns li')).toHaveCount(3);
});

test('one reserved Stop waits two seconds then inserts and clicks Send once', async ({wake}) => {
  await wake.page.evaluate(() => {
    window.stopClicks = 0; fixture.busy = true;
    const form = document.querySelector('form'), send = form.querySelector('button'); send.hidden = true;
    const stop = document.createElement('button'); stop.type = 'button'; stop.setAttribute('aria-label', 'Stop');
    stop.onclick = () => { window.stopClicks++; setTimeout(() => { fixture.busy = false; stop.remove(); send.hidden = false; }, 2000); };
    form.append(stop);
  });
  wake.metadata.delivery_ids = [delivery(11)];
  await wake.check(); await wake.check();
  expect(await wake.page.evaluate(() => window.stopClicks)).toBe(1);
  await expect(wake.page.locator('#turns li')).toHaveText(['Process your mailbox', 'Process your mailbox']);
});

test('unrelated draft, attachments and ambiguous visible Send never dispatch', async ({wake}) => {
  wake.metadata.delivery_ids = [delivery(12)];
  await wake.page.locator('#prompt-textarea').fill('Keep my draft'); await wake.check();
  await expect(wake.page.locator('#prompt-textarea')).toHaveText('Keep my draft');
  await wake.page.locator('#prompt-textarea').fill('');
  await wake.page.evaluate(() => { const attached = document.createElement('button'); attached.dataset.testid = 'remove-file-button'; document.querySelector('form').append(attached); });
  await wake.check();
  await wake.page.evaluate(() => { document.querySelector('[data-testid="remove-file-button"]').remove(); const duplicate = document.querySelector('form button').cloneNode(true); document.querySelector('form').append(duplicate); });
  await wake.check(); await expect(wake.page.locator('#turns li')).toHaveCount(0);
  const state = await wake.popup.evaluate(() => chrome.storage.local.get('wakeBindings'));
  expect(state.wakeBindings[0].halted).toBeUndefined();
});


test('actual Chrome alarm preserves the bounded budget across worker shutdown', async ({wake}) => {
  const ring = () => wake.worker.evaluate(() => chrome.alarms.create('mailbox-wake', {when: Date.now() + 100}));
  wake.metadata.delivery_ids = [delivery(31)];
  await ring();
  await expect(wake.page.locator('#turns li')).toHaveText(['Process your mailbox']);
  await expect.poll(async () => (await wake.popup.evaluate(() => chrome.storage.local.get('wakeBindings'))).wakeBindings[0].seen).toContain(delivery(31));
  const polls = wake.metadata.polls;
  await wake.worker.evaluate(() => chrome.alarms.create('mailbox-wake', {when: Date.now() + 2000}));
  const session = await wake.context.newCDPSession(wake.popup);
  await session.send('ServiceWorker.enable');
  await session.send('ServiceWorker.stopAllWorkers');
  await session.detach();
  // The retained real alarm, rather than a popup Check, must restart the worker and poll.
  await expect.poll(() => wake.metadata.polls, {timeout: 10000}).toBeGreaterThan(polls);
  await wake.message({type: 'verify', name: 'Fixture'});
  await expect(wake.page.locator('#turns li')).toHaveCount(2);
  wake.metadata.delivery_ids = []; await wake.check();
  const [worker] = wake.context.serviceWorkers();
  wake.metadata.delivery_ids = [delivery(32)];
  await worker.evaluate(() => chrome.alarms.create('mailbox-wake', {when: Date.now() + 100}));
  await expect(wake.page.locator('#turns li')).toHaveText(['Process your mailbox', 'Process your mailbox', 'Process your mailbox']);
  await wake.message({type: 'disarm'});
  wake.metadata.delivery_ids = [delivery(33)];
  await worker.evaluate(() => chrome.alarms.create('mailbox-wake', {when: Date.now() + 100}));
  await wake.message({type: 'verify', name: 'Fixture'});
  await expect(wake.page.locator('#turns li')).toHaveCount(3);
});

test('multiple exact conversations isolate Stop and resume routing after tab closure', async ({wake}) => {
  const secondUrl = 'https://chatgpt.com/c/second-confirmed';
  const secondIdentity = delivery(90);
  const second = await wake.context.newPage(); await second.goto(secondUrl);
  const tabId = await wake.worker.evaluate(async url => (await chrome.tabs.query({})).find(t => t.url === url).id, secondUrl);
  const metadata = {status:'ok', name:'Second', endpoint:secondIdentity, generation:1, delivery_ids:[]};
  wake.mailboxes.set('Second', metadata);
  expect((await wake.message({type:'bind', ...metadata, tabId, url:secondUrl})).error).toBeUndefined();
  wake.metadata.delivery_ids = [delivery(31)]; metadata.delivery_ids = [delivery(32)];
  await wake.check();
  await expect(wake.page.locator('#turns li')).toHaveText(['Process your mailbox']);
  await expect(second.locator('#turns li')).toHaveText(['Process your mailbox']);
  await wake.page.close();
  wake.metadata.delivery_ids = []; metadata.delivery_ids = []; await wake.check();
  wake.metadata.delivery_ids = [delivery(33)]; metadata.delivery_ids = [delivery(34)];
  await wake.check();
  let bindings = (await wake.popup.evaluate(() => chrome.storage.local.get('wakeBindings'))).wakeBindings;
  expect(bindings[0].availability).toBe('closed');
  expect(bindings[0].seen).toEqual([delivery(31)]);
  await expect(second.locator('#turns li')).toHaveCount(2);
  const reopened = await wake.context.newPage(); await reopened.goto(url);
  await wake.check();
  await expect(reopened.locator('#turns li')).toHaveText(['Process your mailbox']);
  await expect(second.locator('#turns li')).toHaveCount(3);
  await wake.message({type:'disarm', endpoint:secondIdentity});
  wake.metadata.delivery_ids = []; await wake.check();
  wake.metadata.delivery_ids = [delivery(35)]; metadata.delivery_ids = [delivery(36)];
  await wake.check();
  await expect(reopened.locator('#turns li')).toHaveCount(2);
  await expect(second.locator('#turns li')).toHaveCount(3);
  bindings = (await wake.popup.evaluate(() => chrome.storage.local.get('wakeBindings'))).wakeBindings;
  expect(bindings[0].armed).toBe(true); expect(bindings[0].availability).toBe('open');
  expect(bindings[1].armed).toBe(false); expect(bindings[1].seen).toEqual([delivery(32), delivery(34)]);
});
