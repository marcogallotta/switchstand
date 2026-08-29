const CAPABILITIES = Object.freeze([
  'adopted_thread_resume_v1',
  'candidate_manifest_v1',
  'codex_exec_v1',
  'read_isolation_bwrap_v1',
]);
const WORK_ID = /^[A-Za-z0-9._:-]{8,80}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function json(body, status = 200) {
  return Response.json(body, { status, headers: { 'cache-control': 'no-store' } });
}

function failure(code, status) {
  return json({ error: code }, status);
}

function exact(value, keys) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.keys(value).sort().join('\0') === [...keys].sort().join('\0')
  );
}

function parseStrict(text) {
  let offset = 0;
  const whitespace = () => {
    while (/[ \t\r\n]/.test(text[offset] || '')) offset += 1;
  };
  const string = () => {
    const start = offset;
    offset += 1;
    while (offset < text.length) {
      if (text[offset] === '\\') {
        offset += text[offset + 1] === 'u' ? 6 : 2;
      } else if (text[offset] === '"') {
        offset += 1;
        return JSON.parse(text.slice(start, offset));
      } else {
        offset += 1;
      }
    }
    throw new Error('invalid_json');
  };
  const value = () => {
    whitespace();
    if (text[offset] === '"') return string();
    if (text[offset] === '{') {
      const result = Object.create(null);
      const seen = new Set();
      offset += 1;
      whitespace();
      if (text[offset] === '}') {
        offset += 1;
        return result;
      }
      while (true) {
        whitespace();
        if (text[offset] !== '"') throw new Error('invalid_json');
        const key = string();
        if (seen.has(key)) throw new Error('duplicate_key');
        seen.add(key);
        whitespace();
        if (text[offset] !== ':') throw new Error('invalid_json');
        offset += 1;
        result[key] = value();
        whitespace();
        if (text[offset] === '}') {
          offset += 1;
          return result;
        }
        if (text[offset] !== ',') throw new Error('invalid_json');
        offset += 1;
      }
    }
    if (text[offset] === '[') {
      const result = [];
      offset += 1;
      whitespace();
      if (text[offset] === ']') {
        offset += 1;
        return result;
      }
      while (true) {
        result.push(value());
        whitespace();
        if (text[offset] === ']') {
          offset += 1;
          return result;
        }
        if (text[offset] !== ',') throw new Error('invalid_json');
        offset += 1;
      }
    }
    const match = text.slice(offset).match(/^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/);
    if (!match) throw new Error('invalid_json');
    offset += match[0].length;
    return JSON.parse(match[0]);
  };
  const parsed = value();
  whitespace();
  if (offset !== text.length) throw new Error('invalid_json');
  return parsed;
}

