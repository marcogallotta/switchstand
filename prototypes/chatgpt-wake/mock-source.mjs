import {createServer} from 'node:http';
import {randomUUID} from 'node:crypto';
import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';

// Fixed ephemeral local-test auth matches the unpacked extension, not Switchstand auth.
export function createMockSource(token = '23e43e46f4e7a58a1ffb2ae5893a16cf8eeceeab806792e8') {
  let id = '';
  const server = createServer((request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    if (request.headers.authorization !== `Bearer ${token}`) {
      response.writeHead(401).end(); return;
    }
    if (request.method !== 'GET' || request.url !== '/wake') {
      response.writeHead(404).end(); return;
    }
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({id}));
  });
  return {server, token, wake() { id = randomUUID(); return id; }};
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const source = createMockSource();
  source.server.listen(Number(process.env.WAKE_PORT || 18765), '127.0.0.1', () => {
    console.log(`Mock only. Port ${source.server.address().port}; local-test auth preconfigured.`);
    console.log('Type wake + Enter to publish one coalescible event; Ctrl-C stops.');
  });
  createInterface({input: process.stdin}).on('line', line => {
    if (line.trim() === 'wake') console.log(`Mock wake ${source.wake()}`);
  });
}
