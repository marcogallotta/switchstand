import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const code = await readFile(new URL('./worker.js', import.meta.url), 'utf8');
const backend = await readFile(new URL('../../src/switchstand/chatgpt_wake.py', import.meta.url), 'utf8');
const acceptedOutcomes = new Set([...backend.match(/OUTCOMES = frozenset\(\{([\s\S]*?)\}\)/)[1].matchAll(/"([a-z-]+)"/g)].map(match => match[1]));
const id = number => `00000000-0000-4000-8000-${String(number).padStart(12, '0')}`;
let saved = {}, handler, removed, outcome = 'click-attempted', blockName, releasePoll;
const observations = new Map(['A', 'B'].map((name, index) => [name,
  {status: 'ok', name, endpoint: id(index + 1), generation: 1, delivery_ids: []}]));
const tabs = new Map([[1, {url: 'https://chatgpt.com/c/a'}], [2, {url: 'https://chatgpt.com/g/g-project/c/b'}]]);
const attempts = [], reports = [], readOnlyChecks = [];
let clock = Date.now(), alertMode = false;
let onSave;
class ClockDate extends Date {
  constructor(value = clock) { super(value); }
  static now() { return clock; }
}
function load() {
  const chrome = {
    runtime: {id: 'test', onMessage: {addListener(value) { handler = value; }}},
    storage: {local: {
      async get() { return structuredClone(saved); },
      async set(value) { saved = structuredClone({...saved, ...value}); if (onSave) await onSave(value); }
    }},
    tabs: {async get(number) { if (!tabs.has(number)) throw Error('closed'); return tabs.get(number); },
      async query() { return [...tabs].map(([id, tab]) => ({id, ...tab})); },
      onRemoved: {addListener(value) { removed = value; }}},
    scripting: {async executeScript(options) {
      const observeOnly = options.args[1] === true;
      (observeOnly ? readOnlyChecks : attempts).push(options.target.tabId);
      if (outcome === 'throw') throw Error('interrupted');
      return [{result: typeof outcome === 'function' ? outcome(options) : observeOnly && outcome === 'click-attempted' ? 'idle' : outcome}];
    }},
    alarms: {onAlarm: {addListener() {}}, async get() { return {}; }}
  };
  runInNewContext(code, {chrome, URL, Date: ClockDate, crypto, AbortSignal, fetch: async (url, options) => {
    if (url.endsWith('/config')) return {ok: true, json: async () => ({status: 'ok', report_token: 'fixture-only', escalation: 'configured', recipient: 'ChatGPT Wake Setup 2026-11-04'})};
    if (url.endsWith('/report')) { const report = JSON.parse(options.body); reports.push(report); if (alertMode === 'expired' && report.incident) { alertMode = true; return {ok: true, json: async () => ({status: 'logged', alert: {status: 'denied', reason: 'observation_expired'}})}; } return {ok: true, json: async () => ({status: 'logged', ...(alertMode && report.incident ? {alert: {status: 'queued', delivery_id: id(999)}} : {})})}; }
    const name = new URL(url).searchParams.get('name');
    if (name === blockName) await new Promise(resolve => { releasePoll = resolve; });
    const observed = observations.get(name);
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
tabs.delete(2);
await removed(2);
observations.get('B').delivery_ids.push(id(15));
await message({type: 'check'});
assert.equal(state('B').halted, true);
assert.equal(state('B').availability, 'closed');
assert.ok(reports.some(report => report.outcome === 'bound-tab-missing' && report.delivery_ids.includes(id(15))));
observations.get('B').delivery_ids = [];
await message({type: 'check'});
assert.equal(state('B').status, 'bound-tab-missing', 'empty mailbox does not erase closure');
assert.equal(state('B').halted, true, 'observation never clears uncertainty');
observations.get('A').generation = 2;
await message({type: 'check'});
assert.equal(state('A').armed, false);
assert.equal(state('A').status, 'mailbox-binding-changed');
assert.ok(reports.some(report => report.outcome === 'attempt-outcome-unknown'));
await removed(1);
assert.equal(state('A').status, 'mailbox-binding-changed', 'closure does not overwrite a stopped stale pairing');
assert.equal(saved.wakeBindings.length, 2, 'closed chat pairing retained');
await message({type: 'disarm'});
assert.ok(saved.wakeBindings.every(binding => !binding.armed));
assert.deepEqual(state('A').seen, [id(10)], 'stopping retains delivery history');
console.log('PASS two mailboxes, same-count new IDs, draft/busy deferral, stale pending cancellation, uncertain restart halt, generation fencing, native tab closure and STOP.');

for (const stopOnlyB of [true, false]) {
  saved = {}; attempts.length = 0;
  tabs.set(1, {url: 'https://chatgpt.com/c/a'});
  tabs.set(2, {url: 'https://chatgpt.com/g/g-project/c/b'});
  for (const name of ['A', 'B']) Object.assign(observations.get(name), {generation: 1, delivery_ids: []});
  load();
  await bind('A', 1); await bind('B', 2);
  observations.get('A').delivery_ids = [id(20)]; observations.get('B').delivery_ids = [id(21)];
  outcome = 'click-attempted'; blockName = 'A'; releasePoll = undefined;
  const checking = message({type: 'check'});
  while (!releasePoll) await new Promise(resolve => setTimeout(resolve, 0));
  const stopping = message({type: 'disarm', ...(stopOnlyB ? {endpoint: id(2)} : {})});
  blockName = undefined; releasePoll();
  await checking; await stopping;
  assert.deepEqual(attempts, stopOnlyB ? [1] : [], 'Stop preempts not-started sends during another mailbox await');
}
console.log('PASS immediate per-chat/all STOP during asynchronous check; unknown closed binding continues observing new IDs and retains closure when empty.');

for (const stopOnlyB of [true, false]) {
  saved = {}; attempts.length = 0;
  for (const name of ['A', 'B']) Object.assign(observations.get(name), {generation: 1, delivery_ids: []});
  load(); await bind('A', 1); await bind('B', 2);
  observations.get('B').delivery_ids = [id(30)];
  blockName = 'B'; releasePoll = undefined;
  const arming = message({type: 'bind', ...observations.get('B'), tabId: 2, url: tabs.get(2).url});
  while (!releasePoll) await new Promise(resolve => setTimeout(resolve, 0));
  const checking = message({type: 'check'});
  const stopping = message({type: 'disarm', ...(stopOnlyB ? {endpoint: id(2)} : {})});
  blockName = undefined; releasePoll();
  await arming; await checking; await stopping;
  assert.deepEqual(attempts, [], 'earlier rearm cannot clear a later Stop intent before queued check');
}
console.log('PASS rearm await → queued check → later per-chat/all Stop preserves stop ordering.');

// Continuous draft qualification, persisted episode, one alert/recovery, no draft modification.
saved = {}; attempts.length = 0; reports.length = 0; alertMode = true;
tabs.set(1, {url: 'https://chatgpt.com/c/a'});
observations.get('A').delivery_ids = [id(200)];
outcome = 'draft-present'; load(); await bind('A', 1);
await message({type: 'check'});
clock += 30000; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 0, 'draft shorter than60s is not alerted');
clock += 30000; await message({type: 'check'});
const firstAlert = reports.find(item => item.incident).incident;
assert.equal(firstAlert.phase, 'blocked');
assert.equal(state('A').incident.state, 'queued');
clock += 30000; load(); await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 1, 'restart/ongoing blocker does not spam');
observations.get('A').delivery_ids = [];
await message({type: 'check'});
const incidents = reports.filter(item => item.incident).map(item => item.incident);
assert.equal(incidents.length, 2);
assert.equal(incidents[1].phase, 'recovered');
assert.equal(incidents[1].incident_id, firstAlert.incident_id);
await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 2, 'one recovery');
// A gap cannot manufacture continuous60seconds of draft evidence.
observations.get('A').delivery_ids = [id(201)];
await message({type: 'check'});
clock += 61000; load(); await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 2);
// Closed + pending qualifies immediately, retained exact evidence before the POST.
tabs.delete(1); await removed(1);
const closedAlert = reports.filter(item => item.incident).at(-1).incident;
assert.equal(closedAlert.reason, 'bound-tab-missing');
assert.equal(state('A').incident.state, 'queued');
const alertsBeforeStop = reports.filter(item => item.incident).length;
await message({type: 'disarm'}); await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, alertsBeforeStop, 'STOP does not alert/recover');
console.log('PASS: incident60s/gap/restart/closed/dedupe/recovery/STOP');

