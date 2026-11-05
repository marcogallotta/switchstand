import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const code = await readFile(new URL('./worker.js', import.meta.url), 'utf8');
const backend = await readFile(new URL('../../src/switchstand/chatgpt_wake.py', import.meta.url), 'utf8');
const acceptedOutcomes = new Set([...backend.match(/OUTCOMES = frozenset\(\{([\s\S]*?)\}\)/)[1].matchAll(/"([a-z-]+)"/g)].map(match => match[1]));
const id = number => `00000000-0000-4000-8000-${String(number).padStart(12, '0')}`;
let saved = {}, handler, removed, alarm, outcome = 'click-attempted', blockName, releasePoll;
const observations = new Map(['A', 'B'].map((name, index) => [name,
  {status: 'ok', name, endpoint: id(index + 1), generation: 1, delivery_ids: []}]));
const tabs = new Map([[1, {url: 'https://chatgpt.com/c/a'}], [2, {url: 'https://chatgpt.com/g/g-project/c/b'}]]);
const attempts = [], reports = [], readOnlyChecks = [];
let clock = Date.now(), alertMode = false, diagnosticFailure = false, diagnosticWait, releaseDiagnostic, context;
let onSave, stopPlan;
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
      if (options.args[2]?.prepare && stopPlan) return [{result: await stopPlan(options)}];
      if (options.args[2]?.prepare) return [{result: ['draft-present', 'busy', 'wrong-conversation'].includes(outcome) ? outcome : {outcome: 'idle', sendReady: true, stopReady: false}}];
      if (options.args[1] === 'control') { readOnlyChecks.push(options.target.tabId); return [{result: {outcome: 'idle', facts: {controlSelection: 'unique'}}}]; }
      const observeOnly = options.args[1] === true;
      (observeOnly ? readOnlyChecks : attempts).push(options.target.tabId);
      if (outcome === 'throw') throw Error('interrupted');
      const value = typeof outcome === 'function' ? outcome(options) : observeOnly && outcome === 'click-attempted' ? 'idle' : outcome;
      return [{result: options.args[3] ? (typeof value === 'string' ? {outcome: value, facts: {editorFound: true}} : {...value, facts: {editorFound: true}}) : value}];
    }},
    alarms: {onAlarm: {addListener(value) { alarm = value; }}, async get() { return {}; }}
  };
  context = {chrome, URL, Date: ClockDate, crypto, AbortSignal, setTimeout, fetch: async (url, options) => {
    if (url.endsWith('/config')) return {ok: true, json: async () => ({status: 'ok', report_token: 'fixture-only', escalation: 'configured', recipient: 'ChatGPT Wake Setup 2026-11-04'})};
    if (url.endsWith('/report')) { const report = JSON.parse(options.body); reports.push(report); if (report.diagnostics && diagnosticFailure) throw Error('offline'); if (report.diagnostics && diagnosticWait) await new Promise(resolve => { releaseDiagnostic = resolve; }); if (alertMode === 'expired' && report.incident) { alertMode = true; return {ok: true, json: async () => ({status: 'logged', alert: {status: 'denied', reason: 'observation_expired'}})}; } return {ok: true, json: async () => ({status: 'logged', ...(alertMode && report.incident ? {alert: {status: 'queued', delivery_id: id(999)}} : {})})}; }
    const name = new URL(url).searchParams.get('name');
    if (name === blockName) await new Promise(resolve => { releasePoll = resolve; });
    const observed = observations.get(name);
    return {ok: Boolean(observed), status: observed ? 200 : 403, json: async () => structuredClone(observed)};
  }};
  runInNewContext(code, context);
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
assert.equal(attempts.length, beforeRestartCheck + 1, 'restart permits only a persisted bounded retry');
assert.equal(state('B').retryBudget.usedRetries, 1);
assert.equal(state('B').manualWakeId, undefined, 'restart never creates a manual grant');
tabs.delete(1);
await removed(1);
const beforeClosed = attempts.length;
observations.get('A').delivery_ids = [id(14)];
await message({type: 'check'});
assert.equal(state('A').status, 'bound-tab-missing');
assert.equal(state('A').armed, true, 'closed binding remains observed');
assert.deepEqual(state('A').pending, [id(14)]);
assert.deepEqual(attempts.slice(beforeClosed), [2], 'closed A never receives DOM attempt; B may consume its final retry');
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
assert.equal(reports.filter(item => item.incident).length, 1, 'busy does not reset continuous pending qualification');
clock += 30000; await message({type: 'check'});
const draftIncident = reports.filter(item => item.incident).at(-1).incident;
assert.equal(draftIncident.reason, 'draft-present');
assert.deepEqual(draftIncident.delivery_ids, [id(401)]);
assert.equal(state('A').halted, false);
assert.deepEqual(state('A').attemptIds, [id(401)], 'read-only checks do not reserve a new mutation');
await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 1, 'ongoing draft never falsely recovers');
outcome = 'click-attempted'; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).length, 1, 'click/idle with pending does not recover');
observations.get('A').delivery_ids = []; await message({type: 'check'});
assert.equal(reports.filter(item => item.incident).at(-1).incident.phase, 'recovered');
assert.equal(reports.filter(item => item.incident).at(-1).incident.incident_id, draftIncident.incident_id);
assert.equal(attempts.length, 1, 'seen mail is observed without another DOM mutation');
assert.ok(readOnlyChecks.length > 0);
console.log('PASS seen-pending draft60s, busy preserves pending continuity, read-only observation, dedupe and same-incident recovery');

