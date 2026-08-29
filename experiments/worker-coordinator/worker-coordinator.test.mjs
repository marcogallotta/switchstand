import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import test from 'node:test';
import { gzipSync } from 'node:zlib';

import { PGlite } from '@electric-sql/pglite';

import { createCoordinator } from './coordinator.mjs';
import { PostgresStore } from './postgres-store.mjs';
import { GitHubPublisher, runPublicationAttempt } from './publication-runner.mjs';
import { createCoordinatorServer } from './node-server.mjs';

const ROOT = new URL('.', import.meta.url);
const WORKER = '10000000-0000-4000-8000-000000000001';
const INSTANCE = '20000000-0000-4000-8000-000000000002';
const PUBLISHER = '30000000-0000-4000-8000-000000000003';
const WORKSPACE = '40000000-0000-4000-8000-000000000004';
const BASE = 'a'.repeat(40);
const THREAD = '50000000-0000-4000-8000-000000000005';

async function migrated(privileges = false) {
  const database = new PGlite();
  const files = ['0001_schema.sql', '0002_worker_routines.sql', '0003_publication_routines.sql'];
  if (privileges) files.push('0004_privileges.sql');
  for (const file of files) await database.exec(await readFile(new URL(file, ROOT), 'utf8'));
  return database;
}

async function seed(database, workspace = WORKSPACE, login = 'web_user') {
  const role = await database.query('SELECT oid FROM pg_roles WHERE rolname=$1', [login]);
  await database.query(
    `INSERT INTO coordinator_v2.service_binding(login_name,login_oid,workspace_id)
     VALUES($1,$2,$3)`,
    [login, role.rows[0].oid, workspace],
  );
  await database.query(
    `INSERT INTO coordinator_v2.repository_policy(
       workspace_id,repository_full_name,candidate_branch,allowed_path_prefixes)
     VALUES($1,'marcogallotta/switchstand','codex/postgres-g5-live',ARRAY['experiments/worker-coordinator'])`,
    [workspace],
  );
}

function admission(id = 'admission:test-001', source = 'Add one bounded coordinator fixture.') {
  return {
    admission_id: id,
    work_type: 'implementation',
    source_text: source,
    acceptance: ['one bounded file changes', 'all focused checks pass'],
    repository: {
      full_name: 'marcogallotta/switchstand',
      base_sha: BASE,
      candidate_branch: 'codex/postgres-g5-live',
      allowed_path_prefixes: ['experiments/worker-coordinator'],
    },
  };
}

