import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {runInNewContext} from 'node:vm';
const code = await readFile(new URL('./worker.js', import.meta.url), 'utf8');
const endpoint = '00000000-0000-4000-8000-000000000001';
const delivery = '00000000-0000-4000-8000-000000000002';
const url = 'https://chatgpt.com/c/confirmed';
let saved = {}, handler, failSave = false, releasePoll, waitPoll = false, jsonFailure = false;
let waitSave = false, releaseSave;
let scriptPlan, effects = [], alarmHandler, scheduledAlarm, alarmCreates = 0;
let observed = {status: 'ok', endpoint, generation: 1, name: 'A', delivery_ids: [delivery]};
let tab = {url}, tabs, observations, removed;
let reports = [], reportFailure = false, holdReport = false, releaseReport, reportReply;
let backendToken = 'SECRET-REPORT-TOKEN', configReads = 0;
const operational = value => JSON.parse(JSON.stringify(value, (key, item) =>
  ['pairDiagnostic', 'diagnosticAck', 'currentControl'].includes(key) ? undefined : item));
function load() {
  runInNewContext(code, {
    URL, AbortSignal, setTimeout, crypto: {randomUUID},
    chrome: {
      runtime: {id: 'fixture', onMessage: {addListener(value) { handler = value; }}},
      alarms: {onAlarm: {addListener(value) { alarmHandler = value; }},
        async get() { return scheduledAlarm; }, async create(name, options) {
          scheduledAlarm = {name, ...options}; alarmCreates++;
        }},
      storage: {local: {
        async get() { return structuredClone(saved); },
        async set(value) {
          if (failSave) throw Error('private storage error');
          if (waitSave) await new Promise(resolve => { releaseSave = resolve; });
          saved = structuredClone({...saved, ...value});
        }
      }},
      tabs: {async get(id) { return structuredClone(tabs?.find(t => t.id === id) || tab); },
        async query() { return structuredClone(tabs || [{id: 1, ...tab}]); },
        onRemoved: {addListener(value) { removed = value; }}},
      scripting: {async executeScript({args, target}) {
        if (args[1] === 'control') return [{result: {outcome: 'idle', facts: {controlSelection: 'unique'}}}];
        const attempt = args[2];
        if (attempt.insert || attempt.final) {
          const targetUrl = (tabs || [{id: 1, ...tab}]).find(t => t.id === target.tabId)?.url;
          assert.equal(saved.wakeBindings.find(b => b.url === targetUrl).halted, true, 'reservation precedes every effect');
          effects.push({...attempt, tabId: target.tabId});
        }
        return [{result: scriptPlan ? await scriptPlan(attempt) : {sendReady: true, outcome: 'click-attempted'}}];
      }}
    },
    fetch: async (_url, options) => {
      if (_url.endsWith('/config')) {
        configReads++;
        return {ok: true, json: async () => ({status: 'ok', report_token: backendToken})};
      }
      if (_url.endsWith('/report')) {
        reports.push(JSON.parse(options.body));
        if (options.headers.Authorization !== 'Bearer ' + backendToken) return {ok: false, status: 403};
        if (holdReport) await new Promise(resolve => { releaseReport = resolve; });
        if (reportReply) reportReply();
        return {ok: !reportFailure, json: async () => ({status: 'logged'})};
      }
      assert.equal(options.cache, 'no-store', 'pairing uses a fresh backend observation');
      if (waitPoll) await new Promise(resolve => { releasePoll = resolve; });
      return {ok: true, json: async () => {
        if (jsonFailure) throw new SyntaxError('Unexpected token: SECRET-BACKEND-TOKEN');
        return structuredClone(observations?.[new URL(_url).searchParams.get('name')] || observed);
      }};
    }
  });
}
const message = (value, sender = 'fixture') => new Promise(resolve => handler(value, {id: sender}, resolve));
const bind = () => message({type: 'bind', tabId: 1, url, name: 'A', endpoint, generation: 1});
const state = () => saved.wakeBindings[0];
load();
assert.equal((await message({type: 'verify', name: 'A'})).status.endpoint, endpoint);
jsonFailure = true;
const malformed = await message({type: 'verify', name: 'A'});
assert.match(malformed.error, /^Could not update pairing\./);
assert.doesNotMatch(JSON.stringify(malformed), /SECRET|Unexpected token/);
jsonFailure = false;
assert.match((await bind()).status, /^Paired/);
assert.deepEqual(state().pending, [delivery], 'pending mail does not block pairing');
assert.equal(state().url, url);
await message({type: 'disarm'});
assert.equal(state().armed, false);
assert.deepEqual(state().pending, [delivery], 'Stop preserves pending identity');
state().seen = [delivery];
assert.match((await bind()).status, /^Paired/);
assert.deepEqual(state().seen, [delivery], 'same pairing preserves delivery history');
const paired = structuredClone(saved);
for (const replacement of [
  {...observed, generation: 2}, {...observed, endpoint: delivery},
  {...observed, delivery_ids: ['invalid']}, {...observed, status: 'denied'}
]) {
  const prior = observed; observed = replacement;
  assert.ok((await bind()).error, 'stale or malformed mailbox identity refuses pairing');
  assert.deepEqual(operational(saved), operational(paired));
  observed = prior;
}
tab.url = 'https://chatgpt.com/c/moved';
assert.ok((await bind()).error);
tab.url = url;
for (const patch of [{halted: true, attemptIds: [delivery]}, {pendingSend: {id: delivery}}, {diagnostic: {result: 'attempt-outcome-unknown'}}]) {
  saved = structuredClone(paired); Object.assign(state(), patch);
  const unresolved = structuredClone(saved);
  assert.match((await bind()).error, /Unresolved/);
  await message({type: 'disarm'});
  assert.deepEqual(operational(state()), operational({...unresolved.wakeBindings[0], armed: false}), 'Stop does not resolve UNKNOWN');
  load();
  assert.match((await bind()).error, /Unresolved/, 'restart does not renew an unresolved attempt');
}
saved = structuredClone(paired);
saved.wakeBindings.push({...state(), endpoint: delivery});
const multiple = structuredClone(saved);
assert.ok((await bind()).error);
assert.deepEqual(operational(saved), operational(multiple), 'legacy multiple bindings are never truncated');
saved = structuredClone(paired); waitPoll = true;
const inFlight = bind();
while (!releasePoll) await new Promise(resolve => setImmediate(resolve));
const stopping = message({type: 'disarm'});
await stopping;
assert.equal(state().armed, false, 'Stop persists before the held backend request is released');
load();
assert.equal(state().armed, false, 'worker restart sees the durable Stop while network is held');
waitPoll = false; releasePoll();
assert.match((await inFlight).error, /Stop requested/);
assert.equal(state().armed, false, 'Stop preempts pairing across backend await');
waitSave = true;
const savingPair = bind();
while (!releaseSave) await new Promise(resolve => setImmediate(resolve));
const stopAfterSaveStarted = message({type: 'disarm'});
waitSave = false; releaseSave();
await savingPair;
await stopAfterSaveStarted;
assert.equal(state().armed, false, 'Stop is ordered after an already-started Pair storage write');
failSave = true;
const beforeFailure = structuredClone(saved);
assert.match((await bind()).error, /^Could not update pairing\./);
assert.deepEqual(operational(saved), operational(beforeFailure), 'failed persistence is not reported as paired');
failSave = false;
let foreignReplied = false;
assert.equal(handler({type: 'disarm'}, {id: 'foreign'}, () => { foreignReplied = true; }), undefined);
assert.equal(foreignReplied, false);
console.log('pairing contract PASS: pending, identity, history, Stop, restart, storage failure');
const check = () => message({type: 'check'});
const reset = () => { saved = structuredClone(paired); state().seen = []; effects = []; scriptPlan = undefined; load(); };
reset();
assert.match((await check()).status, /^Wake click attempted/);
assert.deepEqual(state().seen, [delivery]);
await check();
assert.equal(effects.length, 2, 'repeat Check cannot send the same batch again');
load(); await check(); assert.equal(effects.length, 2, 'restart retains dedupe');
for (const response of ['draft-present', 'busy', 'composer-unavailable']) {
  reset(); scriptPlan = () => response;
  await check(); assert.equal(effects.length, 0); assert.equal(state().halted, undefined);
}
reset(); observed.generation = 2;
assert.ok((await check()).error); assert.equal(effects.length, 0); observed.generation = 1;
reset(); failSave = true;
assert.ok((await check()).error); assert.equal(effects.length, 0, 'save failure blocks insertion/Stop/Send'); failSave = false;
reset(); scriptPlan = attempt => { if (attempt.insert) throw Error('interrupted'); return {sendReady: true}; };
await check(); assert.equal(state().halted, true);
load(); await check(); assert.equal(effects.length, 1);
assert.match((await bind()).error, /Unresolved/, 'Pair cannot renew an UNKNOWN attempt');
reset(); scriptPlan = attempt => {
  if (attempt.insert) observed.delivery_ids = [];
  return {sendReady: true};
};
await check(); assert.equal(effects.some(effect => effect.final), false, 'original pending loss blocks final Send');
assert.equal(state().halted, true); observed.delivery_ids = [delivery];
reset(); let releaseReadiness;
scriptPlan = async attempt => {
  if (attempt.insert) await new Promise(resolve => { releaseReadiness = resolve; });
  return {sendReady: true};
};
const preparing = check();
while (!releaseReadiness) await new Promise(resolve => setImmediate(resolve));
await message({type: 'disarm'});
assert.equal(state().armed, false);
releaseReadiness(); await preparing;
assert.equal(effects.some(effect => effect.final), false, 'Stop during readiness prevents final dispatch');
assert.equal(state().armed, false, 'attempt completion cannot resurrect Stop');
assert.equal(state().halted, true);
console.log('manual contract PASS: reserve-before-effect, dedupe, identity, draft, save failure, UNKNOWN, pending loss and Stop');

