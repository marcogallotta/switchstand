import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const code = await readFile(new URL('./worker.js', import.meta.url), 'utf8');
const endpoint = '00000000-0000-4000-8000-000000000001';
const delivery = '00000000-0000-4000-8000-000000000002';
const url = 'https://chatgpt.com/c/confirmed';
let saved = {}, handler, failSave = false, releasePoll, waitPoll = false, jsonFailure = false;
let waitSave = false, releaseSave;
let scriptPlan, effects = [], alarmHandler, scheduledAlarm, alarmCreates = 0;
let observed = {status: 'ok', endpoint, generation: 1, name: 'A', delivery_ids: [delivery]};
let tab = {url};
function load() {
  runInNewContext(code, {
    URL, AbortSignal,
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
      tabs: {async get() { return structuredClone(tab); }},
      scripting: {async executeScript({args}) {
        const attempt = args[2];
        if (attempt.insert || attempt.final) {
          assert.equal(saved.wakeBindings[0].halted, true, 'reservation precedes every effect');
          effects.push(attempt);
        }
        return [{result: scriptPlan ? await scriptPlan(attempt) : {sendReady: true, outcome: 'click-attempted'}}];
      }}
    },
    fetch: async (_url, options) => {
      assert.equal(options.cache, 'no-store', 'pairing uses a fresh backend observation');
      if (waitPoll) await new Promise(resolve => { releasePoll = resolve; });
      return {ok: true, json: async () => {
        if (jsonFailure) throw new SyntaxError('Unexpected token: SECRET-BACKEND-TOKEN');
        return structuredClone(observed);
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
  assert.deepEqual(saved, paired);
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
  assert.deepEqual(state(), {...unresolved.wakeBindings[0], armed: false}, 'Stop does not resolve UNKNOWN');
  load();
  assert.match((await bind()).error, /Unresolved/, 'restart does not renew an unresolved attempt');
}
saved = structuredClone(paired);
saved.wakeBindings.push({...state(), endpoint: delivery});
const multiple = structuredClone(saved);
assert.ok((await bind()).error);
assert.deepEqual(saved, multiple, 'legacy multiple bindings are never truncated');
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
assert.deepEqual(saved, beforeFailure, 'failed persistence is not reported as paired');
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