// All fresh pending outcomes share one timer, even seen IDs and HALT; only empty proves pickup.
for (const blocker of ['idle', 'attempt-outcome-unknown', 'wrong-conversation']) {
  saved = {}; reports.length = 0; attempts.length = 0;
  observations.set('A', {status: 'ok', name: 'A', endpoint: id(1), generation: 1, delivery_ids: [id(410)]});
  outcome = 'click-attempted'; load(); await bind('A', 1); await message({type: 'check'});
  if (blocker === 'attempt-outcome-unknown') state('A').halted = true; delete state('A').manualWakeId;
  outcome = blocker;
  clock += 59000; await message({type: 'check'});
  assert.equal(reports.filter(item => item.incident).length, 0, blocker + ' below60s');
  observations.get('A').delivery_ids.push(id(411)); // New arrivals do not restart an existing backlog.
  clock += 1000; await message({type: 'check'});
  const incident = reports.filter(item => item.incident).at(-1).incident;
  assert.equal(incident.reason, 'pending-mail');
  assert.deepEqual(incident.delivery_ids, [id(410), id(411)]);
  outcome = 'busy'; await message({type: 'check'});
  assert.equal(reports.filter(item => item.incident).length, 1, 'status change dedupes');
  observations.delete('A'); await message({type: 'check'});
  assert.equal(reports.filter(item => item.incident).length, 1, 'failed poll cannot recover');
  observations.set('A', {status: 'ok', name: 'A', endpoint: id(1), generation: 1, delivery_ids: []});
  await message({type: 'check'});
  assert.equal(reports.filter(item => item.incident).at(-1).incident.phase, 'recovered');
}
// Failed poll and mismatched identity cannot qualify, and STOP resets the unqualified timer.
for (const interruption of ['offline', 'mismatch', 'stop']) {
  saved = {}; reports.length = 0;
  observations.set('A', {status: 'ok', name: 'A', endpoint: id(1), generation: 1, delivery_ids: [id(420)]});
  outcome = 'idle'; load(); await bind('A', 1); await message({type: 'check'});
  clock += 30000;
  const observation = observations.get('A');
  if (interruption === 'offline') observations.delete('A');
  if (interruption === 'mismatch') observation.generation = 2;
  if (interruption === 'stop') await message({type: 'disarm'});
  await message({type: 'check'});
  assert.equal(state('A').blockSince, undefined);
  observations.set('A', {...observation, generation: 1});
  clock += 30000; await message({type: 'check'});
  assert.equal(reports.filter(item => item.incident).length, 0, 'interruption cannot age pending');
}
console.log('PASS pending idle/HALT/wrong-chat59/60s, changing backlog, dedupe, errors, identity and STOP');