function withAuthority(claim, fields) {
  return {
    protocol: 'worker-v2',
    work_id: claim.work_id,
    worker_id: claim.worker_id,
    instance_id: claim.instance_id,
    fence: Number(claim.fence),
    lease_token: claim.lease_token,
    cancellation_version: Number(claim.cancellation_version),
    ...fields,
  };
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function redigest(value) {
  const copy = structuredClone(value);
  delete copy.request_digest;
  copy.request_digest = createHash('sha256').update(canonical(copy)).digest('hex');
  return copy;
}

function candidate(claim, operation = 'candidate:test-001') {
  const content = Buffer.from('bounded\n');
  const value = withAuthority(claim, {
    operation_id: operation,
    base_sha: BASE,
    expected_branch_head: BASE,
    message: 'Add bounded fixture',
    files: [
      {
        path: 'experiments/worker-coordinator/fixture.txt',
        mode: '100644',
        type: 'blob',
        content_base64: content.toString('base64'),
        decoded_bytes: content.byteLength,
        sha256: createHash('sha256').update(content).digest('hex'),
      },
    ],
    deletions: [],
    check_summaries: [{ name: 'focused', outcome: 'PASS', summary: 'passed' }],
  });
  value.request_digest = createHash('sha256')
    .update(canonical(value))
    .digest('hex');
  return value;
}

test('PGlite applies the g5 migrations on PostgreSQL 17', async () => {
  const database = await migrated(true);
  const version = await database.query('SHOW server_version');
  assert.match(version.rows[0].server_version, /^17\./);
  const tables = await database.query(
    `SELECT count(*)::int AS count FROM information_schema.tables WHERE table_schema='coordinator_v2'`,
  );
  assert.equal(tables.rows[0].count, 9);
  await database.close();
});

test('admission is exact, idempotent, policy-bound, and atomically rolled back', async () => {
  const database = await migrated();
  await seed(database);
  const store = new PostgresStore(database);
  const first = await store.call('admit', [admission()]);
  assert.equal(first.created, true);
  assert.equal((await store.call('admit', [admission()])).work_id, first.work_id);
  await assert.rejects(() => store.call('admit', [admission('admission:test-001', 'changed')]), /idempotency_conflict/);
  await assert.rejects(
    () => store.call('admit', [{ ...admission('admission:test-002'), repository: {
      ...admission().repository, candidate_branch: 'forbidden',
    } }]),
    /policy_denied/,
  );
  await assert.rejects(
    database.transaction(async (transaction) => {
      const nested = new PostgresStore({ transaction: (run) => run(transaction) });
      await nested.call('admit', [admission('admission:test-rollback')]);
      throw new Error('forced_rollback');
    }),
    /forced_rollback/,
  );
  const rolledBack = await database.query(
    `SELECT count(*)::int AS count FROM coordinator_v2.work_admission WHERE admission_id='admission:test-rollback'`,
  );
  assert.equal(rolledBack.rows[0].count, 0);
  await database.close();
});

test('claim, checkpoint replay, expiry fencing, and candidate durability follow g5', async () => {
  const database = await migrated();
  await seed(database);
  const store = new PostgresStore(database);
  const admitted = await store.call('admit', [admission()]);
  const first = await store.call('claim', [WORKER, INSTANCE]);
  assert.equal(first.work_id, admitted.work_id);
  assert.equal(Number(first.fence), 1);
  assert.deepEqual(first.repository, admission().repository);
  const checkpoint = withAuthority(first, {
    operation_id: 'checkpoint:test-001', sequence: 1, phase: 'codex_started',
    codex_thread_id: THREAD, checkpoint_state: 'thread_adopted',
  });
  const response = await store.call('checkpoint', [checkpoint]);
  assert.deepEqual(await store.call('checkpoint', [checkpoint]), response);
  await assert.rejects(
    () => store.call('checkpoint', [{ ...checkpoint, checkpoint_state: 'changed' }]),
    /idempotency_conflict/,
  );
  await database.query(
    `UPDATE coordinator_v2.work SET lease_expires_at=clock_timestamp()-interval '1 second'
     WHERE work_id=$1`,
    [first.work_id],
  );
  const second = await store.call('claim', [WORKER, INSTANCE]);
  assert.equal(Number(second.fence), 2);
  assert.equal(second.admission_sha, first.admission_sha);
  assert.equal(second.source_text, first.source_text);
  assert.deepEqual(second.prior_checkpoint, {
    sequence: 1, phase: 'codex_started', codex_thread_id: THREAD, checkpoint_state: 'thread_adopted',
  });
  assert.deepEqual(await store.call('checkpoint', [checkpoint]), response);
  await assert.rejects(
    () => store.call('checkpoint', [withAuthority(second, {
      operation_id: checkpoint.operation_id, sequence: checkpoint.sequence, phase: checkpoint.phase,
      codex_thread_id: checkpoint.codex_thread_id, checkpoint_state: checkpoint.checkpoint_state,
    })]),
    /stale_or_invalid_lease/,
  );
  const accepted = await store.call('candidate', [candidate(second)]);
  assert.equal(accepted.status, 'candidate_ready');
  const exported = await store.call('exportWork', [first.work_id, true]);
  assert.equal(exported.source_text, admission().source_text);
  assert.equal(exported.accepted_candidate.manifest_sha, accepted.manifest_sha);
  assert.ok(exported.accepted_candidate.canonical_manifest_base64.length > 100);
  await database.close();
});

test('authorization is ordered against cancellation and publication remains reconcilable', async () => {
  const database = await migrated();
  await seed(database);
  const store = new PostgresStore(database);
  const work = await store.call('admit', [admission()]);
  const claim = await store.call('claim', [WORKER, INSTANCE]);
  await store.call('candidate', [candidate(claim)]);
  const authorized = await store.call('authorize', [work.work_id]);
  assert.equal(authorized.state, 'authorized');
  const cancelled = await store.call('cancel', [work.work_id]);
  assert.equal(cancelled.result, 'publication_already_authorized');
  const attempt = await store.call('claimPublication', [PUBLISHER]);
  assert.equal(Number(attempt.attempt), 1);
  const objects = await store.call('recordObjects', [attempt.publication_id, attempt.attempt,
    PUBLISHER, attempt.publisher_token, 'b'.repeat(40), 'c'.repeat(40)]);
  assert.equal(objects.state, 'reconciling');
  const publish = await store.call('observePublication', [attempt.publication_id, attempt.attempt,
    PUBLISHER, attempt.publisher_token, null, BASE, false]);
  assert.equal(publish.directive, 'publish');
  const applied = await store.call('observePublication', [attempt.publication_id, attempt.attempt,
    PUBLISHER, attempt.publisher_token, 'c'.repeat(40), BASE, false]);
  assert.equal(applied.state, 'applied');
  const observations = await database.query(
    'SELECT count(*)::int AS count FROM coordinator_v2.publication_observation WHERE publication_id=$1',
    [attempt.publication_id],
  );
  assert.equal(observations.rows[0].count, 4);
  await database.close();
});

test('server rejects forged digests, oversized files, and paths outside admission', async () => {
  const database = await migrated();
  await seed(database);
  const store = new PostgresStore(database);
  await store.call('admit', [admission()]);
  const claim = await store.call('claim', [WORKER, INSTANCE]);
  const wrongRequest = candidate(claim, 'candidate:bad-request');
  wrongRequest.request_digest = '0'.repeat(64);
  await assert.rejects(() => store.call('candidate', [wrongRequest]), /invalid_request/);
  const wrongFile = candidate(claim, 'candidate:bad-file');
  wrongFile.files[0].sha256 = '0'.repeat(64);
  await assert.rejects(() => store.call('candidate', [redigest(wrongFile)]), /invalid_request/);
  const outside = candidate(claim, 'candidate:outside');
  outside.files[0].path = 'outside.txt';
  await assert.rejects(() => store.call('candidate', [redigest(outside)]), /policy_denied/);
  const oversized = candidate(claim, 'candidate:oversized');
  const content = Buffer.alloc(65537, 97);
  oversized.files[0].content_base64 = content.toString('base64');
  oversized.files[0].decoded_bytes = content.byteLength;
  oversized.files[0].sha256 = createHash('sha256').update(content).digest('hex');
  await assert.rejects(() => store.call('candidate', [redigest(oversized)]), /invalid_request/);
  await database.close();
});

test('review work cannot submit a candidate and requires an explicit verdict', async () => {
  const database = await migrated();
  await seed(database);
  const store = new PostgresStore(database);
  await store.call('admit', [{ ...admission('admission:review-001'), work_type: 'review' }]);
  const claim = await store.call('claim', [WORKER, INSTANCE]);
  await assert.rejects(() => store.call('candidate', [candidate(claim)]), /work_type_forbidden/);
  const complete = withAuthority(claim, {
    operation_id: 'complete:review-001', status: 'succeeded', candidate_id: null,
    review_verdict: null, summary_code: 'reviewed', checks: [{ name: 'review', outcome: 'PASS' }],
  });
  await assert.rejects(() => store.call('complete', [complete]), /invalid_request/);
  complete.review_verdict = 'PASS';
  const result = await store.call('complete', [complete]);
  assert.equal(result.status, 'succeeded');
  await database.close();
});

test('cancel before authorization permanently prevents publication', async () => {
  const database = await migrated();
  await seed(database);
  const store = new PostgresStore(database);
  const work = await store.call('admit', [admission()]);
  const claim = await store.call('claim', [WORKER, INSTANCE]);
  await store.call('candidate', [candidate(claim)]);
  assert.equal((await store.call('cancel', [work.work_id])).result, 'cancelled');
  await assert.rejects(() => store.call('authorize', [work.work_id]), /invalid_request/);
  assert.equal(await store.call('claimPublication', [PUBLISHER]), null);
  await database.close();
});

test('duplicate publishers share one immutable plan and divergent objects fail closed', async () => {
  const database = await migrated();
  await seed(database);
  const store = new PostgresStore(database);
  const work = await store.call('admit', [admission()]);
  const claim = await store.call('claim', [WORKER, INSTANCE]);
  await store.call('candidate', [candidate(claim)]);
  await store.call('authorize', [work.work_id]);
  const first = await store.call('claimPublication', [PUBLISHER]);
  await store.call('recordObjects', [first.publication_id, first.attempt, PUBLISHER,
    first.publisher_token, 'b'.repeat(40), 'c'.repeat(40)]);
  const secondPublisher = '31000000-0000-4000-8000-000000000003';
  const second = await store.call('claimPublication', [secondPublisher]);
  assert.equal(Number(second.attempt), 2);
  assert.equal(second.plan_sha, first.plan_sha);
  assert.equal(second.desired_commit_sha, 'c'.repeat(40));
  const failed = await store.call('recordObjects', [second.publication_id, second.attempt,
    secondPublisher, second.publisher_token, 'b'.repeat(40), 'd'.repeat(40)]);
  assert.equal(failed.state, 'failed');
  assert.equal(failed.error, 'determinism_violation');
  await database.close();
});

test('provider marker is the close fence for stale heads and delayed publication races', async () => {
  const database = await migrated();
  await seed(database);
  const store = new PostgresStore(database);
  const work = await store.call('admit', [admission()]);
  const claim = await store.call('claim', [WORKER, INSTANCE]);
  await store.call('candidate', [candidate(claim)]);
  await store.call('authorize', [work.work_id]);
  const attempt = await store.call('claimPublication', [PUBLISHER]);
  await store.call('recordObjects', [attempt.publication_id, attempt.attempt, PUBLISHER,
    attempt.publisher_token, 'b'.repeat(40), 'c'.repeat(40)]);
  const close = await store.call('observePublication', [attempt.publication_id, attempt.attempt,
    PUBLISHER, attempt.publisher_token, null, 'd'.repeat(40), false]);
  assert.equal(close.directive, 'close_expected');
  assert.equal(close.seal_reason, 'stale_head');
  const sealed = await store.call('observePublication', [attempt.publication_id, attempt.attempt,
    PUBLISHER, attempt.publisher_token, BASE, 'd'.repeat(40), false]);
  assert.equal(sealed.state, 'stale_head');
  await database.close();
});

test('runtime has execute-only authority and cross-workspace reads disclose nothing', async () => {
  const database = await migrated(true);
  await database.exec(`
    CREATE ROLE switchstand_coordinator_runtime_b LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE;
    GRANT USAGE ON SCHEMA coordinator_v2 TO switchstand_coordinator_runtime_b;
    GRANT EXECUTE ON FUNCTION coordinator_v2.export_work_v2(text,boolean) TO switchstand_coordinator_runtime_b;
  `);
  await seed(database, WORKSPACE, 'switchstand_coordinator_runtime');
  const other = '60000000-0000-4000-8000-000000000006';
  await seed(database, other, 'switchstand_coordinator_runtime_b');
  await database.exec('SET SESSION AUTHORIZATION switchstand_coordinator_runtime');
  const store = new PostgresStore(database);
  const work = await store.call('admit', [admission()]);
  await assert.rejects(() => database.query('SELECT * FROM coordinator_v2.work'), /permission denied/);
  await database.exec('RESET SESSION AUTHORIZATION');
  await database.exec('SET SESSION AUTHORIZATION switchstand_coordinator_runtime_b');
  await assert.rejects(() => store.call('exportWork', [work.work_id, false]), /not_found/);
  await database.exec('RESET SESSION AUTHORIZATION');
  await database.close();
});

test('HTTP adapter rejects duplicate keys and keeps worker/coordinator authority separate', async () => {
  const calls = [];
  const handler = createCoordinator({
    store: { call: async (name, values) => { calls.push([name, values]); return null; } },
    workerKey: 'worker', coordinatorKey: 'coordinator', publisherKey: 'publisher',
    checkoutProvider: async () => ({ body: new Uint8Array(), sha256: '0'.repeat(64) }),
  });
  const duplicate = await handler(new Request('https://coordinator.example/v2/work/claim', {
    method: 'POST', headers: { authorization: 'Bearer worker' },
    body: `{"protocol":"worker-v2","protocol":"worker-v2","worker_id":"${WORKER}","instance_id":"${INSTANCE}"}`,
  }));
  assert.equal(duplicate.status, 400);
  const wrongKey = await handler(new Request('https://coordinator.example/v2/work/admit', {
    method: 'POST', headers: { authorization: 'Bearer worker' }, body: JSON.stringify(admission()),
  }));
  assert.equal(wrongKey.status, 401);
  const oversized = await handler(new Request('https://coordinator.example/v2/work/admit', {
    method: 'POST', headers: { authorization: 'Bearer coordinator' }, body: 'x'.repeat(16385),
  }));
  assert.equal(oversized.status, 413);
  const register = await handler(new Request('https://coordinator.example/v2/workers/register', {
    method: 'POST', headers: { authorization: 'Bearer worker' }, body: JSON.stringify({
      protocol: 'worker-v2', worker_id: WORKER, instance_id: INSTANCE,
      capabilities: ['adopted_thread_resume_v1', 'candidate_manifest_v1', 'codex_exec_v1', 'read_isolation_bwrap_v1'],
    }),
  }));
  assert.equal(register.status, 200);
  assert.equal(calls.length, 0);
  const zippedCandidate = {
    protocol: 'worker-v2', work_id: 'work:http-zip', worker_id: WORKER, instance_id: INSTANCE,
    fence: 1, lease_token: 'A'.repeat(43), cancellation_version: 0,
    operation_id: 'candidate:http-zip', base_sha: BASE, expected_branch_head: BASE,
    message: 'zip', files: [], deletions: [{ path: 'experiments/worker-coordinator/old.txt' }],
    check_summaries: [], request_digest: '0'.repeat(64),
  };
  const zipped = await handler(new Request(
    'https://coordinator.example/v2/work/work%3Ahttp-zip/candidate',
    {
      method: 'POST',
      headers: { authorization: 'Bearer worker', 'content-encoding': 'gzip' },
      body: gzipSync(JSON.stringify(zippedCandidate)),
    },
  ));
  assert.equal(zipped.status, 200);
  assert.equal(calls.at(-1)[0], 'candidate');
});

test('publisher emits deterministic objects, sha:null deletion, and atomic target plus marker CAS', async () => {
  const bodies = [];
  const manifest = {
    files: [{ path: 'experiments/worker-coordinator/a.txt', content_base64: Buffer.from('a').toString('base64') }],
    deletions: [{ path: 'experiments/worker-coordinator/old.txt' }],
  };
  const bytes = new TextEncoder().encode(JSON.stringify(manifest));
  const digest = Buffer.from(await crypto.subtle.digest('SHA-256', bytes)).toString('hex');
  const plan = {
    publication_id: '70000000-0000-4000-8000-000000000007',
    plan_sha: '8'.repeat(64), manifest_sha: digest,
    canonical_manifest_base64: Buffer.from(bytes).toString('base64'),
    repository_full_name: 'marcogallotta/switchstand', candidate_branch: 'codex/postgres-g5-live',
    expected_head: BASE, message: 'bounded', marker_ref: 'refs/tags/switchstand-publications/marker',
    author: { name: 'Switchstand Coordinator', email: 'x@example.com', date: '2026-08-29T00:00:00Z' },
  };
  const fetch = async (url, init) => {
    const path = new URL(url).pathname;
    const body = init?.body ? JSON.parse(init.body) : null;
    bodies.push([path, body]);
    if (path.endsWith(`/git/commits/${BASE}`)) return Response.json({ tree: { sha: 'a'.repeat(40) } });
    if (path.endsWith('/git/blobs')) return Response.json({ sha: 'b'.repeat(40) });
    if (path.endsWith('/git/trees')) return Response.json({ sha: 'c'.repeat(40) });
    if (path.endsWith('/git/commits')) return Response.json({ sha: 'd'.repeat(40) });
    if (path === '/repos/marcogallotta/switchstand') return Response.json({ node_id: 'R_node' });
    if (path === '/graphql') return Response.json({ data: { updateRefs: { clientMutationId: plan.plan_sha } } });
    return new Response('', { status: 404 });
  };
  const publisher = new GitHubPublisher({ fetch, token: 'secret' });
  const objects = await publisher.objects(plan);
  assert.deepEqual(objects, { treeSha: 'c'.repeat(40), commitSha: 'd'.repeat(40) });
  plan.desired_commit_sha = objects.commitSha;
  await publisher.publish(plan);
  const tree = bodies.find(([path]) => path.endsWith('/git/trees'))[1].tree;
  assert.equal(bodies.find(([path]) => path.endsWith('/git/trees'))[1].base_tree, 'a'.repeat(40));
  assert.deepEqual(tree[1], {
    path: 'experiments/worker-coordinator/old.txt', mode: '100644', type: 'blob', sha: null,
  });
  const mutation = bodies.find(([path]) => path === '/graphql')[1].variables.input.refUpdates;
  assert.equal(mutation.length, 2);
  assert.equal(mutation[0].beforeOid, BASE);
  assert.equal(mutation[1].beforeOid, '0'.repeat(40));
});

test('lost provider response stays reconciling and never becomes not-applied by time', async () => {
  const claim = { desired_commit_sha: 'd'.repeat(40), expected_head: BASE };
  const result = await runPublicationAttempt({
    claim,
    coordinator: { recordObjects: async () => assert.fail(), observe: async () => assert.fail() },
    publisher: { readRefs: async () => { throw new Error('lost'); } },
  });
  assert.equal(result.outcome, 'reconciling');
});

test('merged Python worker client completes the exact HTTP journey against PGlite', async () => {
  const database = await migrated();
  await seed(database);
  const archive = gzipSync('bounded checkout');
  const archiveDigest = Buffer.from(await crypto.subtle.digest('SHA-256', archive)).toString('hex');
  const handler = createCoordinator({
    store: new PostgresStore(database),
    workerKey: 'worker-secret', coordinatorKey: 'coordinator-secret', publisherKey: 'publisher-secret',
    checkoutProvider: async () => ({ body: archive, sha256: archiveDigest }),
  });
  const server = createCoordinatorServer(handler);
  await new Promise((resolve, reject) => server.listen(0, '127.0.0.1', (error) => error ? reject(error) : resolve()));
  const address = server.address();
  const baseUrl = `http://127.0.0.1:${address.port}`;
  const admitted = await fetch(`${baseUrl}/v2/work/admit`, {
    method: 'POST', headers: { authorization: 'Bearer coordinator-secret', 'content-type': 'application/json' },
    body: JSON.stringify(admission('admission:http-001')),
  });
  assert.equal(admitted.status, 201);
  const script = `
import base64, hashlib, json, os, uuid
from switchstand_worker.protocol import CoordinatorClient
client=CoordinatorClient(os.environ['BASE_URL'], 'worker-secret', timeout=5)
worker='${WORKER}'; instance='${INSTANCE}'; thread='${THREAD}'
client.register(worker, instance)
claim=client.claim(worker, instance)
assert claim is not None
data, headers=client.checkout(claim)
assert data
client.checkpoint(claim.authority, 'checkpoint:http-001', 1, 'codex_started', thread, 'thread_adopted')
content=b'bounded\\n'
manifest={
  'operation_id':'candidate:http-001','base_sha':'${BASE}','expected_branch_head':'${BASE}',
  'message':'Add bounded fixture','files':[{'path':'experiments/worker-coordinator/http.txt',
  'mode':'100644','type':'blob','content_base64':base64.b64encode(content).decode(),
  'decoded_bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}],
  'deletions':[],'check_summaries':[{'name':'focused','outcome':'PASS','summary':'passed'}]
}
body={**claim.authority.fields(),**manifest}
manifest['request_digest']=hashlib.sha256(json.dumps(body,separators=(',',':'),sort_keys=True).encode()).hexdigest()
accepted=client.submit_candidate(claim.authority, manifest)
client.complete(claim.authority, 'complete:http-001', status='succeeded',
  candidate_id=accepted['candidate_id'], summary_code='done', checks=[{'name':'focused','outcome':'PASS'}])
print('PYTHON_CLIENT_PASS')
`;
  const child = spawn('python', ['-c', script], {
    cwd: new URL('../..', import.meta.url).pathname,
    env: { ...process.env, BASE_URL: baseUrl, PYTHONPATH: 'src' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  let errors = '';
  child.stdout.on('data', (chunk) => { output += chunk; });
  child.stderr.on('data', (chunk) => { errors += chunk; });
  const exit = await new Promise((resolve) => child.on('close', resolve));
  server.close();
  await database.close();
  assert.equal(exit, 0, errors);
  assert.match(output, /PYTHON_CLIENT_PASS/);
});
