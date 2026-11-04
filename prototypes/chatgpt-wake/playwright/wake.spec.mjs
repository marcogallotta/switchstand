import {test as base, expect, chromium} from '@playwright/test';
import {createServer} from 'node:http';
import {mkdtemp, readFile, writeFile, copyFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';

const extension = dirname(dirname(fileURLToPath(import.meta.url)));
const url = 'https://chatgpt.com/c/11111111-2222-4333-8444-555555555555';
const other = 'https://chatgpt.com/c/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
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
    const reports = [];
    server = createServer(async (request, response) => {
      let body=''; for await (const chunk of request) body+=chunk;
      response.setHeader('Content-Type','application/json');
      if(request.url.endsWith('/config')) response.end(JSON.stringify({status:'ok',report_token:'fixture',escalation:'unconfigured',recipient:'Fixture'}));
      else if(request.url.includes('/poll?')) response.end(JSON.stringify(metadata));
      else if(request.url.endsWith('/report')) {reports.push(JSON.parse(body));response.end(JSON.stringify({status:'logged',escalation:'unconfigured'}));}
      else {response.statusCode=409;response.end(JSON.stringify({status:'fixture-does-not-deliver-alerts'}));}
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
      await worker.evaluate(()=>chrome.alarms.clearAll()); // Poll only on explicit public Check messages.
      const popup=await context.newPage();
      await popup.goto(`chrome-extension://${new URL(worker.url()).host}/popup.html`);
      const message=value=>popup.evaluate(value=>chrome.runtime.sendMessage(value),value);
      const open=async(target=url)=>{const page=await context.newPage();await page.goto(target);return page;};
      const page=await open();
      const tabId=await worker.evaluate(async url=>(await chrome.tabs.query({})).find(tab=>tab.url===url).id,url);
      const verified=await message({type:'verify',name:'Fixture'});
      expect(verified.error).toBeUndefined();
      expect((await message({type:'bind',...verified.status,tabId,url})).error).toBeUndefined();
      const check=async()=>{expect((await message({type:'check'})).error).toBeUndefined();};
      await info.attach('extension-source.json',{body:JSON.stringify({worker_sha256:hash(original),backend_port:port,seam:'SOURCE literal only'}),contentType:'application/json'});
      const pair=()=>message({type:'bind',...metadata,tabId,url});
      await use({context,page,open,check,message,pair,metadata,reports,popup});
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

for(const transition of ['same-tab return','different tab','close and reopen']) {
  test(`registered conversation wakes after ${transition}`,async({wake})=>{
    const {page,metadata,check,open}=wake;
    if(transition==='close and reopen')await page.close();else await page.goto(other);
    metadata.delivery_ids=[delivery(1)];await check();
    const target=transition==='same-tab return'?page:await open();
    if(target===page)await page.goto(url);
    await check();await expect(target.locator('#turns li')).toHaveText(['Process your mailbox']);
    await check();await expect(target.locator('#turns li')).toHaveCount(1);
    if(target!==page && !page.isClosed())await expect(page.locator('#turns li')).toHaveCount(0);
  });
}

test('one deterministic matching tab; same batch deduped, distinct arrivals wake',async({wake})=>{
  const duplicate=await wake.open();
  wake.metadata.delivery_ids=[delivery(1),delivery(2)];
  await wake.check();await wake.check();
  await expect(wake.page.locator('#turns li')).toHaveCount(1);
  await expect(duplicate.locator('#turns li')).toHaveCount(0);
  wake.metadata.delivery_ids.push(delivery(3));await wake.check();
  await expect(wake.page.locator('#turns li')).toHaveCount(2);
});

test('existing draft and thinking are preserved until idle',async({wake})=>{
  const editor=wake.page.locator('#prompt-textarea');
  await editor.fill('Human draft');wake.metadata.delivery_ids=[delivery(1)];await wake.check();
  await expect(editor).toHaveText('Human draft');await expect(wake.page.locator('#turns li')).toHaveCount(0);
  await editor.fill('');
  await wake.page.evaluate(()=>{fixture.busy=true;const stop=document.createElement('button');stop.dataset.testid='stop-button';document.body.append(stop);});
  await wake.check();await expect(editor).toBeEmpty();await expect(wake.page.locator('#turns li')).toHaveCount(0);
  await wake.page.evaluate(()=>{fixture.busy=false;document.querySelector('[data-testid="stop-button"]').remove();});
  await wake.check();await expect(wake.page.locator('#turns li')).toHaveCount(1);
});

test('waits for delayed render/Send enabling after insertion',async({wake})=>{
  await wake.page.evaluate(()=>fixture.delay=650);wake.metadata.delivery_ids=[delivery(1)];
  await wake.check();await expect(wake.page.locator('#turns li')).toHaveText(['Process your mailbox']);
  expect(await wake.page.evaluate(()=>fixture.insertions)).toBe(1);
});

test('temporarily blocked Send gets one owned no-click retry without reinsertion',async({wake})=>{
  await wake.page.evaluate(()=>fixture.blocked=true);wake.metadata.delivery_ids=[delivery(1)];
  await wake.check();await expect(wake.page.locator('#turns li')).toHaveCount(0);
  await expect(wake.page.locator('#prompt-textarea')).toHaveText('Process your mailbox');
  await wake.page.evaluate(()=>{fixture.blocked=false;document.querySelector('button').disabled=false;});
  await wake.check();await wake.check();await expect(wake.page.locator('#turns li')).toHaveCount(1);
  expect(await wake.page.evaluate(()=>fixture.insertions)).toBe(1);
});

test('prior click retaining text is never retried; persistent error remains fenced',async({wake})=>{
  await wake.page.evaluate(()=>fixture.retain=true);wake.metadata.delivery_ids=[delivery(1)];
  await wake.check();await wake.check();await expect(wake.page.locator('#turns li')).toHaveCount(1);
  await expect(wake.page.locator('#prompt-textarea')).toHaveText('Process your mailbox');
  wake.metadata.delivery_ids.push(delivery(2));await wake.check();
  await expect(wake.page.locator('#turns li')).toHaveCount(1);
});

test('persistent blocked Send stays halted after its sole retry',async({wake})=>{
  await wake.page.evaluate(()=>{fixture.blocked=true;document.body.insertAdjacentHTML('beforeend','<p role="alert">Fixture error</p>');});
  wake.metadata.delivery_ids=[delivery(1)];await wake.check();await wake.check();await wake.check();
  await expect(wake.page.locator('#turns li')).toHaveCount(0);
  expect(await wake.page.evaluate(()=>fixture.insertions)).toBe(1);
  expect(wake.reports.some(report=>report.outcome==='attempt-outcome-unknown')).toBe(true);
});

test('only explicit Pair reconciles whole-mailbox-empty halt, preserving draft and UNKNOWN',async({wake})=>{
  wake.metadata.delivery_ids=[delivery(1)];await wake.check();
  await wake.page.evaluate(()=>fixture.blocked=true);
  wake.metadata.delivery_ids=[delivery(2)];await wake.check();
  wake.metadata.delivery_ids=[delivery(1)];
  expect((await wake.pair()).error).toMatch(/must be empty/);
  wake.metadata.delivery_ids=[];
  await wake.message({type:'verify',name:'Fixture'});await wake.check();
  const binding=()=>wake.popup.evaluate(()=>chrome.storage.local.get('wakeBindings').then(s=>s.wakeBindings[0]));
  expect((await binding()).halted).toBe(true);
  await wake.message({type:'disarm',endpoint:identity});
  expect((await wake.pair()).error).toBeUndefined();
  const reconciled=await binding();
  expect(reconciled.armed).toBe(true);expect(reconciled.halted).toBeFalsy();
  expect(reconciled.seen).toEqual([delivery(1)]);
  expect(reconciled.lastReconciledAttempt).toMatchObject({outcome:'attempt-outcome-unknown',delivery_ids:[delivery(2)]});
  await expect(wake.page.locator('#turns li')).toHaveCount(1);
  await expect(wake.page.locator('#prompt-textarea')).toHaveText('Process your mailbox');
});


for (const variant of ['hidden-generic', 'hidden-explicit', 'multiple', 'multiple-disabled', 'missing', 'missing-form', 'all-hidden', 'disabled', 'aria-disabled', 'voice']) {
  test(`Send selection: ${variant}`, async ({wake}) => {
    await wake.page.evaluate(variant => {
      const form=document.querySelector('form'), send=document.querySelector('[data-testid="send-button"]');
      form.addEventListener('submit', event=>event.preventDefault());
      window.wrongClicks=0;
      if (variant.startsWith('hidden') || variant.startsWith('multiple')) {
        const other=document.createElement('button');other.type='submit';
        other.hidden=variant.startsWith('hidden');
        if(variant!=='hidden-generic') other.dataset.testid='composer-submit-button';
        other.disabled=variant==='multiple-disabled';
        other.addEventListener('click',()=>window.wrongClicks++);form.prepend(other);
      }
      if(variant==='missing') {send.removeAttribute('data-testid');send.removeAttribute('aria-label');send.type='submit';}
      if(variant==='missing-form') document.body.append(document.querySelector('#prompt-textarea'));
      if(variant==='all-hidden') send.hidden=true;
      if(variant==='disabled') {fixture.blocked=true;send.disabled=true;}
      if(variant==='aria-disabled') send.setAttribute('aria-disabled','true');
      if(variant==='voice') send.setAttribute('aria-label','Start dictation');
    },variant);
    wake.metadata.delivery_ids=[delivery(902)];await wake.check();await wake.check();
    expect(await wake.page.evaluate(()=>window.wrongClicks)).toBe(0);
    const binding=await wake.popup.evaluate(()=>chrome.storage.local.get('wakeBindings').then(s=>s.wakeBindings[0]));
    const reason=variant==='missing-form' ? 'missing-form' : variant==='missing' ? 'no-explicit'
      : variant==='all-hidden' ? 'all-hidden' : variant.startsWith('multiple') ? 'ambiguous' : 'unique';
    expect(binding.diagnostic.facts.controlSelection).toBe(reason);
    const submits=variant.startsWith('hidden');
    await expect(wake.page.locator('#turns li')).toHaveCount(submits ? 1 : 0);
    if(!submits) {
      await expect(wake.page.locator('#prompt-textarea')).toHaveText('Process your mailbox');
      expect(binding.seen).not.toContain(delivery(902));expect(binding.halted).toBe(true);
    }
  });
}


test('HALTED current control snapshot preserves draft, busy state and legacy cause without input', async ({wake}) => {
  await wake.popup.evaluate(async()=>{const s=await chrome.storage.local.get('wakeBindings');
    s.wakeBindings[0].halted=true;await chrome.storage.local.set(s);
    await chrome.scripting.executeScript({target:{tabId:s.wakeBindings[0].tabId},func:()=>{
      globalThis.switchstandWakeWitness={get token(){throw new Error('observation touched ownership');}};
    }});
  });
  await wake.page.evaluate(()=>{document.querySelector('#prompt-textarea').textContent='private draft sentinel';
    fixture.busy=true;const stop=document.createElement('button');stop.dataset.testid='stop-button';document.body.append(stop);
    window.focuses=0;document.querySelector('#prompt-textarea').addEventListener('focus',()=>window.focuses++);
  });
  wake.metadata.delivery_ids=[delivery(903)];await wake.check();
  const read=()=>wake.popup.evaluate(()=>chrome.storage.local.get('wakeBindings').then(s=>s.wakeBindings[0]));
  let binding=await read();expect(binding.currentControl.reason).toBe('unique');
  expect(binding.halted).toBe(true);expect(binding.diagnostic).toBeUndefined();expect(binding.seen).toEqual([]);
  await expect(wake.page.locator('#prompt-textarea')).toHaveText('private draft sentinel');
  expect(await wake.page.evaluate(()=>[fixture.insertions,window.focuses,document.querySelectorAll('#turns li').length])).toEqual([0,0,0]);
  await wake.page.goto(other);await wake.check();binding=await read();expect(binding.currentControl.reason).toBe('unavailable');
  expect(wake.reports.filter(r=>r.diagnostics).at(-1).diagnostics.current_control.reason).toBe('unavailable');
  expect(JSON.stringify(wake.reports)).not.toContain('private draft sentinel');
});