// Unqualified restart resets even a short gap; qualified identity remains durable.
saved = {}; reports.length = 0; attempts.length = 0; alertMode = true;
tabs.set(1, {url: 'https://chatgpt.com/c/a'});
observations.get('A').delivery_ids = [id(300)]; outcome = 'draft-present';
load(); await bind('A', 1); await message({type: 'check'});
clock += 30000; load(); await message({type: 'check'});
clock += 30000; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 0, 'short restart cannot carry unqualified timer');
clock += 30000; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 1);
const qualified = state('A').incident.evidence.incident_id;
load(); clock += 1000; await message({type: 'check'});
assert.equal(state('A').incident.evidence.incident_id, qualified);
// A cached onRemoved empty list is not a fresh mailbox-clear observation.
state('A').pending = []; tabs.delete(1); await removed(1);
assert.equal(reports.filter(item => item.incident).length, 1);
observations.get('A').delivery_ids = []; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 2);
assert.equal(reports.filter(item => item.incident).at(-1).incident.phase, 'recovered');
assert.equal(reports.filter(item => item.incident).at(-1).incident.incident_id, qualified);
assert.equal(state('A').availability, 'closed');
await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 2, 'closed empty recovery deduped');
// Outage >90s: frozen pending op is retained across unknown/unconfigured responses.
observations.get('A').delivery_ids = [id(301)]; alertMode = false;
await message({type: 'check'});
const expiredId = state('A').incident.evidence.incident_id;
clock += 91000; load(); alertMode = 'expired'; await message({type: 'check'});
assert.equal(state('A').lastRetiredIncident, expiredId);
assert.equal(state('A').incident, undefined, 'only definitive absence retires old operation');
await message({type: 'check'});
assert.equal(state('A').incident.state, 'queued');
assert.notEqual(state('A').incident.evidence.incident_id, expiredId);
console.log('PASS: closed+fresh-empty recovery, short restart reset, qualified identity persistence, expired-uncommitted requalification');