// Drain the same serial queue through a read-only Verify; no second Check drives the alarm.
const alarm = async (name = 'mailbox-wake') => {
  alarmHandler({name});
  await message({type: 'verify', name: 'A'});
};
reset();
assert.equal(scheduledAlarm.periodInMinutes, 0.5);
const created = alarmCreates;
load(); await message({type: 'verify', name: 'A'});
assert.equal(alarmCreates, created, 'worker restart reuses the existing alarm');
await alarm('unrelated'); assert.equal(effects.length, 0);
await alarm(); assert.equal(effects.length, 2, 'alarm executes the ordinary reserved wake');
await alarm(); load(); await alarm();
assert.equal(effects.length, 2, 'alarm and restart preserve committed ID dedupe');
const nextDelivery = '00000000-0000-4000-8000-000000000003';
observed.delivery_ids = [nextDelivery];
await alarm(); assert.equal(effects.length, 4, 'same count with a different delivery is new mail');
assert.deepEqual(state().seen, [delivery, nextDelivery]);
observed.delivery_ids = [delivery];
reset(); scriptPlan = () => 'draft-present';
await alarm(); assert.equal(effects.length, 0); assert.equal(state().halted, undefined);
scriptPlan = undefined; await alarm(); assert.equal(effects.length, 2, 'draft defers until a later safe check');
reset(); jsonFailure = true;
await alarm(); assert.equal(effects.length, 0); assert.equal(state().halted, undefined);
jsonFailure = false; await alarm(); assert.equal(effects.length, 2, 'failed observation cannot consume mail');
reset(); scriptPlan = attempt => { if (attempt.insert) throw Error('interrupted'); return {sendReady: true}; };
await alarm(); assert.equal(state().halted, true);
load(); await alarm(); assert.equal(effects.length, 1, 'alarm and restart cannot renew UNKNOWN');
reset(); await message({type: 'disarm'}); await alarm(); assert.equal(effects.length, 0);
reset(); waitPoll = true; releasePoll = undefined;
alarmHandler({name: 'mailbox-wake'});
while (!releasePoll) await new Promise(resolve => setImmediate(resolve));
await message({type: 'disarm'});
waitPoll = false; releasePoll(); await message({type: 'verify', name: 'A'});
assert.equal(state().armed, false); assert.equal(effects.length, 0, 'Stop preempts an alarm awaiting observation');
console.log('automatic contract PASS: alarm ownership, restart, IDs, deferral, UNKNOWN and Stop');

