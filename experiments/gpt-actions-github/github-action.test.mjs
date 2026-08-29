import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createGithubAction, createHostedGithubHandler, DEFAULT_POLICY, D1OperationStore, MemoryOperationStore } from "./github-action.mjs";

const expected = "1".repeat(40);
const commit = "2".repeat(40);
const tree = "3".repeat(40);
const blob = "4".repeat(40);

function encodedFile(path, content) {
  const bytes = Buffer.isBuffer(content) ? content : Buffer.from(content);
  return {
    path,
    content_base64: bytes.toString("base64"),
    expected_bytes: bytes.length,
    expected_sha256: createHash("sha256").update(bytes).digest("hex"),
  };
}

test("hosted authentication rejects requests before requiring GitHub authority", async () => {
  const handler = createHostedGithubHandler({ ACTION_KEY: "action-only" });
  const missing = await handler(new Request("https://action.test/v1/github/commit", { method: "POST" }));
  const wrong = await handler(new Request("https://action.test/v1/github/commit", {
    method: "POST",
    headers: { authorization: "Bearer wrong" },
  }));
  assert.equal(missing.status, 401);
  assert.equal(wrong.status, 401);
});

function request(overrides = {}) {
  const body = {
    operation_id: "op-github-0001",
    repository: "marcogallotta/gpt-actions-github-fixture",
    branch: "gpt-actions-controlled-github-feasibility",
    expected_head_sha: expected,
    mode: "create",
    message: "Add GPT Action feasibility fixture",
    files: [encodedFile("experiments/gpt-actions-github/a.txt", "alpha\n")],
    ...overrides,
  };
  return new Request("https://action.test/v1/github/commit", { method: "POST", body: JSON.stringify(body) });
}

function referencedRequest(content, overrides = {}) {
  const file = encodedFile("experiments/gpt-actions-github/referenced.txt", content);
  return request({
    files: undefined,
    file_manifest: [{
      file_id: "file-test-001",
      path: file.path,
      expected_bytes: file.expected_bytes,
      expected_sha256: file.expected_sha256,
    }],
    openaiFileIdRefs: [{
      name: "referenced.txt",
      id: "file-test-001",
      mime_type: "text/plain",
      download_link: "https://files.oaiusercontent.com/file-test-001?sig=temporary",
    }],
    ...overrides,
  });
}

function fakeGithub({ branchExists = false, failRefOnce = false, mainSha = expected } = {}) {
  const calls = [];
  let ref = branchExists ? expected : null;
  let shouldFail = failRefOnce;
  const fn = async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    const path = new URL(url).pathname;
    if (init.method === "GET" && path.includes("/git/ref/heads/")) {
      if (path.endsWith("/heads/main")) return Response.json({ object: { sha: mainSha } });
      return ref ? Response.json({ object: { sha: ref } }) : Response.json({ message: "Not Found" }, { status: 404 });
    }
    if (init.method === "GET" && path.includes("/git/commits/")) return Response.json({ tree: { sha: "5".repeat(40) } });
    if (path.endsWith("/git/blobs")) return Response.json({ sha: blob });
    if (path.endsWith("/git/trees")) return Response.json({ sha: tree });
    if (path.endsWith("/git/commits")) return Response.json({ sha: commit });
    if (path.endsWith("/git/refs") && init.method === "POST") {
      if (shouldFail) { shouldFail = false; return Response.json({ message: "injected" }, { status: 503 }); }
      ref = commit; return Response.json({ object: { sha: ref } });
    }
    if (path.includes("/git/refs/heads/") && init.method === "PATCH") { ref = commit; return Response.json({ object: { sha: ref } }); }
    throw new Error(`unexpected ${init.method} ${path}`);
  };
  return { fn, calls, getRef: () => ref };
}

function pullRequest(overrides = {}) {
  return new Request("https://action.test/v1/github/pull", {
    method: "POST",
    body: JSON.stringify({
      operation_id: "op-pull-000001",
      repository: "marcogallotta/gpt-actions-github-fixture",
      branch: "gpt-actions-controlled-github-feasibility",
      base: "main",
      expected_head_sha: commit,
      mode: "create",
      title: "GPT Action GitHub feasibility proof",
      body: "Bounded experiment.",
      draft: true,
      ...overrides,
    }),
  });
}

