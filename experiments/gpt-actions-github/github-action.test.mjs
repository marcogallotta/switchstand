import assert from "node:assert/strict";
import test from "node:test";
import { createGithubAction, MemoryOperationStore } from "./github-action.mjs";

const expected = "1".repeat(40);
const commit = "2".repeat(40);
const tree = "3".repeat(40);
const blob = "4".repeat(40);

function request(overrides = {}) {
  const body = {
    operation_id: "op-github-0001",
    repository: "marcogallotta/switchstand",
    branch: "agent/gpt-actions-github-proof",
    expected_head_sha: expected,
    mode: "create",
    message: "Add GPT Action feasibility fixture",
    files: [{ path: "experiments/gpt-actions-github/a.txt", content_base64: Buffer.from("alpha\n").toString("base64") }],
    ...overrides,
  };
  return new Request("https://action.test/v1/github/commit", { method: "POST", body: JSON.stringify(body) });
}

function fakeGithub({ branchExists = false, failRefOnce = false } = {}) {
  const calls = [];
  let ref = branchExists ? expected : null;
  let shouldFail = failRefOnce;
  const fn = async (url, init) => {
    calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
    const path = new URL(url).pathname;
    if (init.method === "GET" && path.includes("/git/ref/heads/")) {
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
      repository: "marcogallotta/switchstand",
      branch: "agent/gpt-actions-github-proof",
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
      pull = { number: 7, html_url: "https://github.com/marcogallotta/switchstand/pull/7", head: { ref: bodyValue.head, sha: commit }, base: { ref: bodyValue.base } };
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
      { path: "experiments/gpt-actions-github/a.txt", content_base64: btoa("alpha\n") },
      { path: "experiments/gpt-actions-github/nested/b.txt", content_base64: btoa("beta\n") },
      { path: "experiments/gpt-actions-github/manifest.json", content_base64: btoa('{"files":2}\n') },
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
    [{ repository: "marcogallotta/ai-tools" }, "forbidden_repository"],
    [{ branch: "main" }, "forbidden_branch"],
    [{ files: [{ path: "README.md", content_base64: "YQ==" }] }, "forbidden_path"],
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

test("rejects oversized files before any GitHub call", async () => {
  const store = new MemoryOperationStore();
  const gh = fakeGithub();
  const action = createGithubAction({ store, githubFetch: gh.fn, token: "server-only" });
  const response = await action(request({ files: [{ path: "experiments/gpt-actions-github/big.bin", content_base64: Buffer.alloc(16 * 1024 + 1).toString("base64") }] }));
  assert.equal(response.status, 413);
  assert.equal((await body(response)).error, "file_too_large");
  assert.equal(gh.calls.length, 0);
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