async function boundedBytes(stream, limit) {
  if (!stream) throw Object.assign(new Error('invalid_request'), { status: 400 });
  const reader = stream.getReader();
  const chunks = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > limit) {
      await reader.cancel();
      throw Object.assign(new Error('request_too_large'), { status: 413 });
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

async function requestBody(request, limit, allowGzip = false) {
  const declared = Number(request.headers.get('content-length') || 0);
  if (!Number.isSafeInteger(declared) || declared < 0 || declared > limit) {
    throw Object.assign(new Error('request_too_large'), { status: 413 });
  }
  let bytes = await boundedBytes(request.body, limit);
  const encoding = request.headers.get('content-encoding');
  if (encoding !== null) {
    if (!allowGzip || encoding !== 'gzip') {
      throw Object.assign(new Error('invalid_request'), { status: 400 });
    }
    try {
      const decompressed = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
      bytes = await boundedBytes(decompressed, limit);
    } catch {
      throw Object.assign(new Error('invalid_request'), { status: 400 });
    }
  }
  try {
    return parseStrict(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } catch {
    throw Object.assign(new Error('invalid_request'), { status: 400 });
  }
}

function authorized(request, key) {
  return typeof key === 'string' && request.headers.get('authorization') === `Bearer ${key}`;
}

function authority(value, workId) {
  const keys = [
    'protocol',
    'work_id',
    'worker_id',
    'instance_id',
    'fence',
    'lease_token',
    'cancellation_version',
  ];
  const selected = Object.fromEntries(keys.map((key) => [key, value[key]]));
  if (
    selected.protocol !== 'worker-v2' ||
    selected.work_id !== workId ||
    !UUID.test(selected.worker_id || '') ||
    !UUID.test(selected.instance_id || '') ||
    !Number.isSafeInteger(selected.fence) ||
    selected.fence < 1 ||
    !/^[A-Za-z0-9_-]{43}$/.test(selected.lease_token || '') ||
    !Number.isSafeInteger(selected.cancellation_version) ||
    selected.cancellation_version < 0
  ) {
    throw Object.assign(new Error('invalid_request'), { status: 400 });
  }
  return selected;
}

function checkoutAuthority(request, workId) {
  const value = {
    protocol: 'worker-v2',
    work_id: workId,
    worker_id: request.headers.get('x-worker-id'),
    instance_id: request.headers.get('x-instance-id'),
    fence: Number(request.headers.get('x-lease-fence')),
    lease_token: request.headers.get('x-lease-token'),
    cancellation_version: Number(request.headers.get('x-cancellation-version')),
  };
  return authority(value, workId);
}

async function checkoutArchive(value) {
  if (!exact(value, ['body', 'sha256']) || !(value.body instanceof Uint8Array) ||
      value.body.byteLength > 8 * 1024 * 1024 || !/^[0-9a-f]{64}$/.test(value.sha256 || '')) {
    throw new Error('checkout_provider_invalid');
  }
  const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', value.body))]
    .map((byte) => byte.toString(16).padStart(2, '0')).join('');
  if (digest !== value.sha256) throw new Error('checkout_provider_invalid');
  return value;
}

const ERROR_STATUS = Object.freeze({
  unauthorized: 401,
  invalid_request: 400,
  policy_denied: 403,
  work_type_forbidden: 403,
  not_found: 404,
  idempotency_conflict: 409,
  stale_or_invalid_lease: 409,
  terminal_immutable: 409,
  stale_head: 409,
  publication_already_authorized: 409,
  request_too_large: 413,
  temporary_failure: 503,
  invalid_admission_binding: 503,
});

function fixedError(error) {
  const code = error.fixedCode || error.message;
  return ERROR_STATUS[code]
    ? failure(code === 'invalid_admission_binding' ? 'temporary_failure' : code, ERROR_STATUS[code])
    : failure('temporary_failure', 503);
}

export function createCoordinator({
  store,
  workerKey,
  coordinatorKey,
  publisherKey,
  checkoutProvider,
  clock = () => new Date(),
}) {
  const keys = [workerKey, coordinatorKey, publisherKey];
  if (keys.some((key) => typeof key !== 'string' || key.length === 0) || new Set(keys).size !== keys.length) {
    throw new Error('invalid_authority_configuration');
  }
  async function workerMutation(request, workId, kind, keys, limit, allowGzip = false) {
    if (!authorized(request, workerKey)) return failure('unauthorized', 401);
    const value = await requestBody(request, limit, allowGzip);
    if (!exact(value, keys)) return failure('invalid_request', 400);
    authority(value, workId);
    return json(await store.call(kind, [value]));
  }

  return async (request) => {
    try {
      const url = new URL(request.url);
      if (url.search || url.hash) return failure('invalid_request', 400);
      const path = url.pathname;
      if (request.method === 'POST' && path === '/v2/work/admit') {
        if (!authorized(request, coordinatorKey)) return failure('unauthorized', 401);
        const value = await requestBody(request, 16384);
        return json(await store.call('admit', [value]), 201);
      }
      if (request.method === 'POST' && path === '/v2/workers/register') {
        if (!authorized(request, workerKey)) return failure('unauthorized', 401);
        const value = await requestBody(request, 4096);
        if (
          !exact(value, ['protocol', 'worker_id', 'instance_id', 'capabilities']) ||
          value.protocol !== 'worker-v2' ||
          !UUID.test(value.worker_id || '') ||
          !UUID.test(value.instance_id || '') ||
          JSON.stringify(value.capabilities) !== JSON.stringify(CAPABILITIES)
        ) return failure('invalid_request', 400);
        return json({
          protocol: 'worker-v2',
          worker_id: value.worker_id,
          poll_after_seconds: 2,
          lease_seconds: 15,
          renew_after_seconds: 1,
          server_time: clock().toISOString(),
        });
      }
      if (request.method === 'POST' && path === '/v2/work/claim') {
        if (!authorized(request, workerKey)) return failure('unauthorized', 401);
        const value = await requestBody(request, 4096);
        if (
          !exact(value, ['protocol', 'worker_id', 'instance_id']) ||
          value.protocol !== 'worker-v2' ||
          !UUID.test(value.worker_id || '') ||
          !UUID.test(value.instance_id || '')
        ) return failure('invalid_request', 400);
        const result = await store.call('claim', [value.worker_id, value.instance_id]);
        return result === null ? new Response(null, { status: 204 }) : json(result);
      }
      const route = path.match(/^\/v2\/work\/([^/]+)\/(checkout|renew|checkpoint|candidate|complete)$/);
      if (route) {
        const workId = decodeURIComponent(route[1]);
        if (!WORK_ID.test(workId)) return failure('invalid_request', 400);
        if (route[2] === 'checkout' && request.method === 'GET') {
          if (!authorized(request, workerKey)) return failure('unauthorized', 401);
          const permission = await store.call('checkout', [checkoutAuthority(request, workId)]);
          const archive = await checkoutArchive(await checkoutProvider(permission));
          return new Response(archive.body, {
            status: 200,
            headers: {
              'content-type': 'application/gzip',
              'content-length': String(archive.body.byteLength),
              'x-base-sha': permission.base_sha,
              'x-archive-sha256': archive.sha256,
              'cache-control': 'no-store',
            },
          });
        }
        if (request.method !== 'POST') return failure('not_found', 404);
        if (route[2] === 'renew') {
          if (!authorized(request, workerKey)) return failure('unauthorized', 401);
          const value = await requestBody(request, 4096);
          const renewKeys = [
            'protocol', 'work_id', 'worker_id', 'instance_id', 'fence',
            'lease_token', 'cancellation_version',
          ];
          if (!exact(value, renewKeys)) {
            return failure('invalid_request', 400);
          }
          authority(value, workId);
          return json(await store.call('renew', [value]));
        }
        const common = [
          'protocol', 'work_id', 'worker_id', 'instance_id', 'fence',
          'lease_token', 'cancellation_version',
        ];
        if (route[2] === 'checkpoint') return workerMutation(request, workId, 'checkpoint', [...common,
          'operation_id', 'sequence', 'phase', 'codex_thread_id', 'checkpoint_state'], 8192);
        if (route[2] === 'candidate') return workerMutation(request, workId, 'candidate', [...common,
          'operation_id', 'base_sha', 'expected_branch_head', 'message', 'files', 'deletions',
          'check_summaries', 'request_digest'], 393216, true);
        return workerMutation(request, workId, 'complete', [...common, 'operation_id', 'status',
          'candidate_id', 'review_verdict', 'summary_code', 'checks'], 16384);
      }
      if (request.method === 'POST' && path === '/v2/publications/claim') {
        if (!authorized(request, publisherKey)) return failure('unauthorized', 401);
        const value = await requestBody(request, 4096);
        if (!exact(value, ['publisher_instance']) || !UUID.test(value.publisher_instance || '')) {
          return failure('invalid_request', 400);
        }
        const result = await store.call('claimPublication', [value.publisher_instance]);
        return result === null ? new Response(null, { status: 204 }) : json(result);
      }
      const publication = path.match(/^\/v2\/publications\/([0-9a-f-]+)\/(objects|observe|fail)$/);
      if (request.method === 'POST' && publication) {
        if (!authorized(request, publisherKey)) return failure('unauthorized', 401);
        const value = await requestBody(request, 8192);
        if (!UUID.test(publication[1]) || !UUID.test(value.publisher_instance || '') ||
            !Number.isSafeInteger(value.attempt) || value.attempt < 1 ||
            !/^[A-Za-z0-9_-]{43}$/.test(value.publisher_token || '')) {
          return failure('invalid_request', 400);
        }
        const prefix = [publication[1], value.attempt, value.publisher_instance, value.publisher_token];
        if (publication[2] === 'fail') {
          if (!exact(value, ['attempt', 'publisher_instance', 'publisher_token'])) {
            return failure('invalid_request', 400);
          }
          return json(await store.call('failPublication', prefix));
        }
        if (publication[2] === 'objects') {
          if (!exact(value, ['attempt', 'publisher_instance', 'publisher_token', 'tree_sha', 'commit_sha']) ||
              !/^[0-9a-f]{40}$/.test(value.tree_sha || '') ||
              !/^[0-9a-f]{40}$/.test(value.commit_sha || '')) {
            return failure('invalid_request', 400);
          }
          return json(await store.call('recordObjects', [...prefix, value.tree_sha, value.commit_sha]));
        }
        if (!exact(value, [
          'attempt', 'publisher_instance', 'publisher_token', 'marker_sha',
          'target_sha', 'permanent_error',
        ]) || (value.marker_sha !== null && !/^[0-9a-f]{40}$/.test(value.marker_sha || '')) ||
          (value.target_sha !== null && !/^[0-9a-f]{40}$/.test(value.target_sha || '')) ||
          typeof value.permanent_error !== 'boolean') {
          return failure('invalid_request', 400);
        }
        return json(await store.call('observePublication', [...prefix, value.marker_sha,
          value.target_sha, value.permanent_error]));
      }
      const control = path.match(/^\/v2\/work\/([^/]+)\/(cancel|authorize-publication)$/);
      if (request.method === 'POST' && control) {
        if (!authorized(request, coordinatorKey)) return failure('unauthorized', 401);
        const workId = decodeURIComponent(control[1]);
        if (!WORK_ID.test(workId)) return failure('invalid_request', 400);
        const declared = Number(request.headers.get('content-length') || 0);
        if (!Number.isSafeInteger(declared) || declared !== 0 || request.body !== null) {
          return failure('request_too_large', 413);
        }
        return json(await store.call(control[2] === 'cancel' ? 'cancel' : 'authorize', [workId]));
      }
      return failure('not_found', 404);
    } catch (error) {
      return fixedError(error);
    }
  };
}
