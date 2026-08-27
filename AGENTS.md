# Working on Switchstand

Read `README.md` and the documents under `docs/` before changing behavior. Start with
`docs/product.md` for the operator boundary and `docs/architecture.md` for the state
invariants.

Keep this repository at prototype altitude: one Work, exactly two durable logical roles,
flat-file state, a vanilla browser surface, and the explicit Codex app-server adapter. Do
not add general orchestration, databases, queues, framework shells, remote lifecycle
automation, or implied authority without a recorded product decision.

For behavioral changes:

1. Preserve exact attempt and generation fencing, ordered messages, conservative restart
   recovery, and truthful `unknown`/`stale` states.
2. Add or update focused `unittest` coverage.
3. Run the commands in `docs/development.md` and report the exact evidence. Never report a
   live adapter checkpoint unless it actually ran against a real socket.
4. Update the relevant product, architecture, development, or decision document when a
   behavior or constraint changes.

Prefer the Python standard library and direct HTML/CSS/JavaScript. Keep errors observable;
do not turn missing acknowledgements into success. Review changes for accidental scope
expansion, unsafe replay, state-schema drift, or stale-result acceptance.

Run `./scripts/quality` before handoff. Do not weaken, bypass, or broaden the quality gate inside
an implementation task. Changes to the gate, its configuration, locks, or CI workflow require
explicit human review. If a requested change needs another surface or larger scope, stop and
return it to the coordinator instead of expanding the task.
