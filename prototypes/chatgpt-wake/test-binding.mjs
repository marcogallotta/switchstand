import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const source = await readFile(new URL('./worker.js', import.meta.url), 'utf8');
const valid = runInNewContext(source.slice(source.indexOf('function validConversation'), source.indexOf('let serial')) + ';validConversation', {URL});
for (const url of ['https://chatgpt.com/c/id', 'https://chatgpt.com/c/id/?model=pro#bottom',
  'https://chatgpt.com/g/g-p-6a99c963c9d48191a717fc3a1a6cc774-switchstand/c/6ae856ae-fe30-83ed-a0b7-d77ded026f49']) assert.equal(valid(url), true);
for (const url of ['https://chatgpt.com/', 'https://evil.example/c/id',
  'https://chatgpt.com.evil.example/c/id', 'https://chatgpt.com/arbitrary/path/c/id',
  'https://user:pass@chatgpt.com/c/id']) assert.equal(valid(url), false);
console.log('PASS exact supported conversation routes and host guard.');