// Wake acceptance is not mailbox pickup: seen IDs still qualify closed-tab alerts.
saved = {}; reports.length = 0; attempts.length = 0; alertMode = true;
tabs.set(1, {url: 'https://chatgpt.com/c/a'});
observations.get('A').delivery_ids = [id(400)]; outcome = 'click-attempted';
load(); await bind('A', 1); await message({type: 'check'});
assert.deepEqual(state('A').seen, [id(400)]);
assert.equal(saved.escalation, 'configured');
assert.equal(saved.alertRecipient, 'ChatGPT Wake Setup 2026-11-04');
assert.equal(saved.status, 'checked');
await message({type: 'check'});
assert.equal(attempts.length, 1, 'seen but AVAILABLE mail is never blindly nudged again');
tabs.delete(1); await removed(1);
const seenIncident = reports.filter(item => item.incident).at(-1).incident;
assert.deepEqual(seenIncident.delivery_ids, [id(400)], 'incident uses all authoritative pending IDs');
assert.equal(state('A').incident.state, 'queued');
await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 1, 'fresh closed poll dedupes same episode');
assert.equal(attempts.length, 1);
observations.get('A').delivery_ids = []; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).at(-1).incident.phase, 'recovered');
assert.equal(reports.filter(item => item.incident).at(-1).incident.incident_id, seenIncident.incident_id);
console.log('PASS seen-but-still-pending closed incident, unchanged DOM dedupe, recovery, actual backend configuration display state');

// Already nudged but still pending mail must also observe live draft blockers.
saved = {}; reports.length = 0; attempts.length = 0; readOnlyChecks.length = 0;
tabs.set(1, {url: 'https://chatgpt.com/c/a'});
observations.get('A').delivery_ids = [id(401)]; outcome = 'click-attempted';
load(); await bind('A', 1); await message({type: 'check'});
outcome = 'draft-present'; await message({type: 'check'});
clock += 30000; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 0);
outcome = 'busy'; await message({type: 'check'});
outcome = 'draft-present'; await message({type: 'check'});
clock += 30000; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 0, 'busy interrupts continuous draft qualification');
clock += 30000; await message({type: 'check'});
const draftIncident = reports.filter(item => item.incident).at(-1).incident;
assert.equal(draftIncident.reason, 'draft-present');
assert.deepEqual(draftIncident.delivery_ids, [id(401)]);
assert.equal(state('A').halted, false);
assert.deepEqual(state('A').attemptIds, [id(401)], 'read-only checks do not reserve a new mutation');
await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 1, 'ongoing draft never falsely recovers');
outcome = 'click-attempted'; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).at(-1).incident.phase, 'recovered');
assert.equal(reports.filter(item => item.incident).at(-1).incident.incident_id, draftIncident.incident_id);
assert.equal(attempts.length, 1, 'seen mail is observed without another DOM mutation');
assert.ok(readOnlyChecks.length > 0);
console.log('PASS seen-pending draft60s, busy resets continuity, read-only observation, dedupe and same-incident recovery');