function fakePullGithub() {
  const calls = [];
  let pull = null;
  const fn = async (url, init) => {
    const path = `${new URL(url).pathname}${new URL(url).search}`;
    const bodyValue = init.body && JSON.parse(init.body);
    calls.push({ path, method: init.method, body: bodyValue });
    if (init.method === "GET" && path.includes("/git/ref/heads/")) return Response.json({ object: { sha: commit } });
    if (init.method === "GET" && path.includes("/pulls?")) return Response.json(pull ? [pull] : []);
    if (init.method === "POST" && path.endsWith("/pulls")) {
      pull = {
        number: 7,
        html_url: "https://github.com/marcogallotta/gpt-actions-github-fixture/pull/7",
        head: { ref: bodyValue.head, sha: commit, repo: { full_name: "marcogallotta/gpt-actions-github-fixture" } },
        base: { ref: bodyValue.base, repo: { full_name: "marcogallotta/gpt-actions-github-fixture" } },
      };
      return Response.json(pull);
    }
    if (init.method === "GET" && path.endsWith("/pulls/7")) return Response.json(pull);
    if (init.method === "PATCH" && path.endsWith("/pulls/7")) { pull = { ...pull, ...bodyValue }; return Response.json(pull); }
    throw new Error(`unexpected ${init.method} ${path}`);
  };
  return { fn, calls, getPull: () => pull };
}

async function body(response) { return response.json(); }

test("creates blobs, tree, commit, and branch; exact retry is idempotent", async () => {
  const store = new MemoryOperationStore();
  const gh = fakeGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const first = await action(request());
  assert.equal(first.status, 200);
  assert.equal((await body(first)).commit_sha, commit);
  assert.equal(gh.getRef(), commit);
  const callCount = gh.calls.length;
  const retry = await action(request());
  assert.equal(retry.status, 200);
  assert.equal((await body(retry)).created, false);
  assert.equal(gh.calls.length, callCount);
});

test("rejects a concurrent duplicate while the first operation owns the lease", async () => {
  const store = new MemoryOperationStore();
  const gh = fakeGithub();
  let unblock;
  const gate = new Promise((resolve) => { unblock = resolve; });
  let firstCall = true;
  const delayedFetch = async (...args) => {
    if (firstCall) { firstCall = false; await gate; }
    return gh.fn(...args);
  };
  const action = createGithubAction({ store, githubFetch: delayedFetch, token: "server-only" });
  const running = action(request());
  await new Promise((resolve) => setTimeout(resolve, 0));
  const duplicate = await action(request());
  assert.equal(duplicate.status, 409);
  assert.equal((await body(duplicate)).error, "operation_in_progress");
  unblock();
  assert.equal((await running).status, 200);
});

test("creates multiple files as one tree without upload transport", async () => {
  const store = new MemoryOperationStore();
  const gh = fakeGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const response = await action(request({
    operation_id: "op-github-bundle-1",
    files: [
      encodedFile("experiments/gpt-actions-github/a.txt", "alpha\n"),
      encodedFile("experiments/gpt-actions-github/nested/b.txt", "beta\n"),
      encodedFile("experiments/gpt-actions-github/manifest.json", '{"files":2}\n'),
    ],
  }));
  assert.equal(response.status, 200);
  const treeCall = gh.calls.find((call) => call.url.endsWith("/git/trees"));
  assert.equal(treeCall.body.tree.length, 3);
});

test("rejects forbidden repository, branch, path, and changed retry", async () => {
  const store = new MemoryOperationStore();
  const gh = fakeGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  for (const [change, error] of [
    [{ repository: "marcogallotta/switchstand" }, "forbidden_repository"],
    [{ branch: "main" }, "forbidden_branch"],
    [{ files: [encodedFile("README.md", "a")] }, "forbidden_path"],
  ]) {
    const response = await action(request(change));
    assert.equal(response.status, 403);
    assert.equal((await body(response)).error, error);
  }
  assert.equal((await action(request())).status, 200);
  const conflict = await action(request({ message: "different" }));
  assert.equal(conflict.status, 409);
  assert.equal((await body(conflict)).error, "idempotency_conflict");
});

