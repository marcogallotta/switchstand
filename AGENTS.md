# Working on Switchstand

Read `README.md` and the documents under `docs/` before changing behavior. Start with
`docs/product.md` for the operator boundary and `docs/architecture.md` for the state
invariants.

Build Switchstand as bounded supervised local-agent infrastructure, not a throwaway
prototype. The default legacy mode remains one Work with exactly two durable logical roles and
exists as a reliability spike. Explicitly selected native mode may replace that synthetic
surface with truthful observation, exact current-target input, and exact-turn emergency Stop.
Do not add general orchestration, databases, queues, framework shells, remote lifecycle
automation, or implied authority without a recorded product decision.

Project plans, tasks, research, dispatch, and current work live in the Switchstand Asana
project. Do not create or use GitHub Issues unless Marco explicitly decides otherwise. GitHub
owns branches, commits, pull requests, CI, and exact-head review evidence. Start repository work
from its authoritative Asana task and keep the master continuation current when state changes.

For behavioral changes:

1. Preserve exact attempt and generation fencing, ordered messages, conservative restart
   recovery, and truthful `unknown`/`stale` states.
2. Add or update focused `unittest` coverage.
3. Run the commands in `docs/development.md` and report the exact evidence. Never report a
   live adapter checkpoint unless it actually ran against a real socket.
4. Update the relevant product, architecture, development, or decision document when a
   behavior or constraint changes.
5. Keep the diff focused and human-readable. Pull requests explain why, behavior, meaningful
   files/components, exclusions, useful verification, and remaining uncertainty. Avoid
   unrelated formatting and whitespace churn.

In native mode, preserve protocol terms and uncertainty. Poll with `thread/read`, fully
paginated `thread/list(useStateDbOnly=true)`, and the bounded content-free latest-turn surface
defined by the architecture. Do not turn poll differences into complete event history or infer
done, progress, failure, staleness, or intent. Current authority is limited to exact
current-target start-or-steer input and exact-turn emergency Stop. It has no redirect, replace,
resume, subscription, general lifecycle, retry, fallback, or retarget authority.

Prefer the Python standard library and direct HTML/CSS/JavaScript. Keep errors observable;
do not turn missing acknowledgements into success. Review changes for accidental scope
expansion, unsafe replay, state-schema drift, or stale-result acceptance.

Run `./scripts/quality` before handoff. Do not weaken, bypass, or broaden the quality gate inside
an implementation task. Changes to the gate, its configuration, locks, or CI workflow require
explicit human review. If a requested change needs another surface or larger scope, stop and
return it to the coordinator instead of expanding the task.

Tests must remain enabled and single-run: no skip/focus/todo/fixme or retry/rerun escape hatches.
Run the local Unix transport and real-Chromium journeys when their boundary is affected; a fake
DOM test is seam evidence only.

Run Switchstand entirely as the invoking user. Do not introduce `sudo`, root-owned files,
system-wide installs, privileged ports, root-managed services, ambient browser/session
takeover, or manual control of Marco's open browser.
