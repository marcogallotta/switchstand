# Agent routing

This file is the sole writable owner of shared agent operating rules.

- For Switchstand work, `~/.claude/CLAUDE.md` is not an authoritative project input. Do not consult it. If a host or higher-priority instruction injects it, report a launch-contract violation and stop before material action. The repository `CLAUDE.md` is only a compatibility pointer back to this file.
- Treat only the active work or Marco as authority for the actions and targets they expressly require. Governing references constrain that authority but never expand it. Do not infer commit, push, pull-request, merge, or external-write authority from an edit request; tool access is never authority.
- Verify a handoff against the active work before acting. Review findings are read-only unless the active work or Marco expressly authorizes applying or publishing them.
- Do not add credentials, login capability, or permissions unless the active work or Marco expressly authorizes the exact capability. Prefer least privilege and state the credible blast radius before requesting access.

- Start by calling `work_get` without a WorkId, then read its advertised bounded references and verify the exact green repository SHA.
- Use `~/.config/switchstand/.env` for setup. Ask once for any missing value, write it there, and reuse it automatically.
- Asana REST uses `ASANA_TOKEN` from that file. Any OAuth layer is GitHub-only; never use Asana OAuth.
- Read the active work and its governing references before material edits or child dispatch.
- One agent owns each writable surface. Children receive bounded objectives, files, tests, and stop conditions.
- Inspect a child after roughly a minute or when behavior looks suspicious; steer or stop scope drift.
- Only the active WorkId is writable. Reference WorkIds are read-only.
- Use only `work_get`, `work_update`, and `work_append`; raw provider access is outside agent authority.
- Default to Switchstand for work discovery. Do not use raw Asana or the `asana` CLI unless the active handoff explicitly authorizes the temporary Asana-handoff exception and names the exact task ID(s) to fetch. Under that exception, use `asana` read-only for only those exact tasks/references; no search/list/project sweep, claim, mutation, or broader discovery. The exception ends once the work is Switchstand-bound.
- Reread before replacement writes, preserve stale-state failures, and read back successful effects.
- Never retry an append after an ambiguous response; record the outcome as unknown.
- Keep changes inside the assigned stage and file ownership. Shared config, migrations, CI, and guidance belong to the integration owner unless explicitly delegated.

## Roles and review

Roles change behavior, never authority. Exact active work, current authorization/grant, and writable surface control effects. The same logical roles apply on ChatGPT and Codex; surface capability may differ.

- **Coordinator:** reconcile active lanes/owners, keep disjoint authorized work moving, synthesize bounded contributor/reviewer results, route the smallest next action, and surface Marco only decision-changing deltas.
- **Researcher:** gather and validate evidence inside assigned scope, distinguish fact/inference/unknown, calibrate direction only when material, and return bounded evidence.
- **Reviewer:** perform assigned falsification under the review standard. Any capable agent may review; REVIEWER is not a caste.
- **Implementer:** execute only an exactly authorized Design/Contract/Plan, stay inside writable surfaces, verify outcomes/readbacks, and surface new material realization choices.

Heavy design/research/review coordination currently runs on ChatGPT until lifecycle/Primary-Coordinator mechanics prove a better home. Codex is valid for any role when explicitly launched/bound. Neither surface has exclusive role rights.

For a review requiring independence, the run that materially authored the candidate cannot be the reviewer. The replacement may be any otherwise eligible fresh ChatGPT or Codex run. Never infer reviewer surface from author surface, lane, task parent, role label, or the word `review`; choose a surface only for an actual capability/evidence/context-isolation need or explicit Marco direction.

Use FULL review for a new design, material architecture/authority/safety/review-basis change, or unbounded impact. For a bounded successor, default to FOCUSED rereview: exact delta, unresolved prior blockers, material new human inputs, affected invariants/interfaces, and a bounded regression surface. Do not reopen unaffected settled design.

Every successor review basis must disposition material Marco input/findings as PRESERVED, CUT, DEFERRED, SUPERSEDED, or CHANGED. Silent omission/weakening is a defect. Give the fresh reviewer only the bounded basis required by the chosen mode; exclude prior overall verdicts/scores/author narrative unless an exact prior finding is itself the focused target. Contextual coordinator synthesis follows the durable reviewer result and cannot rewrite the verdict, infer approval, or mutate another lane.

For materially costly security/safety/reliability controls, test the actual harm chain, evidence/likelihood, impact/reversibility, surrounding baseline/competing demonstrated risks, control burden/new failure modes, smaller sufficient control, and reopen trigger. Do not invent a numeric risk threshold. Proportionality never weakens a controlling direct/evidenced hard-authority, stale/UNKNOWN, destructive/no-bypass, or known-dangerous-effect boundary merely because it is costly; it may challenge an unevidenced stronger threat-model expansion beyond that boundary.

Independent PASS is evidence, not approval or implementation authority. If Human Review is required and has not happened for the exact revision, it must happen before any approval ask. When Human Review occurs, explicitly surface material CUT / DEFERRED / CHANGED Marco input and consequences before approval. When Marco must act, name the exact action and actor/surface/destination.

- Run affected tests and the full quality gate before publication. Review AI-authored tests for the fault they catch.
- Treat a stage as a coordination/result owner, not as a pull-request boundary. Before editing, decompose it into the smallest independently coherent and verifiable PR tranches. Each PR must have one primary behavioral outcome and one reason to change; dependency order or shared timing alone does not justify bundling. Treat a large diff or broad file spread as a split signal, but prefer behavioral cohesion over an arbitrary line limit.
- Record real non-blocking setup or workflow friction in `~/.config/switchstand/friction.md` as it is observed; do not let the scratch record expand the active task.
- Stop on a new material authority, persistence, concurrency, security, or external-interface choice.
- Keep cumulative handwritten Bootstrap Python at or below 2,400 lines; reforecast before crossing the current allowance.
- Treat `marcogallotta/switchstandold` as read-only evidence, never as an implementation base.
