import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const code = await readFile(new URL('./worker.js', import.meta.url), 'utf8');
const id = number => `00000000-0000-4000-8000-${String(number).padStart(12, '0')}`;
let saved = {}, handler, removed, outcome = 'click-attempted';
const observations = new Map(['A', 'B'].map((name, index) => [name,
  {status: 'ok', name, endpoint: id(index + 1), generation: 1, delivery_ids: []}]));
const tabs = new Map([[1, {url: 'https://chatgpt.com/c/a'}], [2, {url: 'https://chatgpt.com/g/g-project/c/b'}]]);
const attempts = [], reports = [];
function load() {
  const chrome = {
    runtime: {id: 'test', onMessage: {addListener(value) { handler = value; }}},
    storage: {local: {
      async get() { return structuredClone(saved); },
      async set(value) { saved = structuredClone({...saved, ...value}); }
    }},
    tabs: {async get(number) { if (!tabs.has(number)) throw Error('closed'); return tabs.get(number); },
      onRemoved: {addListener(value) { removed = value; }}},
    scripting: {async executeScript(options) { attempts.push(options.target.tabId); if (outcome === 'throw') throw Error('interrupted'); return [{result: outcome}]; }},
    alarms: {onAlarm: {addListener() {}}, async get() { return {}; }}
  };
  runInNewContext(code, {chrome, URL, Date, AbortSignal, fetch: async (url, options) => {
    if (url.endsWith('/config')) return {ok: true, json: async () => ({status: 'ok', report_token: 'fixture-only'})};
    if (url.endsWith('/report')) { reports.push(JSON.parse(options.body)); return {ok: true, json: async () => ({status: 'logged'})}; }
    const observed = observations.get(new URL(url).searchParams.get('name'));
    return {ok: Boolean(observed), status: observed ? 200 : 403, json: async () => structuredClone(observed)};
  }});
}
function message(value) { return new Promise(resolve => handler(value, {id: 'test'}, resolve)); }
async function bind(name, tabId) {
  const metadata = (await message({type: 'verify', name})).status;
  return message({type: 'bind', ...metadata, tabId, url: tabs.get(tabId).url});
}
const state = name => saved.wakeBindings.find(binding => binding.name === name);
load();
assert.match((await bind('A', 1)).status, /armed/);
assert.match((await bind('B', 2)).status, /armed/);
observations.get('A').delivery_ids = [id(10)];
await message({type: 'check'});
assert.deepEqual(attempts, [1], 'A mail never wakes B');
await message({type: 'check'});
assert.equal(attempts.length, 1, 'same IDs deduped');
observations.get('A').delivery_ids = [id(11)]; // Same sender/count, different delivery.
outcome = 'draft-present';
await message({type: 'check'});
assert.equal(state('A').halted, false);
assert.deepEqual(state('A').pending, [id(11)]);
outcome = 'busy';
await message({type: 'check'});
assert.deepEqual(state('A').seen, [id(10)]);
observations.get('A').delivery_ids = []; // Mail handled elsewhere while deferred.
outcome = 'click-attempted';
const count = attempts.length;
await message({type: 'check'});
assert.equal(attempts.length, count, 'vanished pending mail is not sent');
observations.get('B').delivery_ids = [id(12)];
outcome = 'throw';
await message({type: 'check'});
assert.equal(state('B').halted, true);
load(); // Restart from persisted crash/unknown state, new IDs do not clear halt.
observations.get('B').delivery_ids.push(id(13));
const beforeRestartCheck = attempts.length;
await message({type: 'check'});
assert.equal(attempts.length, beforeRestartCheck);
assert.match((await bind('B', 2)).error, /unresolved/);
tabs.delete(1);
await removed(1);
const beforeClosed = attempts.length;
observations.get('A').delivery_ids = [id(14)];
await message({type: 'check'});
assert.equal(state('A').status, 'bound-tab-missing');
assert.equal(state('A').armed, true, 'closed binding remains observed');
assert.deepEqual(state('A').pending, [id(14)]);
assert.equal(attempts.length, beforeClosed, 'closed tab never receives DOM attempt');
assert.ok(reports.some(report => report.outcome === 'bound-tab-missing' && report.delivery_ids.includes(id(14))), 'new undeliverable mail reported after close');
observations.get('A').generation = 2;
await message({type: 'check'});
assert.equal(state('A').armed, false);
assert.equal(state('A').status, 'mailbox-binding-changed');
assert.ok(reports.some(report => report.outcome === 'attempt-outcome-unknown'));
await removed(1);
assert.equal(state('A').status, 'bound-tab-missing');
assert.equal(saved.wakeBindings.length, 2, 'closed chat pairing retained');
await message({type: 'disarm'});
assert.ok(saved.wakeBindings.every(binding => !binding.armed));
assert.deepEqual(state('A').seen, [id(10)], 'stopping retains delivery history');
console.log('PASS two mailboxes, same-count new IDs, draft/busy deferral, stale pending cancellation, uncertain restart halt, generation fencing, native tab closure and STOP.');
