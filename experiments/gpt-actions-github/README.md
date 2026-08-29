# GPT Action GitHub feasibility slice

This isolated prototype implements the only GitHub mutation route admitted for the first experiment:

- implementation home: `marcogallotta/switchstand`;
- fixed mutation target: `marcogallotta/gpt-actions-github-fixture`;
- exact candidate branch: `gpt-actions-controlled-github-feasibility`;
- fixed path prefix: `experiments/gpt-actions-github/`;
- blobs → tree → commit → non-force ref creation/update;
- operation-ID idempotency and payload-conflict rejection;
- expected-head fencing;
- a 40-second request deadline inside a 60-second operation lease, with an active-attempt check before every GitHub mutation;
- bounded request, file, total-content, and file-count limits;
- UTF-8 text and exact request-shape validation;
- retry recovery after orphaned Git objects without moving a ref early.

The fixed limits are 32 files, 64 KiB per file, 256 KiB aggregate decoded content, and
384 KiB per HTTP request. The file-count bound limits the sequential GitHub calls made within
the 40-second request deadline. The fixture's initial `main` commit is a separately recorded
provisioning step and is not part of the Action capability evidence.

`github-action.mjs` is Action-side code. The GitHub token is injected only as a hosted secret and is never accepted from the Action request.

Run locally with Node 20+:

```sh
node --test github-action.test.mjs
```

The module includes the durable D1 operation store. Deployment must wire its hosted
handler and migration into the existing coordinator and inject a GitHub credential
scoped only to the fixture repository.