test("enforces exact request shape, exact branch, and UTF-8 text", async () => {
  let index = 0;
  for (const [change, error, status] of [
    [{ unexpected: true }, "invalid_request", 400],
    [{ operation_id: "bad id" }, "invalid_operation_id", 400],
    [{ branch: "agent/gpt-actions-github-proof" }, "forbidden_branch", 403],
    [{ message: " padded" }, "invalid_message", 400],
    [{ test_fail_after_commit: "yes" }, "invalid_request", 400],
    [{ files: [encodedFile("experiments/gpt-actions-github/sp ace.txt", "a")] }, "invalid_path", 400],
    [{ files: [{
      ...encodedFile("experiments/gpt-actions-github/bad.txt", Buffer.from([255])),
    }] }, "invalid_utf8", 400],
  ]) {
    const gh = fakeGithub();
    const action = createGithubAction({ store: new MemoryOperationStore(), githubFetch: gh.fn, token: "server-only" });
    const response = await action(request({ operation_id: `shape-test-000${index++}`, ...change }));
    assert.equal(response.status, status);
    assert.equal((await body(response)).error, error);
    assert.equal(gh.calls.length, 0);
  }
});

test("enforces file-count, total-content, and request limits before GitHub", async () => {
  const cases = [
    {
      operation_id: "limit-files-0001",
      files: Array.from({ length: 33 }, (_, i) => encodedFile(`experiments/gpt-actions-github/${i}.txt`, "a")),
      error: "invalid_file_count",
      status: 400,
    },
    {
      operation_id: "limit-total-0001",
      files: Array.from({ length: 5 }, (_, i) => (
        encodedFile(`experiments/gpt-actions-github/${i}.txt`, Buffer.alloc(53 * 1024, "a"))
      )),
      error: "content_too_large",
      status: 413,
    },
    { operation_id: "limit-body-00001", message: "x".repeat(384 * 1024), error: "request_too_large", status: 413 },
  ];
  for (const { error, status, ...change } of cases) {
    const gh = fakeGithub();
    const action = createGithubAction({ store: new MemoryOperationStore(), githubFetch: gh.fn, token: "server-only" });
    const response = await action(request(change));
    assert.equal(response.status, status);
    assert.equal((await body(response)).error, error);
    assert.equal(gh.calls.length, 0);
  }
});

test("rejects stale update without moving the ref", async () => {
  const store = new MemoryOperationStore();
  const gh = fakeGithub({ branchExists: true });
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const response = await action(request({ mode: "update", expected_head_sha: "9".repeat(40) }));
  assert.equal(response.status, 409);
  assert.equal((await body(response)).error, "stale_head");
  assert.equal(gh.getRef(), expected);
  assert.equal(gh.calls.some((c) => c.method === "PATCH"), false);
});

test("rejects branch creation from a stale base head", async () => {
  const store = new MemoryOperationStore();
  const gh = fakeGithub({ mainSha: "8".repeat(40) });
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const response = await action(request());
  assert.equal(response.status, 409);
  assert.equal((await body(response)).error, "stale_head");
  assert.equal(gh.getRef(), null);
  assert.equal(gh.calls.some((call) => call.method === "POST"), false);
});

test("recovers after a partial failure without corrupting the branch", async () => {
  const store = new MemoryOperationStore();
  const gh = fakeGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const failed = await action(request({ test_fail_after_commit: true }));
  assert.equal(failed.status, 503);
  assert.equal(gh.getRef(), null);
  const recovered = await action(request({ test_fail_after_commit: true }));
  assert.equal(recovered.status, 200);
  assert.equal(gh.getRef(), commit);
  assert.equal((await body(recovered)).recovered, true);
});