// Multiple exact bindings share dispatch but retain independent cancellation and history.
const endpointB = '00000000-0000-4000-8000-000000000003';
const deliveryB = '00000000-0000-4000-8000-000000000004';
const urlB = 'https://chatgpt.com/c/second';
const bindB = () => message({type: 'bind', tabId: 2, url: urlB, name: 'B', endpoint: endpointB, generation: 1});
const setupMultiple = async () => {
  reset(); tabs = [{id: 1, url}, {id: 2, url: urlB}];
  observations = {A: {...observed, name: 'A', delivery_ids: [delivery]},
    B: {...observed, endpoint: endpointB, name: 'B', delivery_ids: [deliveryB]}};
  assert.match((await bindB()).status, /^Paired/);
};
await setupMultiple();
assert.equal(saved.wakeBindings.length, 2);
assert.deepEqual(state().seen, []);
const duplicateBefore = structuredClone(saved);
assert.ok((await message({type: 'bind', tabId: 1, url, name: 'B', endpoint: endpointB, generation: 1})).error);
assert.deepEqual(operational(saved), operational(duplicateBefore), 'conversation cannot be reassigned to another mailbox');
await message({type: 'check'});
assert.deepEqual(effects.filter(e => e.final).map(e => e.tabId), [1, 2]);
assert.deepEqual(saved.wakeBindings[1].seen, [deliveryB]);
await setupMultiple();
state().halted = true; state().attemptIds = [delivery];
await message({type: 'check'});
assert.deepEqual(state().attemptIds, [delivery], 'legacy UNKNOWN remains unresolved');
assert.deepEqual(effects.filter(e => e.final).map(e => e.tabId), [2], 'unrelated mailbox still wakes');
await setupMultiple();
tabs = [{id: 1, url: 'https://chatgpt.com/c/unrelated'}, {id: 2, url: urlB}];
removed(); await message({type: 'verify', name: 'A'});
assert.equal(state().availability, 'closed');
await message({type: 'check'});
assert.deepEqual(state().seen, [], 'closed conversation consumes no pending identity');
assert.deepEqual(effects.filter(e => e.final).map(e => e.tabId), [2]);
tabs.push({id: 9, url}, {id: 7, url});
await message({type: 'check'});
assert.equal(effects.filter(e => e.final).at(-1).tabId, 7, 'reopened exact URL uses lowest ID');
assert.equal(state().availability, 'open');
await setupMultiple(); tabs.push({id: 0, url});
await message({type: 'check'});
assert.equal(effects.find(e => e.final).tabId, 1, 'saved exact tab wins over duplicate');
await setupMultiple();
let releaseA;
scriptPlan = async attempt => {
  if (attempt.prepare && !attempt.insert && !releaseA) await new Promise(resolve => { releaseA = resolve; });
  return {sendReady: true, outcome: 'click-attempted'};
};
const checkingMultiple = message({type: 'check'});
while (!releaseA) await new Promise(resolve => setImmediate(resolve));
await message({type: 'disarm', endpoint: endpointB});
releaseA(); await checkingMultiple;
assert.deepEqual(effects.filter(e => e.final).map(e => e.tabId), [1], 'Stop B does not cancel A');
assert.equal(saved.wakeBindings[1].armed, false);
await setupMultiple();
// A Pair awaiting a backend response must merge the newest Stop state.
saved.wakeBindings.pop(); waitPoll = true; releasePoll = undefined;
const pairingB = bindB();
while (!releasePoll) await new Promise(resolve => setImmediate(resolve));
await message({type: 'disarm', endpoint});
waitPoll = false; releasePoll(); await pairingB;
assert.equal(state().armed, false);
assert.equal(saved.wakeBindings[1].armed, true);
assert.deepEqual(state().pending, [delivery]);
for (const stoppedEndpoint of [endpoint, undefined]) {
  await setupMultiple();
  let release;
  scriptPlan = async attempt => {
    if (attempt.prepare && !attempt.insert && !release) await new Promise(resolve => { release = resolve; });
    return {sendReady: true, outcome: 'click-attempted'};
  };
  const checking = message({type: 'check'});
  while (!release) await new Promise(resolve => setImmediate(resolve));
  await message({type: 'disarm', endpoint: stoppedEndpoint});
  release(); await checking;
  assert.deepEqual(effects.filter(e => e.final).map(e => e.tabId), stoppedEndpoint ? [2] : [],
    'endpoint Stop isolates cancellation; global Stop cancels every dispatch');
}
await setupMultiple();
saved.wakeBindings.pop(); waitPoll = true; releasePoll = undefined;
const stalePair = bindB();
while (!releasePoll) await new Promise(resolve => setImmediate(resolve));
await message({type: 'disarm', endpoint: endpointB});
waitPoll = false; releasePoll();
assert.match((await stalePair).error, /Stop requested/);
assert.equal(saved.wakeBindings.length, 1, 'Stop fences a not-yet-persisted endpoint Pair');
console.log('Multiple exact pairing contracts PASS: identity, history, deterministic routing, close/reopen, isolated/global Stop and stale Pair');

