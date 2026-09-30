# Incident record and closeout

This is the canonical record shape for
[live-incident operations](operations-live-incident.md). Store each incident privately at
`~/.local/state/switchstand/incidents/<IncidentId>/`; do not commit live records or secrets.

## Current status (`status.md`)

Initialize this file atomically at mode `0600` before publishing the incident's `current` pointer.
Replace it thereafter from a mode-`0600` temporary file in the same directory.

```markdown
# <IncidentId> — <short title>

- State: DETECTED | MITIGATING | MONITORING | MITIGATED | CLOSED
- Operator: <one owner>
- Updated: <RFC3339 timestamp>
- Impact / understood scope: <users, paths, fail-open/fail-closed/unknown>
- Difficulty / prognosis: <trivial|contained|architectural|unknown; confidence; horizon>
- Human / agent action: <what Marco should do/tell agents, or none>
- Service posture: <RUN|GATE|SUSPEND> — <why and reversal condition>
- Current action / next checkpoint: <one action; result or time>
- Coordination changes: <none, or references to entries below>
- Dependency boundary: <client -> ingress -> origin -> auth -> provider; evidence at each layer>
- Evidence: <CURRENT/HISTORICAL/UNKNOWN and exact private paths/identities>
- Residual truth: <NOT_RUN/MISSING_CAPABILITY/UNKNOWN/Human Input, or none>

## Temporary coordination changes

| Surface | Exact temporary behavior | Authority | Owner | Effective time | Removal trigger | State |
| --- | --- | --- | --- | --- | --- | --- |
| Project Settings / START HERE / agent guidance / canary / HOLD | ... | ... | ... | ... | ... | PROPOSED/ACTIVE/SUPERSEDED/REMOVED |

## Rollback and forward-recovery obligations

| Removed candidate/runtime | Restored target | Forward-only state preserved | Suspect defect | Bundled good work | Owner | Disposition | Prerequisite / return trigger | State |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ... | ... | ... | ... | ... | ... | ROLL_FORWARD/REDEPLOY/RETIRE_EXPLICITLY | ... | OPEN/CLEARED |
```

## Append-only timeline (`timeline.jsonl`)

Append one JSON object per material event. Do not rewrite earlier records.

```json
{"at":"<RFC3339>","kind":"observation|decision|action|result|handoff|status","actor":"<identity>","classification":"CURRENT|HISTORICAL|UNKNOWN","summary":"<redacted fact>","evidence":"<private path or bounded identity>"}
```

If an earlier entry was wrong, append a correction referring to it. Record attempted effects and
their readback separately; a command starting is not proof that its effect completed.

## Mandatory closeout

The operator may set `State: CLOSED` only when authenticated affected-path recovery is evidenced
across the relevant restart/refresh boundary and the entire stated monitoring/watch window has
completed without recurrence. This gate may not be `NOT_RUN`, `MISSING_CAPABILITY`, `UNKNOWN`,
Human Input, or owned residual work. If it is not satisfied, keep the incident `MONITORING` or
`MITIGATED`. Every remaining item must be evidenced or explicitly remain as owned residual work:

- [ ] **Non-deferrable close gate:** authenticated affected-path recovery is proven across the
  relevant restart/refresh boundary and the entire stated monitoring/watch window completes
  without recurrence; process/HTTP-only evidence and an unverified user report are not substituted.
- [ ] For shared-ingress involvement, installed routes were read back and every affected public
  path plus one unchanged sibling path was checked; product-local state was not reset without
  causal evidence.
- [ ] Service posture is returned from `GATE`/`SUSPEND` to the intended steady state, or an owner
  and removal trigger are recorded.
- [ ] Every rollback has an explicit roll-forward/redeploy/retirement decision; exact removed
  candidate and bundled good work are accounted for; forward-only state was not rewound.
- [ ] Every temporary Project Settings, `START HERE`, agent-guidance, canary, or HOLD change is
  `REMOVED` or reviewed and installed as permanent guidance.
- [ ] Technical RCA distinguishes trigger, root and contributing causes; response-process RCA
  covers detection, diagnosis, communication, mitigation, and recovery.
- [ ] Causal regression/qualification evidence exists, with exact `NOT_RUN`,
  `MISSING_CAPABILITY`, or `UNKNOWN` preserved.
- [ ] Monitoring and Wakeful detection/notification follow-up is completed or has an owner,
  target date/event trigger, and acceptance proof.
- [ ] Every other follow-up has one owner, next action, due date or event trigger, and clearing
  evidence; required Human Input is named exactly.
- [ ] The append-only timeline, final status, exact runtime/candidate identities, bounded logs,
  decisions, and handoffs are retained in the incident directory.

Immediately after recovery, keep using this record and execute this checklist. Recovery does not
create a pause, a second incident, or a need for another prompt.

Closeout does not require all long-term engineering to be finished. It requires that nothing is
silently abandoned: every residual has durable identity, ownership, return trigger, and truthful
state.