test("accepts exactly 64 KiB and rejects one byte more before any GitHub call", async () => {
  const acceptedGithub = fakeGithub();
  const acceptedAction = createGithubAction({
    store: new MemoryOperationStore(),
    githubFetch: acceptedGithub.fn,
    token: "server-only",
  });
  const accepted = await acceptedAction(request({
    operation_id: "limit-file-exact-64k",
    files: [encodedFile("experiments/gpt-actions-github/exact.txt", Buffer.alloc(64 * 1024, "a"))],
  }));
  assert.equal(accepted.status, 200);
  const blobCall = acceptedGithub.calls.find((call) => call.url.endsWith("/git/blobs"));
  assert.equal(Buffer.from(blobCall.body.content, "base64").length, 64 * 1024);

  const store = new MemoryOperationStore();
  const gh = fakeGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const response = await action(request({
    files: [{
      ...encodedFile("experiments/gpt-actions-github/big.txt", Buffer.alloc(64 * 1024 + 1, "a")),
      expected_bytes: 64 * 1024,
    }],
  }));
  assert.equal(response.status, 413);
  assert.equal((await body(response)).error, "file_too_large");
  assert.equal(gh.calls.length, 0);
});

test("rejects byte-count and SHA-256 mismatches before any GitHub call", async () => {
  const correct = encodedFile("experiments/gpt-actions-github/integrity.txt", "actual content\n");
  for (const file of [
    { ...correct, expected_bytes: correct.expected_bytes + 1 },
    { ...correct, expected_sha256: "0".repeat(64) },
  ]) {
    const gh = fakeGithub();
    const store = new MemoryOperationStore();
    const action = createGithubAction({
      store,
      githubFetch: gh.fn,
      token: "server-only",
    });
    const response = await action(request({ files: [file] }));
    assert.equal(response.status, 409);
    const result = await body(response);
    assert.equal(result.error, "content_integrity_mismatch");
    assert.equal(result.expected_bytes, file.expected_bytes);
    assert.equal(result.actual_bytes, correct.expected_bytes);
    assert.equal(result.received_base64_characters, correct.content_base64.length);
    assert.match(result.hint, /complete Base64 payload inline/);
    assert.equal(gh.calls.length, 0);
    assert.equal(store.records.size, 0);
  }
});

test("fetches an official Action file reference and commits its exact verified bytes", async () => {
  const content = Buffer.alloc(5 * 1024, "a");
  const gh = fakeGithub();
  const fileCalls = [];
  const action = createGithubAction({
    store: new MemoryOperationStore(),
    githubFetch: gh.fn,
    fileFetch: async (url, init) => {
      fileCalls.push({ url, init });
      return new Response(content, { headers: { "content-length": String(content.length) } });
    },
    token: "server-only",
  });
  const response = await action(referencedRequest(content));
  assert.equal(response.status, 200);
  assert.equal(fileCalls.length, 1);
  assert.equal(fileCalls[0].init.redirect, "error");
  assert.equal(new URL(fileCalls[0].url).hostname, "files.oaiusercontent.com");
  const blobCall = gh.calls.find((call) => call.url.endsWith("/git/blobs"));
  assert.deepEqual(Buffer.from(blobCall.body.content, "base64"), content);
});

test("file references reject unsafe targets, oversized content, and integrity drift before GitHub", async () => {
  const content = Buffer.from("shortened");
  const cases = [
    {
      request: referencedRequest(content, {
        openaiFileIdRefs: [{
          name: "bad.txt", id: "file-test-001", mime_type: "text/plain",
          download_link: "https://example.com/file",
        }],
      }),
      fileFetch: async () => { throw new Error("must not fetch"); },
      status: 403,
      error: "invalid_file_reference",
    },
    {
      request: referencedRequest(Buffer.alloc(64 * 1024, "a")),
      fileFetch: async () => new Response(Buffer.alloc(64 * 1024 + 1, "a")),
      status: 413,
      error: "file_too_large",
    },
    {
      request: referencedRequest(Buffer.alloc(5 * 1024, "a")),
      fileFetch: async () => new Response(content),
      status: 409,
      error: "content_integrity_mismatch",
      actualBytes: content.length,
      hint: /Regenerate and reattach the complete file/,
    },
  ];
  for (const item of cases) {
    const store = new MemoryOperationStore();
    const gh = fakeGithub();
    const action = createGithubAction({ store, githubFetch: gh.fn, fileFetch: item.fileFetch, token: "server-only" });
    const response = await action(item.request);
    const result = await body(response);
    assert.equal(response.status, item.status);
    assert.equal(result.error, item.error);
    if (item.actualBytes !== undefined) assert.equal(result.actual_bytes, item.actualBytes);
    if (item.hint) {
      assert.match(result.hint, item.hint);
      assert.equal(result.received_base64_characters, undefined);
    }
    assert.equal(gh.calls.length, 0);
    assert.equal(store.records.size, 0);
  }
});

