const OP_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,79}$/;
const SHA_RE = /^[0-9a-f]{40}$/;
const PATH_RE = /^experiments\/gpt-actions-github\/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:\/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)*$/;
const ONE_LINE_RE = /^\S(?:[^\r\n]*\S)?$/;

export const DEFAULT_POLICY = Object.freeze({
  repository: "marcogallotta/gpt-actions-github-fixture",
  baseBranch: "main",
  candidateBranch: "gpt-actions-controlled-github-feasibility",
  pathPrefix: "experiments/gpt-actions-github/",
  maxBodyBytes: 32 * 1024,
  maxFileBytes: 4 * 1024,
  maxTotalBytes: 16 * 1024,
  maxFiles: 5,
  maxRequestMs: 40 * 1000,
  operationLeaseSeconds: 60,
  allowFaultInjection: true,
});

const COMMIT_KEYS = ["operation_id", "repository", "branch", "expected_head_sha", "mode", "message", "files", "test_fail_after_commit"];
const PULL_KEYS = ["operation_id", "repository", "branch", "base", "expected_head_sha", "mode", "pull_number", "title", "body", "draft", "test_fail_after_mutation"];

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${stable(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

async function digest(value) {
  const bytes = new TextEncoder().encode(stable(value));
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((n) => n.toString(16).padStart(2, "0")).join("");
}

function byteLength(value) {
  return new TextEncoder().encode(value).length;
}

function codePointLength(value) {
  return [...value].length;
}

function exactKeys(value, allowed, required) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return required.every((key) => keys.includes(key)) && keys.every((key) => allowed.includes(key));
}

function invalid(error, status = 400) {
  throw Object.assign(new Error(error), { status });
}

function decodeFile(file, policy) {
  if (!exactKeys(file, ["path", "content_base64"], ["path", "content_base64"]) ||
      typeof file.path !== "string" || typeof file.content_base64 !== "string") invalid("invalid_file");
  if (file.path.length > 240) invalid("invalid_path");
  if (file.path.includes("\\") || file.path.startsWith("/") || file.path.split("/").some((p) => !p || p === "." || p === "..")) {
    throw Object.assign(new Error("forbidden_path"), { status: 403 });
  }
  if (!file.path.startsWith(policy.pathPrefix)) {
    throw Object.assign(new Error("forbidden_path"), { status: 403 });
  }
  if (!PATH_RE.test(file.path)) invalid("invalid_path");
  let binary;
  try { binary = atob(file.content_base64); } catch { throw Object.assign(new Error("invalid_base64"), { status: 400 }); }
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  const normalized = btoa(String.fromCharCode(...bytes));
  if (normalized.replace(/=+$/, "") !== file.content_base64.replace(/=+$/, "")) {
    throw Object.assign(new Error("invalid_base64"), { status: 400 });
  }
  if (bytes.length > policy.maxFileBytes) {
    throw Object.assign(new Error("file_too_large"), { status: 413 });
  }
  try { new TextDecoder("utf-8", { fatal: true }).decode(bytes); } catch { invalid("invalid_utf8"); }
  return { path: file.path, bytes, content_base64: normalized };
}

function validateCommit(input, policy, bodyBytes) {
  if (bodyBytes > policy.maxBodyBytes) throw Object.assign(new Error("request_too_large"), { status: 413 });
  if (!exactKeys(input, COMMIT_KEYS, COMMIT_KEYS.slice(0, 7))) invalid("invalid_request");
  if (!input || !OP_RE.test(input.operation_id || "")) throw Object.assign(new Error("invalid_operation_id"), { status: 400 });
  if (input.repository !== policy.repository) throw Object.assign(new Error("forbidden_repository"), { status: 403 });
  if (input.branch !== policy.candidateBranch) {
    throw Object.assign(new Error("forbidden_branch"), { status: 403 });
  }
  if (!SHA_RE.test(input.expected_head_sha || "")) throw Object.assign(new Error("invalid_expected_head_sha"), { status: 400 });
  if (!['create', 'update'].includes(input.mode)) throw Object.assign(new Error("invalid_mode"), { status: 400 });
  if (!Array.isArray(input.files) || input.files.length < 1 || input.files.length > policy.maxFiles) {
    throw Object.assign(new Error("invalid_file_count"), { status: 400 });
  }
  const files = input.files.map((file) => decodeFile(file, policy));
  if (new Set(files.map((f) => f.path)).size !== files.length) throw Object.assign(new Error("duplicate_path"), { status: 409 });
  if (files.reduce((n, f) => n + f.bytes.length, 0) > policy.maxTotalBytes) {
    throw Object.assign(new Error("content_too_large"), { status: 413 });
  }
  const message = typeof input.message === "string" ? input.message : "";
  if (!ONE_LINE_RE.test(message) || codePointLength(message) > 160) throw Object.assign(new Error("invalid_message"), { status: 400 });
  if (input.test_fail_after_commit !== undefined && typeof input.test_fail_after_commit !== "boolean") invalid("invalid_request");
  if (input.test_fail_after_commit && !policy.allowFaultInjection) {
    throw Object.assign(new Error("fault_injection_disabled"), { status: 403 });
  }
  return { ...input, message, files: files.map(({ path, content_base64 }) => ({ path, content_base64 })) };
}

function validatePull(input, policy, bodyBytes) {
  if (bodyBytes > policy.maxBodyBytes) throw Object.assign(new Error("request_too_large"), { status: 413 });
  if (!exactKeys(input, PULL_KEYS, ["operation_id", "repository", "branch", "base", "expected_head_sha", "mode", "title", "body"])) invalid("invalid_request");
  if (!input || !OP_RE.test(input.operation_id || "")) throw Object.assign(new Error("invalid_operation_id"), { status: 400 });
  if (input.repository !== policy.repository) throw Object.assign(new Error("forbidden_repository"), { status: 403 });
  if (input.base !== policy.baseBranch) throw Object.assign(new Error("forbidden_base"), { status: 403 });
  if (input.branch !== policy.candidateBranch) {
    throw Object.assign(new Error("forbidden_branch"), { status: 403 });
  }
  if (!SHA_RE.test(input.expected_head_sha || "")) throw Object.assign(new Error("invalid_expected_head_sha"), { status: 400 });
  if (!['create', 'update'].includes(input.mode)) throw Object.assign(new Error("invalid_mode"), { status: 400 });
  if (input.mode === "update" && (!Number.isInteger(input.pull_number) || input.pull_number < 1)) {
    throw Object.assign(new Error("invalid_pull_number"), { status: 400 });
  }
  if (typeof input.title !== "string" || !ONE_LINE_RE.test(input.title) || codePointLength(input.title) > 120) {
    throw Object.assign(new Error("invalid_title"), { status: 400 });
  }
  if (typeof input.body !== "string" || codePointLength(input.body) > 4000) {
    throw Object.assign(new Error("invalid_body"), { status: 400 });
  }
  if (input.draft !== undefined && typeof input.draft !== "boolean") invalid("invalid_request");
  if (input.test_fail_after_mutation !== undefined && typeof input.test_fail_after_mutation !== "boolean") invalid("invalid_request");
  if (input.test_fail_after_mutation && !policy.allowFaultInjection) {
    throw Object.assign(new Error("fault_injection_disabled"), { status: 403 });
  }
  return { ...input, draft: input.mode === "create" ? input.draft !== false : undefined };
}

function validatePullTarget(pull, input, repo) {
  if (
    pull.head?.ref !== input.branch ||
    pull.base?.ref !== input.base ||
    pull.head?.repo?.full_name !== repo ||
    pull.base?.repo?.full_name !== repo
  ) invalid("forbidden_pull", 403);
  if (pull.head.sha !== input.expected_head_sha) {
    throw Object.assign(new Error("stale_head"), { status: 409, actual_head_sha: pull.head.sha });
  }
}

function ghPath(repo, suffix) {
  return `https://api.github.com/repos/${repo}${suffix}`;
}

async function gh(call, token, method, url, body, signal) {
  const response = await call(url, {
    method,
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "x-github-api-version": "2022-11-28",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error("github_rejected"), { status: response.status, github: value });
  return value;
}

async function refSha(call, token, repo, branch, signal) {
  const value = await gh(call, token, "GET", ghPath(repo, `/git/ref/heads/${encodeURIComponent(branch)}`), undefined, signal);
  return value.object.sha;
}

async function mutateGh(context, method, url, body) {
  await context.guard();
  return gh(context.call, context.token, method, url, body, context.signal);
}

async function executeCommit(context) {
  const { input, record, store, call, token, policy, signal } = context;
  const repo = policy.repository;
  let current;
  try {
    current = await refSha(call, token, repo, input.branch, signal);
  } catch (error) {
    if (!(input.mode === "create" && error.status === 404)) throw error;
    current = null;
  }

  if (record.commit_sha && current === record.commit_sha) {
    return store.complete(input.operation_id, { commit_sha: record.commit_sha, branch: input.branch, recovered: true }, record.attempt);
  }
  if (input.mode === "create" ? current !== null : current !== input.expected_head_sha) {
    throw Object.assign(new Error("stale_head"), { status: 409, actual_head_sha: current });
  }
  if (input.mode === "create") {
    const baseHead = await refSha(call, token, repo, policy.baseBranch, signal);
    if (baseHead !== input.expected_head_sha) {
      throw Object.assign(new Error("stale_head"), { status: 409, actual_head_sha: baseHead });
    }
  }

  if (record.commit_sha) {
    if (input.mode === "create") {
      await mutateGh(context, "POST", ghPath(repo, "/git/refs"), { ref: `refs/heads/${input.branch}`, sha: record.commit_sha });
    } else {
      await mutateGh(context, "PATCH", ghPath(repo, `/git/refs/heads/${encodeURIComponent(input.branch)}`), { sha: record.commit_sha, force: false });
    }
    return store.complete(input.operation_id, { commit_sha: record.commit_sha, branch: input.branch, recovered: true }, record.attempt);
  }

  const baseCommit = await gh(call, token, "GET", ghPath(repo, `/git/commits/${input.expected_head_sha}`), undefined, signal);
  const entries = [];
  for (const file of input.files) {
    const blob = await mutateGh(context, "POST", ghPath(repo, "/git/blobs"), { content: file.content_base64, encoding: "base64" });
    entries.push({ path: file.path, mode: "100644", type: "blob", sha: blob.sha });
  }
  const tree = await mutateGh(context, "POST", ghPath(repo, "/git/trees"), { base_tree: baseCommit.tree.sha, tree: entries });
  const commit = await mutateGh(context, "POST", ghPath(repo, "/git/commits"), {
    message: input.message,
    tree: tree.sha,
    parents: [input.expected_head_sha],
  });
  await store.saveCommit(input.operation_id, commit.sha, record.attempt);
  if (input.test_fail_after_commit) throw Object.assign(new Error("injected_after_commit"), { status: 503 });

  if (input.mode === "create") {
    await mutateGh(context, "POST", ghPath(repo, "/git/refs"), { ref: `refs/heads/${input.branch}`, sha: commit.sha });
  } else {
    const beforeUpdate = await refSha(call, token, repo, input.branch, signal);
    if (beforeUpdate !== input.expected_head_sha) throw Object.assign(new Error("stale_head"), { status: 409, actual_head_sha: beforeUpdate });
    try {
      await mutateGh(context, "PATCH", ghPath(repo, `/git/refs/heads/${encodeURIComponent(input.branch)}`), { sha: commit.sha, force: false });
    } catch (error) {
      if (error.status === 422) throw Object.assign(new Error("stale_head"), { status: 409 });
      throw error;
    }
  }
  return store.complete(input.operation_id, { commit_sha: commit.sha, tree_sha: tree.sha, branch: input.branch, recovered: false }, record.attempt);
}

async function executePull(context) {
  const { input, record, store, call, token, policy, signal } = context;
  const repo = policy.repository;
  if (input.mode === "create") {
    const head = await refSha(call, token, repo, input.branch, signal);
    if (head !== input.expected_head_sha) throw Object.assign(new Error("stale_head"), { status: 409, actual_head_sha: head });
    const owner = repo.split("/")[0];
    const query = new URLSearchParams({ state: "open", head: `${owner}:${input.branch}`, base: input.base });
    const existing = await gh(call, token, "GET", ghPath(repo, `/pulls?${query}`), undefined, signal);
    if (Array.isArray(existing) && existing.length) {
      validatePullTarget(existing[0], input, repo);
      return store.complete(input.operation_id, {
        pull_number: existing[0].number,
        url: existing[0].html_url,
        head_sha: existing[0].head.sha,
        recovered: true,
      }, record.attempt);
    }
    const created = await mutateGh(context, "POST", ghPath(repo, "/pulls"), {
      title: input.title,
      body: input.body,
      head: input.branch,
      base: input.base,
      draft: input.draft,
      maintainer_can_modify: false,
    });
    if (input.test_fail_after_mutation) throw Object.assign(new Error("injected_after_mutation"), { status: 503 });
    return store.complete(input.operation_id, {
      pull_number: created.number,
      url: created.html_url,
      head_sha: created.head.sha,
      recovered: false,
    }, record.attempt);
  }

  const current = await gh(call, token, "GET", ghPath(repo, `/pulls/${input.pull_number}`), undefined, signal);
  validatePullTarget(current, input, repo);
  const updated = await mutateGh(context, "PATCH", ghPath(repo, `/pulls/${input.pull_number}`), {
    title: input.title,
    body: input.body,
  });
  return store.complete(input.operation_id, {
    pull_number: updated.number,
    url: updated.html_url,
    head_sha: updated.head.sha,
    recovered: false,
  }, record.attempt);
}

async function requestValue(request, limit) {
  const declared = request.headers.get("content-length");
  if (/^\d+$/.test(declared || "") && Number(declared) > limit) invalid("request_too_large", 413);
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.length > limit) invalid("request_too_large", 413);
  let raw;
  try { raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
  catch { invalid("invalid_json"); }
  try { return { input: JSON.parse(raw), bodyBytes: bytes.length }; }
  catch { invalid("invalid_json"); }
}

export function createGithubAction({ store, githubFetch = fetch, token, policy = DEFAULT_POLICY, now = Date.now }) {
  if (!token) throw new Error("missing_server_github_token");
  return async function handle(request) {
    const pathname = new URL(request.url).pathname;
    const routes = ["/v1/github/commit", "/v1/github/pull"];
    if (request.method !== "POST" || !routes.includes(pathname)) return json(404, { error: "not_found" });
    try {
      const deadline = now() + policy.maxRequestMs;
      const signal = AbortSignal.any([request.signal, AbortSignal.timeout(policy.maxRequestMs)]);
      const value = await requestValue(request, policy.maxBodyBytes);
      const input = pathname.endsWith("/commit")
        ? validateCommit(value.input, policy, value.bodyBytes)
        : validatePull(value.input, policy, value.bodyBytes);
      const payloadHash = await digest(input);
      const started = await store.begin(input.operation_id, payloadHash, policy.operationLeaseSeconds);
      if (started.conflict) return json(409, { error: "idempotency_conflict" });
      if (started.busy) return json(409, { error: "operation_in_progress" });
      if (started.record.status === "complete") return json(200, { ...started.record.result, created: false });
      try {
        const guard = async () => {
          if (signal.aborted || now() >= deadline) invalid("operation_deadline_exceeded", 503);
          await store.assertActive(input.operation_id, started.record.attempt);
          if (signal.aborted || now() >= deadline) invalid("operation_deadline_exceeded", 503);
        };
        const context = { input, record: started.record, store, call: githubFetch, token, policy, signal, guard };
        const result = pathname.endsWith("/commit")
          ? await executeCommit(context)
          : await executePull(context);
        return json(200, { ...result, created: started.created });
      } catch (error) {
        await store.release(input.operation_id, started.record.attempt);
        throw error;
      }
    } catch (error) {
      const status = error.status || 502;
      const body = { error: error.message || "internal_error" };
      if (error.actual_head_sha !== undefined) body.actual_head_sha = error.actual_head_sha;
      return json(status, body);
    }
  };
}

export class MemoryOperationStore {
  constructor() { this.records = new Map(); }
  async begin(id, hash) {
    const existing = this.records.get(id);
    if (existing) {
      if (existing.payload_hash !== hash) return { conflict: true };
      if (existing.status === "complete") return { created: false, record: existing };
      if (existing.leased) return { busy: true };
      existing.leased = true;
      existing.attempt += 1;
      return { created: false, record: existing };
    }
    const record = { operation_id: id, payload_hash: hash, status: "running", commit_sha: null, result: null, leased: true, attempt: 1 };
    this.records.set(id, record);
    return { created: true, record };
  }
  async assertActive(id, attempt) {
    const record = this.records.get(id);
    if (!record || record.status !== "running" || record.attempt !== attempt || !record.leased) invalid("stale_operation_attempt", 409);
  }
  async saveCommit(id, sha, attempt) {
    const record = this.records.get(id);
    if (!record || record.status !== "running" || record.attempt !== attempt) throw Object.assign(new Error("stale_operation_attempt"), { status: 409 });
    record.commit_sha = sha;
  }
  async complete(id, result, attempt) {
    const record = this.records.get(id);
    if (!record || record.status !== "running" || record.attempt !== attempt) throw Object.assign(new Error("stale_operation_attempt"), { status: 409 });
    record.status = "complete"; record.result = result; return result;
  }
  async release(id, attempt) {
    const record = this.records.get(id);
    if (record && record.status === "running" && record.attempt === attempt) record.leased = false;
  }
}

export class D1OperationStore {
  constructor(db) { this.db = db; }
  async begin(id, hash, leaseSeconds = 60) {
    const inserted = await this.db.prepare(
      "INSERT OR IGNORE INTO github_operations(operation_id,payload_hash,status,lease_until,attempt) VALUES(?,?,'running',unixepoch()+?,1)",
    ).bind(id, hash, leaseSeconds).run();
    let row = await this.db.prepare(
      "SELECT operation_id,payload_hash,status,commit_sha,result_json,lease_until,attempt FROM github_operations WHERE operation_id=?",
    ).bind(id).first();
    if (!row) throw new Error("operation_store_unavailable");
    if (row.payload_hash !== hash) return { conflict: true };
    if (row.status === "complete") {
      return { created: false, record: { ...row, result: row.result_json ? JSON.parse(row.result_json) : null } };
    }
    const created = Number(inserted.meta?.changes || 0) === 1;
    if (!created) {
      const claimed = await this.db.prepare(
        "UPDATE github_operations SET lease_until=unixepoch()+?,attempt=attempt+1,updated_at=CURRENT_TIMESTAMP WHERE operation_id=? AND status='running' AND lease_until<=unixepoch()",
      ).bind(leaseSeconds, id).run();
      if (Number(claimed.meta?.changes || 0) !== 1) return { busy: true };
      row = await this.db.prepare(
        "SELECT operation_id,payload_hash,status,commit_sha,result_json,lease_until,attempt FROM github_operations WHERE operation_id=?",
      ).bind(id).first();
    }
    return {
      created,
      record: {
        ...row,
        result: row.result_json ? JSON.parse(row.result_json) : null,
      },
    };
  }
  async assertActive(id, attempt) {
    const row = await this.db.prepare("SELECT operation_id FROM github_operations WHERE operation_id=? AND status='running' AND attempt=? AND lease_until>unixepoch()").bind(id, attempt).first();
    if (!row) invalid("stale_operation_attempt", 409);
  }
  async saveCommit(id, sha, attempt) {
    const result = await this.db.prepare("UPDATE github_operations SET commit_sha=?,updated_at=CURRENT_TIMESTAMP WHERE operation_id=? AND status='running' AND attempt=?").bind(sha, id, attempt).run();
    if (Number(result.meta?.changes || 0) !== 1) throw Object.assign(new Error("stale_operation_attempt"), { status: 409 });
  }
  async complete(id, result, attempt) {
    const updated = await this.db.prepare("UPDATE github_operations SET status='complete',result_json=?,updated_at=CURRENT_TIMESTAMP WHERE operation_id=? AND status='running' AND attempt=?").bind(JSON.stringify(result), id, attempt).run();
    if (Number(updated.meta?.changes || 0) !== 1) throw Object.assign(new Error("stale_operation_attempt"), { status: 409 });
    return result;
  }
  async release(id, attempt) {
    await this.db.prepare("UPDATE github_operations SET lease_until=0,updated_at=CURRENT_TIMESTAMP WHERE operation_id=? AND status='running' AND attempt=?").bind(id, attempt).run();
  }
}

export function createHostedGithubHandler(env, options = {}) {
  return async (request) => {
    const authorization = request.headers.get("authorization") || "";
    if (!env.ACTION_KEY || authorization !== `Bearer ${env.ACTION_KEY}`) {
      return json(401, { error: "unauthorized" });
    }
    const action = createGithubAction({
      store: new D1OperationStore(env.DB),
      githubFetch: options.githubFetch || fetch,
      token: env.GITHUB_TOKEN,
      policy: options.policy || DEFAULT_POLICY,
    });
    return action(request);
  };
}