// Typed receipts preserve wake semantics; failures and ACKs touch metadata only.
observations = undefined; tabs = undefined; tab = {url};
reset(); reports = [];
scriptPlan = attempt => ({sendReady: true, outcome: 'click-attempted',
  inserted: Boolean(attempt.insert), facts: {inserted: Boolean(attempt.insert), clickDispatched: Boolean(attempt.final)}});
await check();
assert.equal(state().diagnostic.outcome, 'click-attempted');
assert.ok(state().diagnostic.finished);
assert.equal(state().diagnostic.facts.clickDispatched, true);
assert.ok(state().diagnosticAck.identity);
const accepted = reports.at(-1);
assert.equal(accepted.diagnostics.pair.result, 'accepted');
const count = reports.length;
await check(); assert.equal(reports.length, count, 'unchanged exact snapshot is ACKed');
assert.match((await bind()).status, /^Paired/, 'successful receipt does not block Pair');
observed.delivery_ids = [nextDelivery];
await check(); assert.equal(effects.filter(e => e.final).length, 2, 'success metadata permits new IDs');
observed.delivery_ids = [delivery];
reset(); reportFailure = true;
await check(); assert.equal(state().diagnosticAck.failure, 'report-unavailable');
assert.equal(effects.filter(e => e.final).length, 1, 'report outage cannot prevent completed wake');
reportFailure = false; load(); await check();
assert.equal(state().diagnosticAck.failure, null);
assert.equal(effects.filter(e => e.final).length, 1, 'outage replay and restart never resend');
reset(); state().halted = true; state().attemptIds = [delivery];
await check(); assert.equal(reports.at(-1).diagnostics.legacy_halt, true);
assert.equal(state().currentControl.reason, 'unique'); assert.equal(effects.length, 0);
reset(); scriptPlan = attempt => attempt.insert ? {outcome: 'composer-unavailable: SECRET-DRAFT'} : {sendReady: true};
await check(); assert.equal(state().status, 'readiness-refused');
assert.equal(state().diagnostic.outcome, 'attempt-outcome-unknown', 'only diagnostic outcome is normalized');
assert.equal(state().halted, true);
await bind(); await check(); assert.equal(reports.at(-1).diagnostics.pair.reason, 'unresolved-attempt');
reset(); scriptPlan = attempt => {
  if (attempt.insert) return {sendReady: false, inserted: true, facts: {inserted: true, controlPresent: true, controlEnabled: false}};
  return {sendReady: true};
};
await check(); assert.equal(state().status, 'readiness-timeout');
assert.equal(state().diagnostic.outcome, 'attempt-outcome-unknown');
assert.deepEqual(state().diagnostic.facts, {inserted: true, controlPresent: true, controlEnabled: false});
reset(); holdReport = true; releaseReport = undefined;
const reporting = check();
while (!releaseReport) await new Promise(resolve => setImmediate(resolve));
await message({type: 'disarm'});
const replacement = {...state().diagnostic, id: randomUUID()};
state().diagnostic = replacement;
holdReport = false; releaseReport(); await reporting;
assert.equal(state().armed, false, 'ACK merge cannot overwrite concurrent Stop');
assert.equal(state().diagnostic.id, replacement.id);
assert.equal(state().diagnosticAck, undefined, 'old snapshot cannot ACK a newer attempt');
reset(); scriptPlan = undefined;
await check();
backendToken = 'SECRET-ROTATED-TOKEN';
state().pairDiagnostic.id = randomUUID(); // A new snapshot after the host restart.
const retained = structuredClone(state().diagnostic);
await message({type: 'disarm'});
const beforeRotation = reports.length, beforeConfig = configReads;
await check();
assert.equal(reports.length, beforeRotation + 1, '403 does not retry within the check');
assert.equal(state().diagnosticAck.failure, 'report-unavailable');
await check();
assert.equal(configReads, beforeConfig + 1, 'next ordinary check refreshes the rejected token');
assert.equal(state().diagnosticAck.failure, null);
assert.deepEqual(state().diagnostic, retained);
assert.equal(state().armed, false);
assert.equal(effects.filter(e => e.final).length, 1, 'report replay cannot renew the wake');
assert.doesNotMatch(JSON.stringify(reports), /SECRET|chatgpt.com|confirmed|composer-unavailable:/);
if (process.env.DIAGNOSTIC_PAYLOADS) await writeFile(process.env.DIAGNOSTIC_PAYLOADS, JSON.stringify(reports));
console.log('diagnostic contracts PASS: typed snapshots, privacy, normalization, success, legacy, ACK/outage/restart and Stop');
