# GPT Action GitHub feasibility slice

This isolated prototype implements the only GitHub mutation route admitted for the first experiment:

- implementation home: `marcogallotta/switchstand`;
- fixed mutation target: `marcogallotta/gpt-actions-github-fixture`;
- one safe Contents-API initialization of `main` while the repository has no refs;
- exact candidate branch: `gpt-actions-controlled-github-feasibility`;
- fixed path prefix: `experiments/gpt-actions-github/`;
- blobs → tree → commit → non-force ref creation/update;
- operation-ID idempotency and payload-conflict rejection;
- expected-head fencing;
- bounded request, file, total-content, and file-count limits;
- UTF-8 text and exact request-shape validation;
- retry recovery after orphaned Git objects without moving a ref early.

The fixed limits are five files, 4 KiB per file, 16 KiB aggregate decoded content, and
32 KiB per HTTP request. Initialization writes one fixed README and cannot update an
existing `main`.

`github-action.mjs` is Action-side code. The GitHub token is injected only as a hosted secret and is never accepted from the Action request.

Run locally with Node 20+:

```sh
node --test github-action.test.mjs
```

The module includes the durable D1 operation store. Deployment must wire its hosted
handler and migration into the existing coordinator and inject a GitHub credential
scoped only to the fixture repository.
