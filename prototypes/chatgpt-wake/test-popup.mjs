import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const element = () => ({textContent: '', children: [], style: {}, append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; }});
const nodes = Object.fromEntries(['status', 'pairing', 'bind', 'escalation', 'bindings', 'name', 'verify', 'check', 'disarm'].map(id => ['#' + id, element()]));
const binding = {name: 'One', endpoint: 'a', armed: true, halted: true, url: 'PRIVATE_URL', status: 'attempt-outcome-unknown: PRIVATE_ERROR', checkedAt: 'PRIVATE_TIME', report: 'PRIVATE_REPORT'};
const calls = [];
runInNewContext(await readFile(new URL('./popup.js', import.meta.url), 'utf8'), {
  document: {querySelector: id => nodes[id], getElementById: id => nodes['#' + id], createElement: element},
  chrome: {storage: {local: {get: async () => ({wakeBindings: [binding], status: 'PRIVATE_GLOBAL', escalation: 'configured'})}},
    runtime: {sendMessage: async value => { calls.push(value); return {}; }}}
});
await new Promise(resolve => setTimeout(resolve, 0));
const row = nodes['#bindings'].children[0];
assert.equal(row.textContent, 'One: Needs attention');
assert.equal(nodes['#status'].textContent, '');
assert.deepEqual(row.children.slice(1).map(button => button.textContent), ['Stop', 'Remove']);
await row.children[2].onclick();
assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{type: 'remove', endpoint: 'a'}]);
console.log('PASS popup hides diagnostic content and Remove targets only its pairing.');