// Execute the actual page function: read-only mode must never focus or edit.
const pageUrl = 'https://chatgpt.com/c/a';
const editor = {isContentEditable: true, textContent: '', getClientRects: () => [{}],
  querySelector: () => null, focus() { throw Error('read-only observation focused editor'); }};
let pageBusy = false;
const pageObserve = runInNewContext(code.slice(code.indexOf('async function nudge('), code.indexOf('const SOURCE')) + ';nudge', {
  location: {href: pageUrl}, HTMLTextAreaElement: class {},
  document: {getElementById: () => editor, querySelector: selector => pageBusy && selector.includes('stop-button') ? {} : null}
});
assert.equal(await pageObserve(pageUrl, true), 'idle');
editor.textContent = 'private draft';
assert.equal(await pageObserve(pageUrl, true), 'draft-present');
pageBusy = true;
assert.equal(await pageObserve(pageUrl, true), 'busy');
assert.equal(await pageObserve('https://chatgpt.com/c/other', true), 'wrong-conversation');
console.log('PASS actual page observer returns before focus/edit with exact URL, draft and busy guards');

// A browser tab can navigate between distinct paired chats without stealing mappings.
saved = {}; attempts.length = 0; reports.length = 0; alertMode = true;
observations.get('A').delivery_ids = [];
observations.get('B').delivery_ids = [];
observations.set('C', {status: 'ok', name: 'C', endpoint: id(3), generation: 1, delivery_ids: []});
tabs.set(1, {url: 'https://chatgpt.com/c/a'});
tabs.set(2, {url: 'https://chatgpt.com/c/b'});
outcome = 'click-attempted'; load(); await bind('A', 1); await bind('B', 2);
observations.get('A').delivery_ids = [id(500)]; await message({type: 'check'});
const originalA = structuredClone(state('A'));
tabs.set(1, {url: 'https://chatgpt.com/c/c'});
assert.equal((await bind('C', 1)).status, 'armed');
assert.equal(saved.wakeBindings.length, 3);
assert.deepEqual(state('A'), originalA, 'new chat never steals or rewrites prior binding state');
assert.equal((await bind('B', 1)).error, 'This chat is already paired with another mailbox.', 'same conversation cannot pair another mailbox');
observations.get('A').delivery_ids = [id(501)];
observations.get('C').delivery_ids = [id(502)]; attempts.length = 0;
await message({type: 'check'});
assert.deepEqual(attempts, [1], 'only the exact current C conversation may receive a wake');
assert.equal(state('A').status, 'bound-tab-missing', 'registered conversation is not open in any tab');
assert.deepEqual(state('A').seen, [id(500)]);
assert.deepEqual(state('C').seen, [id(502)]);
tabs.set(1, {url: originalA.url}); attempts.length = 0;
observations.get('C').delivery_ids = [id(503)];
await message({type: 'check'});
assert.deepEqual(attempts, [1], 'navigating back allows only original A pending mail');
assert.deepEqual(state('A').seen, [id(500), id(501)]);
assert.deepEqual(state('C').seen, [id(502)], 'other chat unseen mail is preserved');
assert.equal(state('C').status, 'bound-tab-missing');
console.log('PASS three pairings, reused tab, retained binding/dedupe state, duplicate URL refusal and exact-chat isolation');

async function unsentWake() {
  saved = {}; attempts.length = 0; onSave = undefined;
  tabs.set(1, {url: 'https://chatgpt.com/c/a'});
  Object.assign(observations.get('A'), {generation: 1, delivery_ids: []});
  load(); await bind('A', 1);
  observations.get('A').delivery_ids = [id(700)];
  outcome = options => ({outcome: 'inserted-not-submitted: no click', noClickToken: options.args[2].token});
  await message({type: 'check'});
  assert.ok(state('A').retry, 'explicit no-click receipt durably retains one allowance');
  assert.equal(state('A').halted, true);
}
await unsentWake();
outcome = options => {
  assert.equal(options.args[2].retry, true, 'retry requests no insertion');
  assert.equal(state('A').retry, undefined, 'allowance consumed in storage before DOM dispatch');
  assert.equal(state('A').halted, true, 'crash during retry stays fenced');
  return 'click-attempted';
};
await message({type: 'check'});
assert.deepEqual(state('A').seen, [id(700)]);
await message({type: 'check'});
assert.equal(attempts.length, 2, 'initial no-click and only one retry');