// Execute the actual page function: read-only mode must never focus or edit.
const pageUrl = 'https://chatgpt.com/c/a';
const editor = {isContentEditable: true, textContent: '', getClientRects: () => [{}],
  closest: () => ({querySelectorAll: () => []}), querySelector: () => null, focus() { throw Error('read-only observation focused editor'); }};
let pageBusy = false;
const pageObserve = runInNewContext(code.slice(code.indexOf('async function nudge('), code.indexOf('const SOURCE')) + ';nudge', {
  location: {href: pageUrl}, HTMLTextAreaElement: class {},
  getComputedStyle: () => ({visibility: 'visible'}),
  document: {getElementById: () => editor, querySelectorAll: () => pageBusy ? [{getBoundingClientRect: () => ({width: 1, height: 1})}] : [], querySelector: () => null}
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
  assert.equal(state('A').retry, undefined, 'no click receipt creates no retry');
  assert.equal(state('A').halted, true);
}
for (const legacyRetry of [false, true]) {
  await unsentWake();
  if (legacyRetry) state('A').retry = {token: 'old-witness', boot: 'old-boot', tabId: 1};
  const originalDiagnostic = structuredClone(state('A').diagnostic);
  state('A').retryBudget.usedRetries = 2; // Exhaustion replaces the old unconditional HALT.
  outcome = 'click-attempted';
  for (const change of [() => {}, () => load(),
    () => { observations.get('A').delivery_ids = [id(701)]; },
    () => { observations.get('A').delivery_ids = []; }]) {
    change(); const before = attempts.length;
    await alarm();
    assert.equal(attempts.length, before, 'exhausted budget prevents replay across checks, reload, and pending changes');
    assert.equal(state('A').halted, true);
    assert.deepEqual(state('A').diagnostic, originalDiagnostic, 'original cause is retained');
    assert.deepEqual(state('A').seen, []);
  }
}
console.log('PASS exhausted budget survives checks/restart/new or empty IDs; retired retry field remains inert.');
assert.ok(reports.some(report => report.outcome === 'attempt-outcome-unknown'));
for (const report of reports) assert.ok(acceptedOutcomes.has(report.outcome), `backend rejects reported outcome ${report.outcome}`);
console.log('PASS every actual emitted report outcome conforms to the backend OUTCOMES contract.');

// Explicit Pair grants a fresh wake without declaring the prior uncertain attempt resolved.
for (const legacy of [false, true]) {
  await unsentWake();
  delete state('A').retry;
  state('A').seen = [id(699), id(700)];
  if (legacy) delete state('A').attemptIds;
  const prior = structuredClone(state('A'));
  observations.get('A').delivery_ids = [id(700)];
  const count = attempts.length;
  outcome = 'draft-present';
  await message({type:'verify', name:'A'}); await message({type:'check'});
  assert.equal(state('A').halted, true, 'empty observation cannot release a halt');
  await message({type:'disarm',endpoint:id(1)});
  assert.equal((await bind('A',1)).error, undefined);
  assert.equal(state('A').armed, true);
  assert.equal(state('A').halted, true, 'Arm preserves unresolved history until a new reservation');
  assert.deepEqual(state('A').seen, prior.seen);
  assert.deepEqual(state('A').lastArmedAttempt.diagnostic, prior.diagnostic);
  assert.deepEqual(state('A').lastArmedAttempt.attemptIds,legacy ? null : prior.attemptIds);
  assert.equal(state('A').manualWakeId, state('A').pairDiagnostic.id);
  assert.deepEqual(state('A').pending, observations.get('A').delivery_ids, 'Arm retains fresh pending IDs');
  assert.equal(attempts.length,count,'Pair does not execute a page script');
  outcome = 'click-attempted'; await message({type:'check'});
  assert.equal(attempts.length, count + 1, 'explicit Arm allows one fresh wake even for seen pending');
  assert.notEqual(state('A').diagnostic.id, prior.diagnostic.id);
  assert.equal(state('A').manualWakeId, undefined);
  assert.deepEqual(state('A').lastArmedAttempt.diagnostic, prior.diagnostic);
  assert.deepEqual(state('A').seen, prior.seen, 'seen history stays unique');
  load(); await alarm(); await message({type:'check'});
  assert.equal(attempts.length, count + 1, 'restart/alarm cannot renew explicit grant');
}
for (const draft of ['exact', 'empty', 'other']) {
  await unsentWake(); const prior = structuredClone(state('A').diagnostic);
  assert.equal((await bind('A',1)).error, undefined, 'bad composer state does not block Arm');
  const before = attempts.length;
  stopPlan = async options => {
    const phase = options.args[2];
    if (draft === 'other' || (draft === 'empty' && phase.recover)) return 'draft-present';
    if (phase.insert) assert.equal(phase.recover, draft === 'exact');
    return {outcome:'idle',prepareReady:true,sendReady:true,stopReady:false};
  };
  outcome = options => {assert.equal(options.args[2].recover, draft === 'exact');return 'click-attempted';};
  await message({type:'check'});
  assert.equal(attempts.length, before + (draft === 'other' ? 0 : 1));
  assert.deepEqual(state('A').lastArmedAttempt.diagnostic, prior);
  assert.equal(Boolean(state('A').manualWakeId), draft === 'other');
  stopPlan = undefined;
}
for (const invalid of ['conversation','unavailable','malformed','generation','endpoint']) {
  await unsentWake();delete state('A').retry;
  const observed=observations.get('A');const original=structuredClone(observed);
  const verified={...observed,tabId:1,url:tabs.get(1).url};
  observed.delivery_ids=[];
  if(invalid==='conversation'){verified.url='https://chatgpt.com/c/other';tabs.set(1,{url:verified.url});}
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
console.log('PASS manual Pair grants one fresh seen-pending wake; archives UNKNOWN/legacy IDs, preserves seen, no page effect from Pair, no alarm/restart renewal, identity/chat/STOP guards.');

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
assert.equal(state('A').retry, undefined, 'no retry allowance after an unsent fallback attempt');
tabs.delete(11); tabs.set(12, {url: state('A').url}); await removed(11);
assert.equal(state('A').retry, undefined, 'lost page cannot create retry');
const beforeLost = attempts.length; await message({type: 'check'});
assert.equal(attempts.length, beforeLost + 1, 'same exact conversation may consume one persisted retry after reopen');
assert.equal(state('A').retryBudget.usedRetries, 1);
assert.equal(state('A').halted, true);

for (const interruption of ['wrong-conversation', 'throw', 'draft-present']) {
  await routingWake(); tabs.set(11, {url: state('A').url});
  outcome = interruption; await message({type: 'check'});
  assert.deepEqual(attempts, interruption === 'throw' ? [10] : [], 'preflight URL/draft defer; execution uncertainty cannot fallback');
  assert.deepEqual(state('A').seen, []);
  assert.equal(Boolean(state('A').halted), interruption === 'throw');
}
console.log('PASS three conversation routing cases; duplicate single target, fallback removal, retained IDs, HALT after fallback page loss, URL race/draft/UNKNOWN fences.');
// Persisted real-world failure states are distinct from a clean fixture binding.
saved = {}; attempts.length = 0; reports.length = 0; alertMode = false;
tabs.set(1, {url: 'https://chatgpt.com/c/a'});
Object.assign(observations.get('A'), {generation: 1, delivery_ids: []});
load(); await bind('A', 1);
observations.get('A').delivery_ids = [id(501)]; outcome = 'throw';
diagnosticFailure = true;
await message({type: 'check'});
const failedAttempt = structuredClone(state('A').diagnostic);
const failureStatus = state('A').status;
assert.equal(failedAttempt.reason, 'execution-failed');
assert.equal(state('A').diagnosticAck.identity, null, 'failed diagnostic POST is not acknowledged');
assert.equal(state('A').diagnosticAck.failure, 'report-unavailable');
const beforeDiagnosticRestart = attempts.length;
load(); diagnosticFailure = false; outcome = 'draft-present';
await message({type: 'check'});
assert.equal(attempts.length, beforeDiagnosticRestart, 'logging never retries unresolved DOM work');
assert.equal(state('A').status, 'draft-present', 'current retry blocker is reported without replacing the prior attempt diagnostic');
assert.deepEqual(state('A').diagnostic, failedAttempt);
assert.ok(state('A').diagnosticAck, 'same retained receipt logged on next ordinary Check');
assert.equal(reports.filter(item => item.diagnostics?.attempt?.id === failedAttempt.id).length, 2);
tabs.set(1,{url:'https://chatgpt.com/c/other'});
assert.match((await bind('A', 1)).error, /another conversation/);
tabs.set(1,{url:'https://chatgpt.com/c/a'});
assert.equal(state('A').pairDiagnostic.result, 'refused');
assert.equal(state('A').pairDiagnostic.reason, 'unresolved-attempt');
assert.deepEqual(state('A').diagnostic, failedAttempt, 'Pair refusal preserves original attempt');
observations.get('A').delivery_ids = [];
await bind('A', 1);
assert.equal(state('A').pairDiagnostic.result, 'accepted');
assert.deepEqual(state('A').diagnostic, failedAttempt, 'Pair acceptance preserves original attempt');
// An unfinished pre-dispatch record never implies that DOM insertion/click occurred.
state('A').halted = true; delete state('A').manualWakeId;
Object.assign(state('A').diagnostic, {finished: null, outcome: null, reason: 'dispatch-start', facts: {}});
state('A').incident = {state: 'queued', evidence: {incident_id: id(502)}};
load(); reports.length = 0;
await message({type: 'check'});
const interrupted = reports.find(item => item.diagnostics).diagnostics;
assert.notEqual(interrupted.boot, interrupted.attempt.boot);
assert.equal(interrupted.attempt.finished, null);
assert.deepEqual(interrupted.attempt.facts, {});
assert.equal(reports.filter(item => item.incident).length, 1, 'fresh empty mailbox recovers inherited alert even under HALT');
assert.equal(reports.find(item => item.incident).incident.phase, 'recovered');
assert.equal(interrupted.attempt.reason, 'dispatch-start', 'unfinished means no durable result; DOM stage unknown');
delete state('A').diagnostic; delete state('A').diagnosticAck;
load(); reports.length = 0;
await message({type: 'check'});
const legacy = reports.find(item => item.diagnostics).diagnostics;
assert.equal(legacy.legacy_halt, true);
assert.equal(legacy.attempt, null, 'legacy HALT does not acquire an invented attempt/cause');
assert.ok(legacy.release && legacy.boot, 'executing diagnostic code identity available for legacy HALT');
// A delayed older host acknowledgement cannot mark a later Pair result as reported.
const binding = state('A');
diagnosticWait = true; releaseDiagnostic = undefined;
binding.pairDiagnostic.id = id(503);
const flush = context.diagnosticReport(binding, async () => {});
while (!releaseDiagnostic) await new Promise(resolve => setTimeout(resolve, 0));
binding.pairDiagnostic = {...binding.pairDiagnostic, id: id(504)};
diagnosticWait = false; releaseDiagnostic(); await flush;
assert.equal(JSON.parse(binding.diagnosticAck.identity).pair.id, id(503));
await context.diagnosticReport(binding, async () => {});
assert.equal(JSON.parse(binding.diagnosticAck.identity).pair.id, id(504));
console.log('PASS diagnostic terminal retention/restart, outage retry, exact ack, registered Pair accepted/refused, inherited incident independence, unfinished and legacy HALT distinction.');

// A Stop sequence consumes its allowance before the effect; all later failures stay HALTED.
for (const failure of ['none', 'pending', 'stop', 'throw', 'timeout', 'refused', 'draft', 'url']) {
  saved = {}; reports.length = 0; attempts.length = 0;
  observations.set('A', {status: 'ok', name: 'A', endpoint: id(1), generation: 1, delivery_ids: [id(700)]});
  tabs.set(1, {url: 'https://chatgpt.com/c/a'}); outcome = 'click-attempted';
  let stopClicks = 0, prepareCalls = 0;
  stopPlan = async options => {
    if (options.args[2].stop) {
      stopClicks++;
      assert.equal(state('A').stopSendAttempt, state('A').diagnostic.id, 'consumed durably before Stop');
      if (failure === 'pending') observations.get('A').delivery_ids = [];
      if (failure === 'stop') void message({type: 'disarm'});
      if (failure === 'throw') throw Error('lost after Stop');
      if (failure === 'draft') outcome = 'draft-present';
      if (failure === 'url') outcome = 'wrong-conversation';
    }
    if (options.args[2].insert) prepareCalls++;
    if (failure === 'refused' && prepareCalls > 1) return {outcome: 'draft-present', facts: {draftPresent: true}};
    return {outcome: 'busy', stopReady: true, sendReady: stopClicks > 0 && !['timeout', 'refused'].includes(failure), inserted: failure === 'refused' && prepareCalls === 1,
      facts: {busy: failure === 'timeout', controlPresent: failure !== 'timeout', controlEnabled: failure !== 'timeout'}};
  };
  load(); await bind('A', 1);
  if (failure === 'pending') state('A').incident = {state: 'queued', evidence: {incident_id: id(705)}};
  await message({type: 'check'});
  if (failure === 'pending') {
    assert.equal(state('A').blockSince, undefined);
    assert.equal(state('A').observedPending.length, 0);
    assert.equal(reports.find(item => item.incident).incident.phase, 'recovered');
  }
  assert.equal(stopClicks, 1);
  assert.equal(attempts.length, ['none', 'draft', 'url'].includes(failure) ? 1 : 0);
  assert.equal(state('A').halted, failure !== 'none');
  assert.ok(state('A').diagnostic.finished, 'every reserved exit finishes diagnostic');
  if (['timeout', 'refused'].includes(failure)) {
    assert.equal(state('A').diagnostic.outcome, 'attempt-outcome-unknown');
    assert.match(state('A').status, new RegExp('readiness-' + failure));
    assert.equal(state('A').diagnostic.facts[failure === 'timeout' ? 'busy' : 'draftPresent'], true);
    if (failure === 'refused') assert.equal(state('A').diagnostic.facts.inserted, true, 'retain own earlier insertion across a later refused preparation');
  }
  if (state('A').retryBudget) state('A').retryBudget.usedRetries = 2;
  load(); await message({type: 'check'});
  assert.equal(stopClicks, 1, 'exhausted budget/restart cannot repeat Stop');
}
// Only durable known no-click can promote an existing exact wake draft; legacy/previous click cannot.
for (const kind of ['known-no-click', 'clicked', 'legacy', 'drained', 'generation', 'endpoint']) {
  saved = {}; attempts.length = 0;
  observations.set('A', {status: 'ok', name: 'A', endpoint: id(1), generation: 1, delivery_ids: []});
  stopPlan = undefined; load(); await bind('A', 1);
  observations.get('A').delivery_ids = [id(701)]; state('A').halted = true; delete state('A').manualWakeId;
  state('A').attemptIds = [id(701)];
  if (kind !== 'legacy') state('A').diagnostic = {id: id(702), endpoint: id(1), generation: 1, delivery_ids: [id(701)], reason: 'result', outcome: 'inserted-not-submitted',
    facts: {inserted: true, ...(kind === 'clicked' ? {clickDispatched: true} : {})}};
  if (kind === 'drained') observations.get('A').delivery_ids = [id(703)];
  if (kind === 'generation') state('A').diagnostic.generation = 2;
  if (kind === 'endpoint') state('A').diagnostic.endpoint = id(704);
  outcome = 'click-attempted'; await message({type: 'check'});
  assert.equal(attempts.length, ['known-no-click', 'clicked', 'legacy'].includes(kind) ? 1 : 0, 'fresh original IDs allow only bounded retries; mismatched evidence remains fenced');
  if (kind === 'known-no-click') assert.equal(state('A').stopSendAttempt, state('A').diagnostic.id);
}
stopPlan = undefined;
console.log('PASS one consumed Stop sequence: pending loss/STOP/error/timeout/draft/URL/restart; exact known NOCLICK versus clicked/legacy HALT');

// Remove uses immediate STOP fencing before its queued pairing deletion.
tabs.set(1, {url: 'https://chatgpt.com/c/a'});
tabs.set(2, {url: 'https://chatgpt.com/g/g-project/c/b'});
saved = {}; attempts.length = 0; outcome = 'click-attempted'; stopPlan = undefined;
for (const name of ['A', 'B']) Object.assign(observations.get(name), {generation: 1, delivery_ids: []});
load(); await bind('A', 1); await bind('B', 2);
const otherBinding = structuredClone(state('B'));
observations.get('A').delivery_ids = [id(980)];
let releaseReady;
stopPlan = async options => {
  if (!options.args[2].insert) return {outcome: 'busy', stopReady: true, sendReady: false};
  await new Promise(resolve => { releaseReady = resolve; });
  return {outcome: 'busy', stopReady: false, sendReady: false};
};
const removingCheck = message({type: 'check'});
while (!releaseReady) await new Promise(resolve => setTimeout(resolve, 0));
const removing = message({type: 'remove', endpoint: id(1)});
releaseReady(); await removingCheck; await removing;
assert.equal(state('A'), undefined, 'remove forgets only the extension pairing');
assert.deepEqual(state('B').seen, otherBinding.seen);
assert.equal(state('B').armed, otherBinding.armed);
assert.equal(attempts.length, 0, 'remove cancels Send while readiness is in flight');
assert.ok(reports.some(r => r.diagnostics?.attempt?.reason === 'stopped-before-dispatch'), 'backend cancellation evidence retained');
stopPlan = undefined; load(); await message({type: 'check'});
assert.equal(state('A'), undefined, 'restart/check cannot resurrect a removed pairing');
assert.equal(attempts.length, 0);
assert.match((await message({type: 'remove'})).error, /Choose a chat/);
assert.ok(state('B'), 'malformed remove never becomes Stop all or remove all');
console.log('PASS Remove cancels in-flight readiness, retains backend evidence/other pairings, and survives restart.');

// Explicit duplicate acceptance is bounded by one durable original-ID episode.
async function retryWake(legacy = false) {
  saved = {}; attempts.length = 0; reports.length = 0; stopPlan = undefined; onSave = undefined;
  observations.set('A', {status: 'ok', name: 'A', endpoint: id(1), generation: 1, delivery_ids: []});
  tabs.clear(); tabs.set(1, {url: 'https://chatgpt.com/c/a'}); outcome = 'throw';
  load(); await bind('A', 1); observations.get('A').delivery_ids = [id(950)];
  if (legacy) { state('A').halted = true; state('A').attemptIds = [id(950)]; delete state('A').manualWakeId; }
}
await retryWake(); await message({type: 'check'});
const original = structuredClone(state('A').diagnostic);
assert.equal(state('A').retryBudget.usedRetries, 0);
load(); await message({type: 'check'});
assert.equal(state('A').retryBudget.usedRetries, 1);
assert.deepEqual(state('A').retryBudget.previousAttempts[0], original);
observations.get('A').delivery_ids.push(id(951)); await bind('A', 1); await alarm();
assert.equal(state('A').retryBudget.usedRetries, 2);
assert.deepEqual(state('A').retryBudget.ids, [id(950)], 'new mail and Pair never expand the original episode');
load(); await bind('A', 1); await message({type: 'check'}); await alarm();
assert.equal(attempts.length, 3, 'initial attempt plus exactly two retries across restart/Pair/alarm');
assert.match(state('A').status, /retry limit/);
clock += 30000; await message({type: 'check'}); clock += 30000; await message({type: 'check'});
assert.ok(reports.some(r => r.incident?.phase === 'blocked'), 'existing pending alert survives exhaustion');
for (const legacy of [false, true]) {
  await retryWake(legacy);
  if (!legacy) await message({type: 'check'});
  const before = attempts.length;
  observations.get('A').delivery_ids = [id(951)];
  await bind('A', 1); await message({type: 'check'});
  assert.equal(attempts.length, before, 'gone original mail cannot authorize a retry from replacement mail');
}
await retryWake(true); await message({type: 'check'}); load(); await message({type: 'check'}); await message({type: 'check'});
assert.equal(attempts.length, 2, 'legacy UNKNOWN has two NEW allowances, never an invented initial send');
assert.equal(state('A').retryBudget.historicalAttempts, 'unknown');
assert.equal(state('A').retryBudget.usedRetries, 2);
await retryWake(true); delete state('A').attemptIds; await bind('A', 1); await message({type: 'check'});
assert.equal(attempts.length, 0, 'legacy without original IDs stays HALTED even after Pair');
assert.equal(state('A').retryBudget, undefined);
await retryWake(true); state('A').armed = false; await message({type: 'check'});
assert.equal(state('A').retryBudget, undefined, 'stopped bindings gain no automatic allowance');
for (const blocker of ['draft-present', 'busy', 'wrong-conversation']) {
  await retryWake(true); outcome = blocker; await message({type: 'check'});
  assert.equal(attempts.length, 0); assert.equal(state('A').retryBudget.usedRetries, 0, 'preflight refusal spends no allowance');
}
await retryWake(true);
onSave = value => { if (value.wakeBindings?.[0].retryBudget?.usedRetries === 1) throw Error('ambiguous durable write'); };
await message({type: 'check'});
assert.equal(attempts.length, 0, 'failed reservation acknowledgement prevents DOM effect');
assert.equal(state('A').retryBudget.usedRetries, 1, 'ambiguous persisted budget is never reset');
onSave = undefined; load(); await message({type: 'check'}); await message({type: 'check'});
assert.equal(attempts.length, 1, 'only remaining allowance survives ambiguous save/restart');
await retryWake(true); blockName = 'A'; releasePoll = undefined;
const retrying = message({type: 'check'});
while (!releasePoll) await new Promise(resolve => setTimeout(resolve, 0));
const stoppingRetry = message({type: 'disarm', endpoint: id(1)});
blockName = undefined; releasePoll(); await retrying; await stoppingRetry;
assert.equal(attempts.length, 0); assert.equal(state('A').armed, false);
console.log('PASS bounded retries: initial+2, legacy2/historyUNKNOWN, durable reserve, Pair/restart/alarm/new-mail cap, original gone, draft and Stop fences, exhaustion alert');
