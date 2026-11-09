// Isolated worker+composer integration: no browser profile, local service or live ChatGPT send.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import assert from 'node:assert/strict';

const code = readFileSync(new URL('worker.js', import.meta.url), 'utf8');
const url = 'https://chatgpt.com/c/exact';
const id = '11111111-1111-4111-8111-111111111111';
let busy, pending, draft, disabled, stopCount, sendCount, sent, stopControls, observed, releaseBusy, clearPendingOnStop;
let saved;
class Textarea {
  getClientRects() { return [1]; }
  closest() { return form; }
  querySelector() { return null; }
  focus() {}
  dispatchEvent() { send.disabled = false; }
  set value(text) { draft = text; }
  get value() { return draft; }
}
const editor = new Textarea();
const stop = {getClientRects: () => [1], getAttribute: () => null,
  get disabled() { return disabled; }, click() { stopCount++; if (releaseBusy) busy = false; if (clearPendingOnStop) pending = false; }};
const send = {getClientRects: () => [1], getAttribute: () => null, disabled: true,
  click() { sendCount++; sent.push(draft); draft = ''; }};
const query = selector => selector.includes('stop-button') ? busy ? Array(stopControls).fill(stop) : []
  : selector.includes('send-button') ? [send] : [editor];
const form = {querySelectorAll: query};
const container = {getClientRects: () => [1], querySelectorAll: query};
const ctx = vm.createContext({
  crypto: webcrypto, URL, AbortSignal, Date, console,
  setTimeout: callback => callback(), location: {href: url},
  document: {
    getElementById: () => editor, querySelector: () => null,
    querySelectorAll: selector => selector.includes('composer') || selector.includes('thread-bottom-container') ? [container] : []
  },
  getComputedStyle: () => ({visibility: 'visible'}), HTMLTextAreaElement: Textarea, InputEvent: class {},
  fetch: async request => {
    if (request.includes('/poll?')) return {ok: true, json: async () => ({status: 'ok',
      name: 'mail', endpoint: id, generation: 1, delivery_ids: pending ? [id] : []})};
    if (request.endsWith('/config')) return {ok: true, json: async () => ({status: 'ok', report_token: 'fixture'})};
    observed++; return {ok: true, json: async () => ({status: 'logged', alert: {status: 'queued'}})};
  },
  chrome: {
    runtime: {id: 'test', onMessage: {addListener() {}}},
    alarms: {create() {}, onAlarm: {addListener() {}}},
    storage: {local: {get: async () => structuredClone(saved),
      set: async next => { saved = {...saved, ...structuredClone(next)}; }}},
    tabs: {query: async () => [{id: 2, url: "https://chatgpt.com/c/other"}, {id: 1, url}]},
    scripting: {executeScript: async ({func,args}) => [{result: func(...args)}]}
  }
});
vm.runInContext(code, ctx);
async function run({hasMail=true, isBusy=true, initialDraft='', stopDisabled=false, stops=1, boundUrl=url,
  stopReleases=true, mailClearsOnStop=false}={}) {
  pending = hasMail; busy = isBusy; draft = initialDraft; disabled = stopDisabled;
  stopCount = 0; sendCount = 0; sent = []; stopControls = stops; observed = 0;
  releaseBusy = stopReleases; clearPendingOnStop = mailClearsOnStop;
  send.disabled = true;
  saved = {minimalBindings: [{id, name: 'mail', generation: 1, url: boundUrl,
    armed: true, status: 'busy'}]}; // A stale previous busy status must not suppress Stop.
  await vm.runInContext('load().then(check)', ctx);
  return {stopCount, sendCount, sent, status: saved.minimalBindings[0].status};
}
const success = await run();
assert.equal(success.stopCount, 1, 'one Stop click interrupts a busy matching chat');
assert.deepEqual(success.sent, ['Process your mailbox'], 'worker sends the mailbox prompt after Stop');
assert.equal(success.sendCount, 1, 'one guarded Send follows Stop');
const noMail = await run({hasMail:false});
assert.equal(noMail.stopCount, 0, 'no pending mail must not interrupt the agent');
const wrongChat = await run({boundUrl:'https://chatgpt.com/c/other'});
assert.equal(wrongChat.stopCount, 0, 'a different paired chat must never be stopped');
const protectedDraft = await run({initialDraft:'User draft'});
assert.equal(protectedDraft.stopCount, 0, 'unrelated draft must prevent Stop');
assert.equal(draft, 'User draft', 'draft is preserved');
const ambiguous = await run({stops:2});
assert.equal(ambiguous.stopCount, 0, 'multiple Stop buttons are ambiguous');
const unavailable = await run({stopDisabled:true});
assert.equal(unavailable.stopCount, 0, 'disabled Stop is not clicked');
const stuck = await run({stopReleases:false});
assert.equal(stuck.stopCount, 1, 'a persistent Stop control is clicked at most once per check');
assert.equal(stuck.sendCount, 0, 'no Send while response is still busy');
const withdrawn = await run({mailClearsOnStop:true});
assert.equal(withdrawn.stopCount, 1);
assert.equal(withdrawn.sendCount, 0, 'fresh poll refuses Send after mailbox becomes empty');
console.log('PASS: P1 Stop→Send with two tabs; no mail, wrong chat, draft, ambiguous, disabled, stuck Stop and cleared-mail guard');