test("file-reference IDs must map one-to-one before any download", async () => {
  const content = Buffer.from("content");
  const store = new MemoryOperationStore();
  const gh = fakeGithub();
  let fetches = 0;
  const action = createGithubAction({
    store,
    githubFetch: gh.fn,
    fileFetch: async () => { fetches += 1; return new Response(content); },
    token: "server-only",
  });
  const expectedFile = encodedFile("experiments/gpt-actions-github/referenced.txt", content);
  const response = await action(referencedRequest(content, {
    file_manifest: [{
      file_id: "different-id",
      path: expectedFile.path,
      expected_bytes: expectedFile.expected_bytes,
      expected_sha256: expectedFile.expected_sha256,
    }],
  }));
  assert.equal(response.status, 409);
  assert.equal((await body(response)).error, "file_reference_mismatch");
  assert.equal(fetches, 0);
  assert.equal(gh.calls.length, 0);
  assert.equal(store.records.size, 0);
});

test("rejects malformed integrity declarations before operation storage or GitHub", async () => {
  const valid = encodedFile("experiments/gpt-actions-github/integrity.txt", "content\n");
  for (const file of [
    { ...valid, expected_bytes: 64 * 1024 + 1 },
    { ...valid, expected_sha256: 7 },
    { ...valid, expected_sha256: "A".repeat(64) },
  ]) {
    const gh = fakeGithub();
    const store = new MemoryOperationStore();
    const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
    const response = await action(request({ files: [file] }));
    assert.equal(response.status, 400);
    assert.equal((await body(response)).error, "invalid_file");
    assert.equal(gh.calls.length, 0);
    assert.equal(store.records.size, 0);
  }
});

test("creates a draft PR and makes exact retries idempotent", async () => {
  const store = new MemoryOperationStore();
  const gh = fakePullGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const first = await action(pullRequest());
  assert.equal(first.status, 200);
  assert.equal((await body(first)).pull_number, 7);
  const count = gh.calls.length;
  const retry = await action(pullRequest());
  assert.equal(retry.status, 200);
  assert.equal(gh.calls.length, count);
});

test("PR create defaults draft to true and counts Unicode code points", async () => {
  const gh = fakePullGithub();
  const action = createGithubAction({ store: new MemoryOperationStore(), githubFetch: gh.fn, token: "server-only" });
  const response = await action(pullRequest({ draft: undefined, title: "😀".repeat(120) }));
  assert.equal(response.status, 200);
  const create = gh.calls.find((call) => call.method === "POST" && call.path.endsWith("/pulls"));
  assert.equal(create.body.draft, true);
});

test("PR policy rejects forbidden base and stale head", async () => {
  const store = new MemoryOperationStore();
  const gh = fakePullGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const forbidden = await action(pullRequest({ base: "release" }));
  assert.equal(forbidden.status, 403);
  assert.equal((await body(forbidden)).error, "forbidden_base");
  const stale = await action(pullRequest({ expected_head_sha: "9".repeat(40) }));
  assert.equal(stale.status, 409);
  assert.equal((await body(stale)).error, "stale_head");
});

test("PR validation requires exact text and a pull number for updates", async () => {
  for (const [change, error] of [
    [{ operation_id: "bad id" }, "invalid_operation_id"],
    [{ title: " padded" }, "invalid_title"],
    [{ draft: "yes" }, "invalid_request"],
    [{ test_fail_after_mutation: 1 }, "invalid_request"],
    [{ mode: "update" }, "invalid_pull_number"],
  ]) {
    const gh = fakePullGithub();
    const action = createGithubAction({ store: new MemoryOperationStore(), githubFetch: gh.fn, token: "server-only" });
    const response = await action(pullRequest(change));
    assert.equal(response.status, 400);
    assert.equal((await body(response)).error, error);
    assert.equal(gh.calls.length, 0);
  }
});