for (const invalidate of [
  () => load(),
  () => { delete state('A').retry; }, // Legacy UNKNOWN has no evidence.
  () => { observations.get('A').generation++; },
  () => { observations.get('A').endpoint = id(9998); },
  () => { observations.get('A').delivery_ids = [id(701)]; },
  () => { observations.get('A').delivery_ids = []; },
  () => { tabs.set(1, {url: 'https://chatgpt.com/c/other'}); },
  async () => { await message({type: 'disarm'}); }
]) {
  observations.get('A').endpoint = id(1);
  await unsentWake();
  await invalidate();
  const before = attempts.length;
  await message({type: 'check'});
  assert.equal(attempts.length, before, 'stale/missing evidence never dispatches');
}
observations.get('A').endpoint = id(1);
await unsentWake();
let stopping;
onSave = () => {
  if (!state('A').retry && !stopping) stopping = message({type: 'disarm'});
};
await message({type: 'check'}); await stopping; onSave = undefined;
assert.equal(attempts.length, 1, 'STOP during retry consumption preempts DOM dispatch');

for (const failed of ['attempt-outcome-unknown: retry exhausted; control unavailable', 'throw', undefined, 'busy']) {
  await unsentWake();
  outcome = failed;
  await message({type: 'check'});
  const before = attempts.length;
  await message({type: 'check'});
  assert.equal(attempts.length, before, 'failed/ambiguous retry never becomes an endless loop');
  assert.equal(state('A').halted, true);
  assert.equal(state('A').retry, undefined);
}
console.log('PASS explicit no-click single retry; consume-before-dispatch, no insertion, legacy/restart/binding/pending/STOP fences and terminal retry failure.');
assert.ok(reports.some(report => report.outcome === 'attempt-outcome-unknown'));
for (const report of reports) assert.ok(acceptedOutcomes.has(report.outcome), `backend rejects reported outcome ${report.outcome}`);
console.log('PASS every actual emitted report outcome conforms to the backend OUTCOMES contract.');

// Only explicit Pair can reconcile a drained halt; observation never clears it.
for (const legacy of [false, true]) {
  await unsentWake();
  delete state('A').retry;
  state('A').seen = [id(699)];
  if (legacy) delete state('A').attemptIds;
  const prior = structuredClone(state('A'));
  observations.get('A').delivery_ids = [];
  const count = attempts.length;
  await message({type:'verify', name:'A'}); await message({type:'check'});
  assert.equal(state('A').halted, true, 'empty observation cannot release a halt');
  await message({type:'disarm',endpoint:id(1)});
  assert.equal((await bind('A',1)).error, undefined);
  assert.equal(state('A').armed, true);
  assert.equal(Boolean(state('A').halted), false);
  assert.deepEqual(state('A').seen, prior.seen);
  assert.equal(state('A').lastReconciledAttempt.outcome,'attempt-outcome-unknown');
  assert.deepEqual(state('A').lastReconciledAttempt.delivery_ids,legacy ? null : prior.attemptIds);
  assert.equal(attempts.length,count,'Pair does not execute a page script');
}
for (const invalid of ['pending-seen','unavailable','malformed','generation','endpoint']) {
  await unsentWake();delete state('A').retry;
  const observed=observations.get('A');const original=structuredClone(observed);
  const verified={...observed,tabId:1,url:tabs.get(1).url};
  observed.delivery_ids=[];
  if(invalid==='pending-seen'){state('A').seen=[id(700)];observed.delivery_ids=[id(700)];}
  if(invalid==='unavailable')observations.delete('A');
  if(invalid==='malformed')observed.delivery_ids=['not-a-uuid'];
  if(invalid==='generation')observed.generation++;
  if(invalid==='endpoint')observed.endpoint=id(9998);
  const count=attempts.length;
  assert.ok((await message({type:'bind',...verified})).error,invalid);
  assert.equal(state('A').halted,true,invalid);
  assert.equal(attempts.length,count);
  observations.set('A',original);
}
await unsentWake();delete state('A').retry;observations.get('A').delivery_ids=[];
blockName='A';releasePoll=undefined;
const reconciling=message({type:'bind',...observations.get('A'),tabId:1,url:tabs.get(1).url});
while(!releasePoll)await new Promise(resolve=>setTimeout(resolve,0));
const stopReconcile=message({type:'disarm',endpoint:id(1)});
blockName=undefined;releasePoll();
assert.match((await reconciling).error,/Stop requested/);await stopReconcile;
assert.equal(state('A').halted,true);assert.equal(state('A').armed,false);
console.log('PASS explicit drained Pair only; preserves seen/UNKNOWN including legacy missing IDs, rejects seen-pending/malformed/unavailable/drift/Stop, and executes no page script.');

