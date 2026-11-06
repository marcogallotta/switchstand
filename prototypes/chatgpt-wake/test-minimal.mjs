import {spawnSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import assert from 'node:assert/strict';
const source = readFileSync(new URL('worker.js', import.meta.url), 'utf8');
const handlers = [], toolbarHandlers = [];
let state = {}, clicks = 0, inserts = 0, stopClicks = 0;
let now = Date.now(), reports = [], observations = [], alertStatus = 'queued', alertReason;
class Clock extends Date { static now() { return now; } }
const url = 'https://chatgpt.com/c/exact';
const id = '11111111-1111-1111-1111-111111111111';
const send = {disabled: false, getClientRects: () => [1], getAttribute: () => null, click: () => clicks++};
let busy = false, attached = false;
const stop = {...send, click: () => { stopClicks++; busy = false; }};
const form = {querySelectorAll: selector => selector.includes('stop-button') ? busy ? [stop] : [] : [send]};
const editor = { getClientRects: () => [1], closest: () => form, querySelector: () => null,
  focus() {}, dispatchEvent() { inserts++; }};
let fallbackEditorAvailable = true, containerAvailable = true;
const container = {getClientRects: () => [1], querySelectorAll: selector => selector.includes('stop-button') ? busy ? [stop] : [] : fallbackEditorAvailable ? [editor] : []};
class Textarea {}
Object.setPrototypeOf(editor, Textarea.prototype);
Object.defineProperty(Textarea.prototype, 'value', {set(value) { this.content = value; }, get() { return this.content || ''; }});
const context = vm.createContext({crypto: webcrypto, Date: Clock, console, URL, AbortSignal, setTimeout: fn => { fn(); },
  location: {href: url}, document: {getElementById: () => editor, querySelector: selector => selector.includes('composer') ? containerAvailable ? container : null : attached ? {} : null,
    querySelectorAll: selector => selector.includes('composer') || selector.includes('thread-bottom-container') ? containerAvailable ? [container] : [] : selector.includes('stop-button') ? busy ? [stop] : [] : [editor]},
  getComputedStyle: () => ({visibility: 'visible'}), HTMLTextAreaElement: Textarea, InputEvent: class {},
  fetch: async (url, options) => {
    if (url.endsWith('/report')) {
      const payload = JSON.parse(options.body);
      (payload.observation ? observations : reports).push(payload);
      return {ok: true, json: async () => ({status: 'logged', alert: {status: alertStatus, reason: alertReason}})};
    }
    return {ok: true, json: async () => ({status: 'ok', report_token: 'fixture', name: 'mail', endpoint: id, generation: 1, delivery_ids: [id]})};
  },
  chrome: {action: {onClicked: {addListener(handler) { toolbarHandlers.push(handler); }}}, runtime: {id: 'test', onMessage: {addListener(handler) { handlers.push(handler); }}}, alarms: {create() {}, onAlarm: {addListener() {}}},
    storage: {local: {get: async () => structuredClone(state), set: async next => { state = {...state, ...structuredClone(next)}; }}},
    tabs: {query: async () => [{id: 1, url}], get: async tabId => ({id: tabId, url})}, scripting: {executeScript: async ({func, args}) => [{result: func(...args)}]}}});
vm.runInContext(source, context);
const run = expression => vm.runInContext(expression, context);
assert.equal(run(`validUrl('${url}')`), true);
assert.equal(run(`validUrl('https://evil.com/c/exact')`), false);
assert.equal(run(`composer('${url}', 'prepare')`), 'waiting');
assert.equal(inserts, 1);
assert.equal(run(`composer('${url}', 'send')`), 'click-attempted');
assert.equal(clicks, 1);
editor.value = 'Unrelated draft';
assert.equal(run(`composer('${url}', 'prepare')`), 'draft-present');
assert.equal(editor.value, 'Unrelated draft');
editor.value = ''; attached = true;
assert.equal(run(`composer('${url}', 'prepare')`), 'draft-present');
attached = false; busy = true;
assert.equal(run(`composer('${url}', 'prepare')`), 'busy');
assert.equal(stopClicks, 1);
assert.equal(run(`composer('https://chatgpt.com/c/other', 'send')`), 'wrong-conversation');
context.document.getElementById = () => null;
assert.equal(run(`composer('${url}', 'prepare')`), 'waiting', 'unique visible fallback editor works without the old ID');
fallbackEditorAvailable = false;
busy = true;
const beforeMissingEditorStop = stopClicks;
assert.equal(run(`composer('${url}', 'prepare')`), 'busy', 'a unique Stop is usable when the editor is absent');
assert.equal(stopClicks, beforeMissingEditorStop + 1);
containerAvailable = false;
assert.equal(run(`composer('${url}', 'prepare')`), 'composer-unavailable', 'unidentified page-wide editor is refused');
containerAvailable = true;
fallbackEditorAvailable = true;
context.document.getElementById = () => editor;
run(`globalThis.bindings = [{id: '${id}', name: 'mail', generation: 1, url: '${url}', armed: true}]`);
await run('check(bindings)');
const before = clicks;
await run('check(bindings)');
assert.equal(clicks, before);
assert.equal(state.minimalBindings[0].uncertain, true, 'click reserves an uncertain outcome before another poll');
const restartedAfterClick = vm.createContext({...context});
vm.runInContext(source, restartedAfterClick);
await vm.runInContext('load().then(check)', restartedAfterClick);
assert.equal(clicks, before, 'a worker restart cannot send the same pending mail twice');
run(`cancelled.add('${id}')`);
await run('check(bindings)');
assert.equal(clicks, before);
console.log('PASS: exact URL, draft/attachment protection, Stop, one uncertain click, restart fence, cancellation');

const nextId = '22222222-2222-4222-8222-222222222222';
const originalBatchFetch = context.fetch;
context.fetch = async (request, options) => request.endsWith('/report') || request.endsWith('/config')
  ? originalBatchFetch(request, options)
  : {ok: true, json: async () => ({status: 'ok', name: 'mail', endpoint: id, generation: 1, delivery_ids: [nextId]})};
run(`cancelled.clear(); globalThis.newBatch = {id: '${id}', name: 'mail', generation: 1, url: '${url}', armed: true, ids: ['${id}'], status: 'draft-present'}`);
const beforeNewBatch = clicks;
await run('check([newBatch])');
assert.equal(clicks, beforeNewBatch + 1, 'a disjoint new batch can wake after a definite no-click');
assert.equal(state.minimalBindings[0].ids[0], nextId);
context.fetch = originalBatchFetch;
console.log('PASS: new pending IDs replace an old definitely unsent batch');

run(`cancelled.clear(); globalThis.alertBinding = {id: '${id}', name: 'mail', generation: 1, pending: ['${id}'], status: 'Chat not open'};`);
await run('report(alertBinding, [alertBinding])');
assert.equal(reports.length, 1);
assert.equal(reports[0].incident.reason, 'bound-tab-missing');
assert.equal(state.minimalBindings[0].alert.incident_id, reports[0].incident.incident_id);
await run('report(alertBinding, [alertBinding])');
assert.equal(reports.length, 1, 'queued alert deduplicated');
run(`alertBinding = {...alertBinding, alertDone: false, alert: undefined, checked: undefined, status: 'draft-present'}`);
await run('report(alertBinding, [alertBinding])');
assert.equal(reports.length, 1, 'draft not reported before 60 seconds');
now += 30000;
await run('report(alertBinding, [alertBinding])');
now += 30000;
await run('report(alertBinding, [alertBinding])');
assert.equal(reports.length, 2);
assert.equal(reports[1].incident.reason, 'draft-present');
run(`alertBinding.alertDone = false`);
alertStatus = 'unavailable';
await run('report(alertBinding, [alertBinding])');
assert.deepEqual(reports[2], reports[1], 'ambiguous receipt replays exact incident');
alertStatus = 'stale'; alertReason = 'pending_evidence_changed';
await run('report(alertBinding, [alertBinding])');
assert.equal(run('alertBinding.alert'), undefined, 'definitively stale evidence retired');
alertStatus = 'queued'; alertReason = undefined;
run(`alertBinding.status = 'busy'`);
await run('report(alertBinding, [alertBinding])');
now += 30000;
await run('report(alertBinding, [alertBinding])');
now += 30000;
await run('report(alertBinding, [alertBinding])');
assert.equal(reports.length, 5, 'continuous pending mail qualifies after 60 seconds even while busy');
assert.equal(reports.at(-1).incident.reason, 'pending-mail');
console.log('PASS: immediate closed-tab alert,60-second draft and pending-mail alerts, exact replay, stale-evidence recovery');

const reportsBeforeStop = reports.length;
run(`cancelled.add('${id}'); alertBinding.alertDone = false`);
await run('report(alertBinding, [alertBinding])');
assert.equal(reports.length, reportsBeforeStop, 'Stop suppresses alert');
run(`cancelled.clear(); bindings[0].alertDone = true`);
const originalFetch = context.fetch;
context.fetch = async () => ({ok: true, json: async () => ({status: 'ok', name: 'mail', endpoint: id, generation: 1, delivery_ids: []})});
await run('check(bindings)');
assert.equal(run('bindings[0].uncertain'), true, 'fresh empty cannot clear uncertain wake evidence');
assert.equal(run('bindings[0].status'), 'Needs manual reconciliation');
assert.equal(run('bindings[0].alertDone'), undefined, 'fresh empty resets episode');
context.fetch = originalFetch;
console.log('PASS: Stop suppresses alerts; fresh empty resolves alert episode but retains uncertain send');

const originalTabs = context.chrome.tabs.query;
context.chrome.tabs.query = async () => [];
state = {minimalBindings: [{id, name: 'mail', generation: 1, url, armed: true}]};
const beforeClosed = reports.length, clicksBeforeClosed = clicks;
await run('load().then(check)');
assert.equal(reports.length, beforeClosed + 1, 'pending mailbox alerts immediately when chat is closed');
assert.equal(reports.at(-1).incident.reason, 'bound-tab-missing');
assert.equal(clicks, clicksBeforeClosed);
state.minimalBindings[0].checked = now;
state.minimalBindings[0].since = now - 50000;
const message = m => new Promise(resolve => handlers.at(-1)(m, {id: 'test'}, resolve));
state.minimalBindings[0].armed = false;
const incidentBeforeResume = state.minimalBindings[0].alert.incident_id;
await message({type: 'resume', id});
assert.equal(state.minimalBindings[0].armed, true);
assert.equal(state.minimalBindings[0].alert.incident_id, incidentBeforeResume);
assert.equal(state.minimalBindings[0].alertDone, true, 'Resume preserves queued alert dedupe');
context.chrome.tabs.query = originalTabs;
delete state.minimalBindings[0].alert;
delete state.minimalBindings[0].alertDone;
const beforeRestart = reports.length;
await run('load().then(check)');
assert.equal(reports.length, beforeRestart, 'confirmed alert is not repeated');
state.minimalBindings[0].status = 'draft-present';
const reportSavedDraft = () => run('load().then(async bs => { await report(bs[0], bs); await save(bs); })');
await reportSavedDraft();
now += 30000;
await reportSavedDraft();
const restarted = vm.createContext({...context});
vm.runInContext(source, restarted);
now += 30000;
await vm.runInContext('load().then(async bs => { await report(bs[0], bs); await save(bs); })', restarted);
assert.equal(reports.length, beforeRestart + 1, 'fresh qualification survives actual new worker VM');
assert.equal(reports.at(-1).incident.reason, 'draft-present');
const afterRestart = reports.length;
await vm.runInContext('load().then(async bs => { await report(bs[0], bs); await save(bs); })', restarted);
assert.equal(reports.length, afterRestart, 'persisted queued dedupe survives restart');
delete state.minimalBindings[0].alert;
delete state.minimalBindings[0].alertDone;
state.minimalBindings[0].checked = now - 61000;
state.minimalBindings[0].since = now - 120000;
await vm.runInContext('load().then(async bs => { await report(bs[0], bs); await save(bs); })', restarted);
assert.equal(reports.length, afterRestart, 'stale polling gap cannot qualify a new alert');
assert.equal(state.minimalBindings[0].since, now);
console.log('PASS: closed-tab alert, Stop/Arm timer reset, draft restart continuity/dedupe, stale-gap reset');
const model = spawnSync('/home/marco/switchstand/.venv/bin/python', ['-c',
  'import json,sys; from switchstand.chatgpt_alerts import AlertReport; [AlertReport.model_validate(p["incident"]) for p in json.load(sys.stdin)]; print("PASS: actual host AlertReport accepts every emitted incident")'],
  {input: JSON.stringify(reports), encoding: 'utf8', env: {...process.env, PYTHONPATH: new URL('../../src', import.meta.url).pathname}});
assert.equal(model.status, 0, model.stderr);
process.stdout.write(model.stdout);

state = {wakeBindings: [
  {endpoint: id, name: 'mail', generation: 1, url, armed: false, halted: true, attemptIds: [id]},
  {endpoint: id, name: 'known', generation: 1, url, armed: true, retryBudget: {usedRetries: 1, ids: [id]}, incident: {state: 'queued', evidence: {incident_id: id}}}
]};
const legacy = structuredClone(state.wakeBindings);
await run('load()');
assert.deepEqual(state.wakeBindings, legacy, 'legacy history preserved untouched');
assert.equal(state.minimalBindings[0].armed, false);
assert.equal(state.minimalBindings[0].uncertain, true, 'legacy unknown stays fenced');
assert.equal(state.minimalBindings[1].uncertain, true, 'legacy retry evidence is fenced');
assert.equal(state.minimalBindings[1].alertDone, true);
console.log('PASS: legacy migration preserves stopped/UNKNOWN, queued alerts and untouched history');

state = {wakeBindings: [{endpoint: id, name: 'unknown', generation: 1, url, armed: true, halted: true}]};
await run('load()');
assert.equal(state.minimalBindings[0].uncertain, true, 'UNKNOWN without original IDs cannot fabricate a retry');
state.minimalBindings[0].uncertain = true;
await run('load()');
assert.equal(state.minimalBindings[0].uncertain, true, 'repeated loads preserve the fence');
console.log('PASS: historical unknown outcomes stay fenced across repeated loads');

state = {wakeBindings: [{endpoint: id, name: 'mail', generation: 1, url, armed: true, seen: [id]}]};
await run('load()');
assert.equal(state.minimalBindings[0].uncertain, false, 'legacy seen IDs do not halt other mail');
assert.deepEqual(state.minimalBindings[0].seen, [id]);
const beforeSeen = clicks;
await run('load().then(check)');
assert.equal(clicks, beforeSeen, 'upgrade cannot resend a still-pending seen delivery');
const beforeSeenFetch = context.fetch;
context.fetch = async (request, options) => request.endsWith('/report') || request.endsWith('/config') ? beforeSeenFetch(request, options)
  : {ok: true, json: async () => ({status: 'ok', name: 'mail', endpoint: id, generation: 1, delivery_ids: [id, nextId]})};
await run('load().then(check)');
assert.equal(clicks, beforeSeen + 1, 'new B wakes even while old seen A remains pending');
context.fetch = beforeSeenFetch;
console.log('PASS: legacy seen A stays fenced while new B can wake');

const oldAlert = {incident_id: id, delivery_ids: [id], phase: 'blocked', name: 'mail', endpoint: id, generation: 1,
  reason: 'bound-tab-missing', started_at: new Date(now).toISOString(), observed_at: new Date(now).toISOString()};
state = {minimalBindings: [{id, name: 'mail', generation: 1, url, armed: true, pending: [id], alert: oldAlert, alertDone: true}]};
const originalAlertFetch = context.fetch, originalAlertTabs = context.chrome.tabs.query;
context.fetch = async (request, options) => request.includes('/poll?') ? Promise.reject(Error('transient poll failure')) : originalAlertFetch(request, options);
await run('load().then(check)');
assert.equal(state.minimalBindings[0].alertDone, true, 'failed poll retains confirmed alert evidence');
context.fetch = async (request, options) => request.endsWith('/report') || request.endsWith('/config')
  ? originalAlertFetch(request, options)
  : {ok: true, json: async () => ({status: 'ok', name: 'mail', endpoint: id, generation: 1, delivery_ids: [id, nextId]})};
context.chrome.tabs.query = async () => [];
const beforeDifferentBatch = reports.length;
await run('load().then(check)');
assert.equal(reports.length, beforeDifferentBatch, 'continuous pending episode is not reported twice when B appears');
assert.equal(state.minimalBindings[0].alert.incident_id, oldAlert.incident_id);
state = {minimalBindings: [{id, name: 'mail', generation: 1, url, armed: true, pending: [id], alert: oldAlert, alertDone: false}]};
const beforeUnconfirmed = reports.length;
await run('load().then(check)');
assert.equal(reports.length, beforeUnconfirmed + 1, 'unconfirmed A replays its exact incident first');
assert.equal(reports.at(-1).incident.incident_id, oldAlert.incident_id);
await run('load().then(check)');
assert.equal(reports.length, beforeUnconfirmed + 1, 'confirmed incident covers the continuous pending episode');
context.fetch = async () => ({ok: true, json: async () => ({status: 'ok', name: 'mail', endpoint: id, generation: 2, delivery_ids: [nextId]})});
state = {minimalBindings: [{id, name: 'mail', generation: 1, url, armed: false, uncertain: true, alert: oldAlert, alertDone: true}]};
const duplicate = await new Promise(resolve => handlers[0]({type: 'add', tabId: 1, name: 'mail', url}, {id: 'test'}, resolve));
assert.match(duplicate.error, /already added/, 'Add cannot replace a prior pairing');
assert.equal(state.minimalBindings[0].alertDone, true, 'duplicate Add retains the ongoing incident');
assert.equal(state.minimalBindings[0].uncertain, true, 'duplicate Add retains the uncertain wake fence');
context.fetch = originalAlertFetch;
context.chrome.tabs.query = originalAlertTabs;
console.log('PASS: one confirmed incident covers continuous pending mail through poll failure and pairing');

state = {minimalBindings: [{id, name: 'mail', generation: 1, url, armed: true}]};
const realGetEditor = context.document.getElementById;
context.document.getElementById = () => null;
containerAvailable = false;
const beforeUnavailable = clicks;
for (let attempt = 0; attempt < 4; attempt++) await run('load().then(check)');
assert.equal(clicks, beforeUnavailable, 'transiently missing composer does not send');
context.document.getElementById = realGetEditor;
containerAvailable = true;
editor.value = '';
await run('load().then(check)');
assert.equal(clicks, beforeUnavailable + 1, 'composer recovery wakes still-pending mail');
console.log('PASS: transient composer unavailability does not strand pending mail');

state = {minimalBindings: [{id, name: 'mail', generation: 1, url, armed: true}]};
const normalTabQuery = context.chrome.tabs.query;
const beforeSuspended = clicks;
for (const key of ['discarded', 'frozen']) {
  context.chrome.tabs.query = async () => [{id: 1, url, [key]: true}];
  await run('load().then(check)');
  assert.equal(clicks, beforeSuspended);
  assert.equal(state.minimalBindings[0].uncertain, undefined, `${key} tab cannot reserve an uncertain wake`);
}
context.chrome.tabs.query = normalTabQuery;
editor.value = '';
await run('load().then(check)');
assert.equal(clicks, beforeSuspended + 1, 'resumed tab can wake pending mail');
console.log('PASS: discarded and frozen tabs defer without losing a later wake');

for (const marker of ['pendingSend', 'diagnostic', 'stopSendAttempt']) {
  state = {wakeBindings: [{endpoint: id, name: 'mail', generation: 1, url, armed: true, [marker]: {outcome: 'unknown'}}]};
  await run('load()');
  assert.equal(state.minimalBindings[0].uncertain, true, `${marker} remains an uncertain legacy fence`);
  const beforeLegacy = clicks;
  await run('load().then(check)');
  assert.equal(clicks, beforeLegacy, `${marker} cannot cause an automatic resend`);
}
console.log('PASS: legacy pending-send, diagnostic and stop-send uncertainty remains fenced');

state = {minimalBindings: [{id, name: 'mail', generation: 1, url, armed: true}]};
const beforeRecovery = reports.length;
const recoveryTabs = context.chrome.tabs.query, recoveryFetch = context.fetch;
context.chrome.tabs.query = async () => [];
await run('load().then(check)');
assert.equal(reports.length, beforeRecovery + 1);
const blocked = reports.at(-1).incident;
context.fetch = async (request, options) => request.includes('/poll?')
  ? {ok: true, json: async () => ({status: 'ok', name: 'mail', endpoint: id, generation: 1, delivery_ids: []})}
  : recoveryFetch(request, options);
alertStatus = 'unavailable';
await run('load().then(check)');
assert.equal(reports.at(-1).incident.phase, 'recovered');
assert.equal(state.minimalBindings[0].recovering, true, 'uncertain recovery keeps exact incident');
const firstRecovery = structuredClone(reports.at(-1));
alertStatus = 'queued';
await run('load().then(check)');
assert.deepEqual(reports.at(-1), firstRecovery, 'recovery replays exact incident and arguments');
assert.equal(reports.at(-1).incident.incident_id, blocked.incident_id);
assert.equal(state.minimalBindings[0].alert, undefined, 'confirmed recovery clears local incident');
context.fetch = recoveryFetch;
context.chrome.tabs.query = recoveryTabs;
console.log('PASS: empty mailbox reports one recovery and replays an uncertain receipt');

state = {minimalBindings: []};
const add = await new Promise(resolve => handlers[0]({type: 'add', name: 'mail', tabId: 1, url}, {id: 'test'}, resolve));
assert.equal(add.error, undefined, 'Add verifies and stores one current chat');
assert.equal(state.minimalBindings.length, 1);
const duplicateAdd = await new Promise(resolve => handlers[0]({type: 'add', name: 'mail', tabId: 1, url}, {id: 'test'}, resolve));
assert.match(duplicateAdd.error, /already added/);
assert.equal(state.minimalBindings.length, 1);
const actualTabGet = context.chrome.tabs.get;
let tabReads = 0;
context.chrome.tabs.get = async tabId => ({id: tabId, url: ++tabReads === 1 ? url : 'https://chatgpt.com/c/other'});
state = {minimalBindings: []};
const navigatedAdd = await new Promise(resolve => handlers[0]({type: 'add', name: 'mail', tabId: 1, url}, {id: 'test'}, resolve));
assert.match(navigatedAdd.error, /Current chat changed/);
assert.equal(state.minimalBindings.length, 0, 'tab navigation before commit cannot pair the wrong chat');
context.chrome.tabs.get = actualTabGet;
state = {minimalBindings: [{id, name: 'mail', generation: 1, url, armed: false, uncertain: true},
  {id: nextId, name: 'other', generation: 1, url: 'https://chatgpt.com/c/other', armed: false}]};
await message({type: 'resume', id});
assert.equal(state.minimalBindings[0].armed, true);
assert.equal(state.minimalBindings[0].uncertain, true, 'Resume cannot clear an unknown send');
assert.equal(state.minimalBindings[1].armed, false, 'per-chat Resume isolates other pairings');
await message({type: 'resume-all'});
assert.equal(state.minimalBindings[1].armed, true);
const unsafeRemove = await message({type: 'remove', id});
assert.match(unsafeRemove.error, /Pending mail or alert/);
assert.equal(state.minimalBindings.length, 2, 'Remove cannot erase unresolved history');
state.minimalBindings[0] = {id, name: 'mail', generation: 1, url, armed: true, uncertain: true, ids: [id], seen: [id]};
context.fetch = async (request, options) => request.endsWith('/report') || request.endsWith('/config') ? originalFetch(request, options)
  : {ok: true, json: async () => ({status: 'ok', name: 'mail', endpoint: id, generation: 1, delivery_ids: []})};
const removed = await new Promise(resolve => handlers[0]({type: 'remove', id}, {id: 'test'}, resolve));
assert.equal(removed.error, undefined);
assert.equal(state.minimalBindings[0].removed, true, 'Remove retains a tombstone');
assert.equal(state.minimalBindings[0].uncertain, true, 'Remove retains uncertain history');
assert.deepEqual(state.minimalBindings[0].ids, [id]);
assert.deepEqual(state.minimalBindings[0].seen, [id]);
const readd = await new Promise(resolve => handlers[0]({type: 'add', name: 'mail', tabId: 1, url}, {id: 'test'}, resolve));
assert.match(readd.error, /retains safety history/);
assert.equal(state.minimalBindings.length, 2, 're-Add cannot erase removed pairing');
context.fetch = originalFetch;
console.log('PASS: one-click Add, duplicate/navigation rejection, isolated Resume, Resume all and guarded Remove tombstone');

// Diagnostic uploads are independent of alerts and browser actions.
context.fetch = originalFetch;
context.chrome.tabs.query = originalTabs;
run(`cancelled.clear(); loggedObservations.clear()`);
const diagnosticClicks = clicks;
run(`globalThis.diagnosticBinding = {id: '${id}', name: 'mail', generation: 1, url: '${url}', armed: true, uncertain: true}`);
await run('check([diagnosticBinding])');
assert.equal(observations.at(-1).observation.reason, 'uncertain-wake');
assert.equal(observations.at(-1).observation.attempt, null, 'legacy uncertainty has no invented attempt');
const diagnosticCount = observations.length;
await run('check([diagnosticBinding])');
assert.equal(observations.length, diagnosticCount, 'unchanged observation is deduplicated');
context.chrome.tabs.query = async () => [{id: 1, url, discarded: true}];
await run('check([diagnosticBinding])');
assert.equal(observations.at(-1).observation.reason, 'tab-suspended');
context.chrome.tabs.query = originalTabs;
const originalScript = context.chrome.scripting.executeScript;
context.chrome.scripting.executeScript = async () => { throw Error('PRIVATE exception sentinel'); };
run('delete diagnosticBinding.uncertain');
await run('check([diagnosticBinding])');
assert.equal(observations.at(-1).observation.reason, 'execution-failed');
assert.equal(observations.at(-1).observation.attempt.outcome, 'execution-failed');
const attemptId = observations.at(-1).observation.attempt.id;
assert.equal(run('diagnosticBinding.uncertain'), true);
context.chrome.scripting.executeScript = originalScript;
await run('check([diagnosticBinding])');
assert.equal(observations.at(-1).observation.attempt.id, attemptId, 'old attempt remains attributable');
assert.equal(observations.at(-1).observation.reason, 'uncertain-wake');
context.fetch = async (request, options) => request.endsWith('/poll?name=mail')
  ? Promise.reject(Error('PRIVATE network sentinel')) : originalFetch(request, options);
await run('check([diagnosticBinding])');
assert.equal(observations.at(-1).observation.reason, 'source-unavailable');
assert.equal(observations.at(-1).observation.pending_count, null, 'failed poll is not empty mail');
context.fetch = async (request, options) => request.endsWith('/report') && JSON.parse(options.body).observation
  ? {ok: false, status: 413} : originalFetch(request, options);
run('loggedObservations.clear()');
await run('check([diagnosticBinding])');
assert.equal(clicks, diagnosticClicks, 'diagnostic failure never clears fence or causes another click');
context.fetch = originalFetch;
await run('check([diagnosticBinding])');
assert.equal(observations.at(-1).observation.reason, 'uncertain-wake', 'failed upload is retried');
assert.ok(observations.some(p => p.observation.attempt?.outcome === 'click-attempted'));
assert.ok(observations.some(p => p.observation.reason === 'bound-tab-missing'));
assert.ok(observations.every(p => p.delivery_ids.length === 0 && !p.incident));
assert.ok(!JSON.stringify(observations).includes('PRIVATE'));
const observationModel = spawnSync('/home/marco/switchstand/.venv/bin/python', ['-c',
  'import json,sys; from switchstand.chatgpt_wake import _Report; [_Report.model_validate(x) for x in json.load(sys.stdin)]'],
  {input: JSON.stringify(observations), encoding: 'utf8', env: {...process.env, PYTHONPATH: new URL('../../src', import.meta.url).pathname}});
assert.equal(observationModel.status, 0, observationModel.stderr);
console.log('PASS: attributable blocker/action diagnostics, dedupe, safe failure/retry, typed producer contract');

context.fetch = originalFetch;
editor.value = 'Process your mailbox';
send.disabled = true;
assert.equal(run(`composer('${url}', 'send', true).detail`), 'send-control-disabled');
send.disabled = false;
editor.value = 'Unrelated draft';
assert.equal(run(`composer('${url}', 'prepare', true).detail`), 'unrelated-draft');
editor.value = '';
attached = true;
assert.equal(run(`composer('${url}', 'prepare', true).detail`), 'attachment-present');
attached = false;
assert.ok(observations.some(p => p.observation.attempt?.detail === 'click-dispatched'));
console.log('PASS: composer logs distinguish disabled Send, unrelated draft, attachment and dispatched click');

// Exercise the actual registered toolbar listener, including persistence failure.
const rearmBinding = {id, name:'mail', generation:1, url, armed:false, uncertain:true,
  ids:[id], seen:[nextId], wakeAttempt:{id, action:'send'}, alert:{incident_id:nextId}};
state={minimalBindings:[structuredClone(rearmBinding),{...rearmBinding,id:nextId},
  {...rearmBinding,id:'removed',removed:true}]};
run(`cancelled.add('${id}')`);
const beforeRearm=structuredClone(state);
const realSave=context.chrome.storage.local.set;
const beforeRearmClicks=clicks;
context.chrome.storage.local.set=async()=>{throw Error('storage failed')};
await assert.rejects(toolbarHandlers[0](),/storage failed/);
assert.deepEqual(state,beforeRearm,'failed storage cannot pretend success');
context.chrome.storage.local.set=realSave;
await toolbarHandlers[0]();
assert.deepEqual(state.minimalBindings,beforeRearm.minimalBindings.map(b=>b.removed?b:{...b,uncertain:false,armed:true,status:'Armed'}));
assert.equal(run(`cancelled.has('${id}')`),false);
assert.equal(clicks,beforeRearmClicks,'no inline composer action');
const onceRearmed=structuredClone(state);
await toolbarHandlers[0]();
assert.deepEqual(state,onceRearmed,'second click is idempotent');
console.log('PASS: registered toolbar Re-arm all, save failure, all active chats, removed/history preserved, zero inline send');

// Independent result expectations; unknown/failed actions remain fenced after restart.
const realExecution = context.chrome.scripting.executeScript;
for (const [outcome, safe] of [['busy',true],['waiting',true],['ready',true],['draft-present',true],
  ['wrong-conversation',true],['composer-unavailable',true],['insert-failed',true],
  ['click-attempted',false],['future-result',false],[undefined,false],['throw',false]]) {
  const seeded = {id, name:'mail', generation:1, url, armed:true, ids:[id], seen:[nextId],
    alert:{incident_id:nextId}, alertDone:true};
  state = {minimalBindings:[structuredClone(seeded)]};
  let executions = 0;
  context.chrome.scripting.executeScript = async () => {
    executions++;
    if (outcome === 'throw') throw Error('injected execution failure');
    return [{result:outcome === undefined ? undefined : {outcome}}];
  };
  await run('load().then(check)');
  assert.equal(Boolean(state.minimalBindings[0].uncertain), !safe, `fence: ${outcome}`);
  for (const key of ['ids','seen','alert']) assert.deepEqual(state.minimalBindings[0][key],seeded[key]);
  if (!safe) {
    const callsBeforeRestart = executions;
    const fresh = vm.createContext({...context});
    vm.runInContext(source,fresh);
    await vm.runInContext('load().then(check)',fresh);
    assert.equal(executions,callsBeforeRestart, `restart cannot repeat ${outcome}`);
  }
}
state = {minimalBindings:[{id,name:'mail',generation:1,url,armed:true}]};
let effectsBeforeSave = 0;
context.chrome.scripting.executeScript = async () => { effectsBeforeSave++; return []; };
context.chrome.storage.local.set = async () => { throw Error('pre-effect storage failed'); };
await assert.rejects(run('load().then(check)'), /pre-effect storage failed/);
assert.equal(effectsBeforeSave,0,'failed pre-effect persistence cannot execute composer');
context.chrome.storage.local.set = realSave;
context.chrome.scripting.executeScript = realExecution;
console.log('PASS: independent safe-result table, unsafe restart fences and failed pre-effect save');
