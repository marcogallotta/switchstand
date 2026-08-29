const ZERO_SHA = '0000000000000000000000000000000000000000';

function decodeBase64(value) {
  const binary = atob(value.replace(/\s/g, ''));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function encodeHex(bytes) {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function sha256(bytes) {
  return encodeHex(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)));
}

function compareUtf8(left, right) {
  const encoder = new TextEncoder();
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

async function responseJson(response) {
  const body = await response.text();
  if (!response.ok) {
    throw Object.assign(new Error('provider_error'), {
      status: response.status,
      permanent: response.status >= 400 && response.status < 500 && response.status !== 409,
    });
  }
  try {
    return JSON.parse(body);
  } catch {
    throw Object.assign(new Error('provider_invalid_response'), { permanent: true });
  }
}

export class GitHubPublisher {
  constructor({ fetch, token, apiUrl = 'https://api.github.com' }) {
    this.fetch = fetch;
    this.token = token;
    this.apiUrl = apiUrl;
  }

  async github(path, init = {}) {
    const headers = new Headers(init.headers);
    headers.set('authorization', `Bearer ${this.token}`);
    headers.set('accept', 'application/vnd.github+json');
    headers.set('content-type', 'application/json');
    headers.set('user-agent', 'switchstand-coordinator/0.1');
    return responseJson(await this.fetch(`${this.apiUrl}${path}`, { ...init, headers }));
  }

  async ref(repository, name) {
    try {
      const refName = encodeURIComponent(name.replace(/^refs\//, ''));
      const result = await this.github(`/repos/${repository}/git/ref/${refName}`);
      return result.object?.sha || null;
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  }

  async readRefs(plan) {
    return {
      marker: await this.ref(plan.repository_full_name, plan.marker_ref),
      target: await this.ref(plan.repository_full_name, `refs/heads/${plan.candidate_branch}`),
    };
  }

  async objects(plan) {
    const manifestBytes = decodeBase64(plan.canonical_manifest_base64);
    if ((await sha256(manifestBytes)) !== plan.manifest_sha) {
      throw Object.assign(new Error('manifest_digest_mismatch'), { permanent: true });
    }
    const manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(manifestBytes));
    const baseCommit = await this.github(
      `/repos/${plan.repository_full_name}/git/commits/${plan.expected_head}`,
    );
    if (!/^[0-9a-f]{40}$/.test(baseCommit.tree?.sha || '')) {
      throw Object.assign(new Error('provider_invalid_response'), { permanent: true });
    }
    const entries = [];
    for (const file of manifest.files) {
      const blob = await this.github(`/repos/${plan.repository_full_name}/git/blobs`, {
        method: 'POST',
        body: JSON.stringify({ content: file.content_base64, encoding: 'base64' }),
      });
      entries.push({ path: file.path, mode: '100644', type: 'blob', sha: blob.sha });
    }
    for (const deletion of manifest.deletions) {
      entries.push({ path: deletion.path, mode: '100644', type: 'blob', sha: null });
    }
    entries.sort((left, right) => compareUtf8(left.path, right.path));
    const tree = await this.github(`/repos/${plan.repository_full_name}/git/trees`, {
      method: 'POST',
      body: JSON.stringify({ base_tree: baseCommit.tree.sha, tree: entries }),
    });
    const commit = await this.github(`/repos/${plan.repository_full_name}/git/commits`, {
      method: 'POST',
      body: JSON.stringify({
        message: plan.message,
        tree: tree.sha,
        parents: [plan.expected_head],
        author: plan.author,
        committer: plan.author,
      }),
    });
    return { treeSha: tree.sha, commitSha: commit.sha };
  }

  async repositoryId(repository) {
    return (await this.github(`/repos/${repository}`)).node_id;
  }

  async updateRefs(plan, updates) {
    const query = `mutation UpdateRefs($input:UpdateRefsInput!){updateRefs(input:$input){clientMutationId}}`;
    const repositoryId = await this.repositoryId(plan.repository_full_name);
    return this.github('/graphql', {
      method: 'POST',
      body: JSON.stringify({
        query,
        variables: {
          input: {
            repositoryId,
            refUpdates: updates,
            clientMutationId: plan.plan_sha,
          },
        },
      }),
    });
  }

  publish(plan) {
    return this.updateRefs(plan, [
      {
        name: `refs/heads/${plan.candidate_branch}`,
        beforeOid: plan.expected_head,
        afterOid: plan.desired_commit_sha,
        force: false,
      },
      { name: plan.marker_ref, beforeOid: ZERO_SHA, afterOid: plan.desired_commit_sha, force: false },
    ]);
  }

  close(plan, desired) {
    return this.updateRefs(plan, [
      { name: plan.marker_ref, beforeOid: ZERO_SHA, afterOid: desired, force: false },
    ]);
  }
}

export async function runPublicationAttempt({ coordinator, publisher, claim }) {
  let plan = claim;
  if (!plan.desired_commit_sha) {
    const objects = await publisher.objects(plan);
    plan = await coordinator.recordObjects(claim, objects);
  }
  let observed;
  try {
    observed = await publisher.readRefs(plan);
  } catch {
    return { outcome: 'reconciling', plan };
  }
  let decision = await coordinator.observe(plan, observed, false);
  if (decision.directive === 'terminal') return { outcome: decision.state, plan: decision };
  try {
    if (decision.directive === 'publish') await publisher.publish(decision);
    else if (decision.directive === 'close_desired') await publisher.close(decision, decision.desired_commit_sha);
    else if (decision.directive === 'close_expected') await publisher.close(decision, decision.expected_head);
  } catch (error) {
    if (error.permanent) {
      decision = await coordinator.observe(decision, observed, true);
      if (decision.directive === 'close_expected') {
        try {
          await publisher.close(decision, decision.expected_head);
        } catch {
          return { outcome: 'reconciling', plan: decision };
        }
      }
    } else {
      return { outcome: 'reconciling', plan: decision };
    }
  }
  try {
    const readback = await publisher.readRefs(decision);
    const terminal = await coordinator.observe(decision, readback, false);
    return { outcome: terminal.state, plan: terminal };
  } catch {
    return { outcome: 'reconciling', plan: decision };
  }
}