// Conversation routing, with one retained pending delivery per distinct case.
async function routingWake() {
  saved = {}; attempts.length = 0; reports.length = 0; onSave = undefined;
  tabs.clear(); tabs.set(10, {url: 'https://chatgpt.com/c/routing'});
  Object.assign(observations.get('A'), {endpoint: id(1), generation: 1, delivery_ids: []});
  outcome = 'click-attempted'; load(); await bind('A', 10);
  observations.get('A').delivery_ids = [id(800)];
}
await routingWake();
tabs.set(10, {url: 'https://chatgpt.com/c/elsewhere'});
await message({type: 'check'});
assert.deepEqual(attempts, []);
tabs.set(10, {url: state('A').url});
await message({type: 'check'});
assert.deepEqual(attempts, [10], 'same tab away/back wakes retained fresh pending');
await message({type: 'check'});
assert.deepEqual(attempts, [10], 'return never resets seen delivery IDs');

await routingWake();
tabs.set(10, {url: 'https://chatgpt.com/c/elsewhere'});
tabs.set(12, {url: state('A').url}); tabs.set(11, {url: state('A').url});
await message({type: 'check'});
assert.deepEqual(attempts, [11], 'different matching tab: deterministic one target despite duplicate');
observations.get('A').delivery_ids = [id(801)];
tabs.delete(11); await removed(11);
assert.equal(state('A').availability, 'open', 'fallback removal resolves another matching tab');
assert.notEqual(state('A').status, 'bound-tab-missing', 'duplicate closure never claims conversation absent');
await message({type: 'check'});
assert.deepEqual(attempts, [11, 12]);
tabs.set(10, {url: state('A').url});
observations.get('A').delivery_ids = [id(802)];
await message({type: 'check'});
assert.deepEqual(attempts, [11, 12, 10], 'registered hint preferred when it matches again');

await routingWake();
tabs.delete(10); await removed(10); await message({type: 'check'});
assert.equal(state('A').availability, 'closed');
assert.deepEqual(attempts, []);
tabs.set(20, {url: state('A').url});
await message({type: 'check'});
assert.deepEqual(attempts, [20], 'close/reopen uses new tab without re-pair');
assert.deepEqual(state('A').seen, [id(800)]);

await routingWake();
tabs.set(10, {url: 'https://chatgpt.com/c/elsewhere'}); tabs.set(11, {url: state('A').url});
outcome = options => ({outcome: 'inserted-not-submitted: no click', noClickToken: options.args[2].token});
await message({type: 'check'});
assert.equal(state('A').retry.tabId, 11, 'retry owns actual fallback page, not stored hint');
tabs.delete(11); tabs.set(12, {url: state('A').url}); await removed(11);
assert.equal(state('A').retry, undefined, 'lost page forfeits retry immediately');
const beforeLost = attempts.length; await message({type: 'check'});
assert.equal(attempts.length, beforeLost, 'replacement cannot inherit retry or clear UNKNOWN');
assert.equal(state('A').halted, true);

for (const interruption of ['wrong-conversation', 'throw', 'draft-present']) {
  await routingWake(); tabs.set(11, {url: state('A').url});
  outcome = interruption; await message({type: 'check'});
  assert.deepEqual(attempts, [10], 'URL race/draft/uncertainty cannot fallback immediately');
  assert.deepEqual(state('A').seen, []);
  assert.equal(state('A').halted, interruption === 'throw');
}
console.log('PASS three conversation routing cases; duplicate single target, fallback removal, retained IDs, page-owned retry loss, URL race/draft/UNKNOWN fences.');