test("OpenAPI exposes only runtime routes and mirrors generated-call constraints", async () => {
  const schema = await readFile(new URL("./openapi.yaml", import.meta.url), "utf8");
  assert.equal(schema.includes("/v1/github/initialize"), false);
  assert.match(schema, /\^\[A-Za-z0-9\]\[A-Za-z0-9\._:-\]\{7,79\}\$/);
  assert.match(schema, /gpt-actions-controlled-github-feasibility/);
  assert.match(schema, /maxItems: 32/);
  assert.match(schema, /maxLength: 87384/);
  assert.match(schema, /x-decodedAggregateMaxBytes: 262144/);
  assert.match(schema, /x-bodyMaxBytes: 393216/);
  assert.match(schema, /contentEncoding: base64/);
  assert.match(schema, /required: \[path, content_base64, expected_bytes, expected_sha256\]/);
  assert.match(schema, /maximum: 65536/);
  assert.match(schema, /pattern: '\^\[0-9a-f\]\{64\}\$'/);
  assert.match(schema, /openaiFileIdRefs:/);
  assert.match(schema, /maxItems: 10/);
  assert.match(schema, /file_manifest:/);
  assert.match(schema, /oneOf:/);
  assert.match(schema, /const: update/);
  assert.match(schema, /required: \[pull_number\]/);
});

test("recovers a PR create after the mutation response is lost", async () => {
  const store = new MemoryOperationStore();
  const gh = fakePullGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const failed = await action(pullRequest({ test_fail_after_mutation: true }));
  assert.equal(failed.status, 503);
  assert.equal(gh.getPull().number, 7);
  const recovered = await action(pullRequest({ test_fail_after_mutation: true }));
  assert.equal(recovered.status, 200);
  assert.equal((await body(recovered)).recovered, true);
  assert.equal(gh.calls.filter((c) => c.method === "POST" && c.path.endsWith("/pulls")).length, 1);
});

test("PR create recovery rejects a fork or unexpected head SHA", async () => {
  const gh = fakePullGithub();
  const action = createGithubAction({ store: new MemoryOperationStore(), githubFetch: gh.fn, token: "server-only" });
  assert.equal((await action(pullRequest())).status, 200);
  gh.getPull().head.repo.full_name = "outsider/fork";
  const fork = await action(pullRequest({ operation_id: "op-pull-recover-fork" }));
  assert.equal(fork.status, 403);
  assert.equal((await body(fork)).error, "forbidden_pull");
  gh.getPull().head.repo.full_name = "marcogallotta/gpt-actions-github-fixture";
  gh.getPull().head.sha = "9".repeat(40);
  const stale = await action(pullRequest({ operation_id: "op-pull-recover-stale" }));
  assert.equal(stale.status, 409);
  assert.equal((await body(stale)).error, "stale_head");
});

test("updates only an allowlisted PR at the fenced head", async () => {
  const store = new MemoryOperationStore();
  const gh = fakePullGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  assert.equal((await action(pullRequest())).status, 200);
  const updated = await action(pullRequest({
    operation_id: "op-pull-update-1",
    mode: "update",
    pull_number: 7,
    title: "Updated bounded proof",
    body: "Updated through the hosted Action.",
  }));
  assert.equal(updated.status, 200);
  assert.equal(gh.getPull().title, "Updated bounded proof");
});

test("rejects a matching branch name when the PR head comes from a fork", async () => {
  const store = new MemoryOperationStore();
  const gh = fakePullGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  assert.equal((await action(pullRequest())).status, 200);
  gh.getPull().head.repo.full_name = "outsider/fork";
  const response = await action(pullRequest({ operation_id: "op-pull-fork-01", mode: "update", pull_number: 7 }));
  assert.equal(response.status, 403);
  assert.equal((await body(response)).error, "forbidden_pull");
});

