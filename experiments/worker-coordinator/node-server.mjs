import { createServer } from 'node:http';

export function createCoordinatorServer(handler) {
  return createServer(async (incoming, outgoing) => {
    try {
      const chunks = [];
      for await (const chunk of incoming) chunks.push(chunk);
      const body = Buffer.concat(chunks);
      const request = new Request(`http://127.0.0.1${incoming.url}`, {
        method: incoming.method,
        headers: incoming.headers,
        body: body.length ? body : undefined,
      });
      const response = await handler(request);
      outgoing.statusCode = response.status;
      for (const [name, value] of response.headers) outgoing.setHeader(name, value);
      outgoing.end(Buffer.from(await response.arrayBuffer()));
    } catch {
      outgoing.statusCode = 500;
      outgoing.setHeader('content-type', 'application/json');
      outgoing.end('{"error":"temporary_failure"}');
    }
  });
}
