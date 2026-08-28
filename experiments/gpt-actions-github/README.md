# GPT Action GitHub feasibility slice

This isolated prototype implements the only GitHub mutation route admitted for the first experiment:

- fixed repository: `marcogallotta/switchstand`;
- fixed branch prefix: `agent/gpt-actions-github-`;
- fixed path prefix: `experiments/gpt-actions-github/`;
- blobs → tree → commit → non-force ref creation/update;
- operation-ID idempotency and payload-conflict rejection;
- expected-head fencing;
- bounded request, file, total-content, and file-count limits;
- retry recovery after orphaned Git objects without moving a ref early.

`github-action.mjs` is Action-side code. The GitHub token is injected only as a hosted secret and is never accepted from the Action request.

Run locally with Node 20+:

```sh
node --test github-action.test.mjs
```

The hosted integration still needs a durable implementation of the operation-store interface and a server-held GitHub credential scoped to the test repository.