class FakeD1 {
  constructor() { this.rows = new Map(); this.now = 1000; }
  prepare(sql) {
    return { bind: (...args) => ({ run: () => this.run(sql, args), first: () => this.first(sql, args) }) };
  }
  async first(sql, [id, attempt]) {
    if (!sql.startsWith("SELECT")) return null;
    const row = this.rows.get(id);
    if (!row) return null;
    if (sql.includes("lease_until>unixepoch()") &&
        (row.status !== "running" || row.attempt !== attempt || row.lease_until <= this.now)) return null;
    return structuredClone(row);
  }
  async run(sql, args) {
    let changes = 0;
    if (sql.startsWith("INSERT OR IGNORE")) {
      const [id, hash, leaseSeconds] = args;
      if (!this.rows.has(id)) {
        this.rows.set(id, { operation_id: id, payload_hash: hash, status: "running", commit_sha: null, result_json: null, lease_until: this.now + leaseSeconds, attempt: 1 });
        changes = 1;
      }
    } else if (sql.includes("attempt=attempt+1")) {
      const [leaseSeconds, id] = args; const row = this.rows.get(id);
      if (row?.status === "running" && row.lease_until <= this.now) { row.attempt += 1; row.lease_until = this.now + leaseSeconds; changes = 1; }
    } else if (sql.startsWith("UPDATE github_operations SET commit_sha")) {
      const [sha, id, attempt] = args; const row = this.rows.get(id);
      if (row?.status === "running" && row.attempt === attempt) { row.commit_sha = sha; changes = 1; }
    } else if (sql.startsWith("UPDATE github_operations SET status='complete'")) {
      const [result, id, attempt] = args; const row = this.rows.get(id);
      if (row?.status === "running" && row.attempt === attempt) { row.status = "complete"; row.result_json = result; changes = 1; }
    } else if (sql.startsWith("UPDATE github_operations SET lease_until=0")) {
      const [id, attempt] = args; const row = this.rows.get(id);
      if (row?.status === "running" && row.attempt === attempt) { row.lease_until = 0; changes = 1; }
    }
    return { meta: { changes } };
  }
}

test("an expired request cannot mutate GitHub after its operation is reclaimed", async () => {
  assert.ok(DEFAULT_POLICY.operationLeaseSeconds * 1000 > DEFAULT_POLICY.maxRequestMs);
  const db = new FakeD1();
  const store = new D1OperationStore(db);
  const calls = [];
  let reclaimed = false;
  const githubFetch = async (url, init) => {
    const path = new URL(url).pathname;
    calls.push({ method: init.method, path });
    if (init.method === "GET" && path.includes("/git/ref/heads/")) {
      return path.endsWith("/heads/main")
        ? Response.json({ object: { sha: expected } })
        : Response.json({ message: "Not Found" }, { status: 404 });
    }
    if (init.method === "GET" && path.includes("/git/commits/")) {
      db.now += DEFAULT_POLICY.operationLeaseSeconds + 1;
      const row = db.rows.get("op-github-0001");
      const next = await store.begin(row.operation_id, row.payload_hash, DEFAULT_POLICY.operationLeaseSeconds);
      assert.equal(next.record.attempt, 2);
      reclaimed = true;
      return Response.json({ tree: { sha: "5".repeat(40) } });
    }
    throw new Error(`stale request attempted ${init.method} ${path}`);
  };
  const action = createGithubAction({
    store,
    githubFetch,
    token: "server-only",
    now: () => db.now * 1000,
  });
  const response = await action(request());
  assert.equal(reclaimed, true);
  assert.equal(response.status, 503);
  assert.equal((await body(response)).error, "operation_deadline_exceeded");
  assert.equal(calls.some((call) => call.method !== "GET"), false);
});

test("D1 reclaim fences every stale writer mutation", async () => {
  const db = new FakeD1();
  const store = new D1OperationStore(db);
  const first = await store.begin("op-d1-fence-001", "a".repeat(64));
  assert.equal(first.record.attempt, 1);
  db.now += 61;
  const second = await store.begin("op-d1-fence-001", "a".repeat(64));
  assert.equal(second.record.attempt, 2);
  await assert.rejects(store.saveCommit("op-d1-fence-001", "1".repeat(40), 1), /stale_operation_attempt/);
  await store.release("op-d1-fence-001", 1);
  assert.equal(db.rows.get("op-d1-fence-001").lease_until, db.now + 60);
  await store.saveCommit("op-d1-fence-001", "2".repeat(40), 2);
  await store.complete("op-d1-fence-001", { ok: true }, 2);
  assert.equal(db.rows.get("op-d1-fence-001").status, "complete");
});
