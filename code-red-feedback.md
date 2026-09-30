# Code Red feedback tracker

Created: 2026-09-30T19:38:35+02:00
Owner: `/root/code_red_lifecycle_rework`
Rule: append-only operational tracker; record later status changes as new rows rather than erasing prior evidence.

| Feedback requirement | Observed/current gap | Target owner/file | Status | Clearing evidence/commit | Follow-up/removal trigger |
| --- | --- | --- | --- | --- | --- |
| Credible live-user failure self-triggers Code Red; explicit declaration also triggers | Current bootstrap covers automatic entry but needs lifecycle reconciliation | `AGENTS.md`, `docs/operations-live-incident.md` | IN_PROGRESS | Pending exact candidate | Review proves both entry routes are explicit |
| Immediate acknowledgement and concise frequent interactive updates | Current runbook has ACK/cadence but incomplete status content | `docs/operations-live-incident.md` | IN_PROGRESS | Pending exact candidate | Review validates first-minute and update contract |
| `STOP` means pause, listen, and re-ground; `CANCEL`/`STOP WORK` terminates work | Current docs incorrectly make `STOP` cancel workers and tools | `AGENTS.md`, `docs/operations-live-incident.md` | IN_PROGRESS | Pending exact candidate | Static test and exact-head review clear distinction |
| One canonical private incident ledger and atomic current status | Current evidence directory has no stable incident ID, append-only timeline, or atomic status | `docs/operations-live-incident.md`, incident record template | IN_PROGRESS | Pending exact candidate | Runbook defines private directory, timeline, status, and current pointer |
| Status includes impact, scope, difficulty/prognosis, Human/agent action, service decision, action, checkpoint | Current four-line format omits several required decisions | Incident status template | IN_PROGRESS | Pending exact candidate | Template contains every required field |
| Temporary/permanent coordination changes cover Project Settings, `START HERE`, agent guidance, canary, and HOLD | Current runbook has no coordination-change lane | Incident status/closeout template | IN_PROGRESS | Pending exact candidate | Template records authority, state, and disposition |
| Temporary setting records owner, effective time, removal trigger, state, then reverts or becomes permanent | Current runbook has no temporary-setting lifecycle | Incident status/closeout template | IN_PROGRESS | Pending exact candidate | Closeout test requires removal or permanent disposition |
| Rollback is temporary; exact removed candidate and bundled good work reach roll-forward/redeploy/explicit retirement | Current runbook names a rollback boundary but can silently abandon rolled-back work | Incident runbook and closeout template; deployment docs | IN_PROGRESS | Pending exact candidate | Static test and review prove owned disposition obligation |
| Forward-only state is never restored from an old snapshot | Edge deployment doc covers OAuth only; general incident lifecycle rule absent | Incident runbook and deployment docs | IN_PROGRESS | Pending exact candidate | Closeout explicitly preserves forward-only state |
| Mandatory post-Code-Red cleanup includes recovery proof, temp cleanup, RCA, tests, monitoring/Wakeful, owned follow-ups, residual Human Input/NOT_RUN | Current follow-up paragraph is incomplete and non-checkable | Incident closeout template | IN_PROGRESS | Pending exact candidate | All closeout fields present and tested |
| Incident procedure is discoverable even when local `main` is stale | Remote bootstrap links it, but stale checkout can miss newer procedure semantics | `AGENTS.md`, handoff/bootstrap guidance | IN_PROGRESS | Pending exact candidate | Review establishes top-level minimum and stale-main warning |
| Design uses and dispositions the repository research-source task list | Current Code Red docs do not record researched basis | `docs/research-sources.md`, incident docs/review handoff | IN_PROGRESS | Pending exact candidate | Source dispositions recorded with exact candidate |
| Correlated sibling failures test shared ingress before product-local resets | Dish and Switchstand recovered after shared Funnel repair; prior runbook did not isolate the common layer | `AGENTS.md`, incident runbook/status | IN_PROGRESS | Pending exact candidate | Dependency map and per-path post-repair proof are explicit |
| A user recovery report advances immediately into verification and backlog, never a stall | Prior response declared future intent and yielded instead of doing the remaining work | Bootstrap, incident runbook/closeout | IN_PROGRESS | Pending exact candidate | Recovery checkpoint rule and no-new-prompt closeout rule are explicit |

## 2026-09-30 implementation disposition

All rows above reached `IMPLEMENTED` in the exact candidate containing this disposition. The
clearing evidence is:

- `AGENTS.md` carries the minimal stale-checkout-safe trigger, control, shared-ingress, rollback,
  recovery-checkpoint, and closeout contract.
- `docs/operations-live-incident.md` carries the full lifecycle and reasoned research-source
  dispositions.
- `docs/operations-incident-record.md` provides the one-record status, timeline, rollback, and
  closeout templates.
- `tests/test_documentation.py` rejects regression of the trigger, `STOP`/`CANCEL` distinction,
  shared-ingress, required status fields, rollback disposition, and no-new-prompt continuation.
- `sh scripts/check tests/test_documentation.py` passed Ruff, Pyright, and all 5 tests in the
  writer-local locked environment.

Remaining delivery truth: fresh remote fetch and exact-current-main composition are blocked by the
host SSH configuration ownership failure recorded in ignored `friction.md`; cached `origin/main`
is two commits ahead and its changed paths do not overlap this candidate.
