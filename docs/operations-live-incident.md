# Live-incident operations

Use this runbook for any credible current live-user failure. The failure itself enters
incident mode; `CODE RED` is an additional explicit trigger, not a required incantation.
The immediate objective is current truth and the smallest safe useful recovery. Root-cause
analysis follows mitigation under the canonical
[root-cause analysis procedure](root-cause-analysis.md); unrelated work waits.

## First minute

1. **Acknowledge immediately.** Say: `ACK. Incident mode. One operator. Checking current
   service state and newest logs now.` Do not wait for a diagnosis. During an interactive
   urgent exchange, send a short factual update at least every 10 seconds, including `no
   change` when true. Marco saying he is leaving or setting another cadence ends that
   interactive requirement; keep the durable status current.
2. **Name one operator.** The operator owns the canonical record, live evidence,
   mitigation/deployment lane, and updates. Pause unrelated work. Delegate only one bounded
   independent lane after a current causal hypothesis exists; workers report to the
   operator rather than issuing competing status.
3. **Open or reuse one private incident record.** Reuse `current` only for the same active
   incident; otherwise create a new record:

   ```console
   incident_root="${XDG_STATE_HOME:-$HOME/.local/state}/switchstand/incidents"
   install -d -m 0700 "$incident_root"
   incident_dir="$(mktemp -d "$incident_root/INC-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
   chmod 0700 "$incident_dir"
   incident_id="${incident_dir##*/}"
   install -m 0600 /dev/null "$incident_dir/timeline.jsonl"
   : "${incident_operator:?set incident_operator to the one named operator}"
   incident_updated="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   incident_status_tmp="$(mktemp "$incident_dir/.status-XXXXXX")"
   chmod 0600 "$incident_status_tmp"
   cat > "$incident_status_tmp" <<EOF
   # $incident_id — live-user failure

   - State: DETECTED
   - Operator: $incident_operator
   - Updated: $incident_updated
   - Impact / understood scope: credible live-user failure; exact scope UNKNOWN
   - Difficulty / prognosis: unknown; diagnosis in progress
   - Causal hypothesis / confidence: UNKNOWN — no supported hypothesis;
     checking current service state and newest logs
   - Human / agent action: none pending current evidence
   - Service posture: PENDING — not selected until current evidence is assessed
   - Current action / next checkpoint: inspect current service state and newest logs
   - Coordination changes: none
   - Dependency boundary: UNKNOWN pending boundary checks
   - Evidence: UNKNOWN; no current evidence captured yet
   - Residual truth: authenticated recovery proof NOT_RUN
   EOF
   mv -f -- "$incident_status_tmp" "$incident_dir/status.md"
   test "$(stat -c '%a' "$incident_dir/status.md")" = 600
   incident_pointer="$incident_root/.current.$$"
   ln -s "$incident_dir" "$incident_pointer"
   mv -Tf "$incident_pointer" "$incident_root/current"
   printf '%s\n' "$incident_id" "$incident_dir"
   ```

   `timeline.jsonl` is append-only: append one timestamped JSON object for each material
   observation, decision, command/effect, result, handoff, and status transition; never
   rewrite or truncate it. Keep secrets, authorization URLs, tokens, credentials, and raw
   personal data out. `status.md` is the current summary. Initialize it atomically with valid
   mode `0600` before publishing `current`. For every later update, write a mode-`0600`
   same-directory temporary file, then `mv -f` it over `status.md`; readers must never see a
   missing or partial current status.
   Use the exact templates in [Incident record and closeout](operations-incident-record.md).
4. **Read current evidence first.** For the ChatGPT edge:

   ```console
   incident_since="$(date --date='10 minutes ago' --iso-8601=seconds)"
   incident_until="$(date --iso-8601=seconds)"
   systemctl --user status --no-pager switchstand-chatgpt-mcp.service
   journalctl --user -u switchstand-chatgpt-mcp.service \
     --since "$incident_since" --until "$incident_until" --no-pager
   incident_log="$(mktemp "$incident_dir/journal-XXXXXX.log")"
   chmod 0600 "$incident_log"
   journalctl --user -u switchstand-chatgpt-mcp.service \
     --since "$incident_since" --until "$incident_until" --no-pager > "$incident_log"
   printf '%s\n' "$incident_log"
   ```

   Give Marco a requested command or the printed exact private log path before analysis.
   Never substitute an old pasted trace for the newest bounded journal window.
5. **Classify every material datum.** `CURRENT` is tied to the active process and incident
   window. `HISTORICAL` predates it. `UNKNOWN` lacks sufficient identity or time evidence.
   Keep process-up, transport-up, authentication, tool execution, provider read, provider
   write, and end-user impact as separate claims.
6. **Bound the dependency layer.** Compare the failing public path with its loopback origin
   and with one harmless sibling service that uses the same ingress. Simultaneous sibling
   failures, or healthy origins behind failed public paths, make shared ingress the leading
   hypothesis; inspect its current route/status and newest logs before changing a product.
   A healthy sibling narrows scope but does not prove the product healthy. Record a tiny
   dependency map (`client -> public ingress -> product origin -> auth -> provider`) and the
   evidence at each boundary. Do not reset OAuth, databases, registrations, or product state
   merely because the public path failed.
7. **Test the user path.** A listener, HTTP 200, OAuth challenge, or metadata document is
   not functional health. Use only the fixed, pre-registered canary identity with a
   read-only grant and fixed harmless target. It lists tools and performs one representative
   read with the affected identity class. Never dynamically register, authorize,
   reauthorize, or broaden the canary during an incident. If that exact capability is absent
   or expired, report `MISSING_CAPABILITY`; do not substitute a fresh identity or declare
   functional health.
8. **Choose service posture and mitigate.** Record one of:
   - `RUN`: known impact is bounded or fail-closed and the unaffected path remains useful.
   - `GATE`: the affected path can be isolated while useful service remains.
   - `SUSPEND`: continued operation risks unsafe writes, corruption, security exposure, or
     effects whose truth cannot be bounded.

   State why, what would change the decision, and whether Marco should notify or pause
   agents. Prefer the smallest reversible causal mitigation. Preserve state before mutation,
   read back the result, and never blindly retry an ambiguous effect. If no proved mitigation
   exists, say so plainly.

   Before this evidence-backed decision, record service posture as `PENDING`. `PENDING` is
   temporary uncertainty, not a synonym for `GATE`; replace it with `RUN`, `GATE`, or `SUSPEND`
   as soon as step 8 is assessed.

## Communication and control

The active chat is Marco's stable update channel; the incident's `status.md` is the canonical
current record and must contain the exact same truth. If chat continuity fails, give the exact
private `status.md` path on re-entry. Every update is short and contains:

- **Impact and understood scope** — what is failing, for whom, and fail-open/fail-closed/
  unknown boundaries.
- **Difficulty / prognosis** — trivial, contained, architectural, or unknown; expected repair
  horizon and its factual basis without invented precision.
- **Causal hypothesis / confidence** — one specific hypothesis and `LOW`/`MEDIUM`/`HIGH`
  confidence, with supporting and contradicting evidence, credible alternatives, and the next
  falsifier; otherwise `UNKNOWN` with the first discriminating check.
- **Human / agent action** — what Marco should do, what agents should be told, or `none`.
- **Service posture** — `PENDING` before step 8; afterward `RUN`, `GATE`, or `SUSPEND`, with
  the reason.
- **Current action / next checkpoint** — one action and the next observable result or time.
- **Coordination changes** — temporary or permanent changes needed in Project Settings,
  `START HERE`, agent guidance, canary state, or HOLD; otherwise `none`.

Causal confidence is advisory inference only. Keep observations, currentness, service posture,
effect readback, recovery proof, and closeout state deterministic under their existing labels and
gates; confidence cannot replace or change them.

For every temporary coordination change record its exact surface/value, authority, owner,
effective time, removal trigger, and state: `PROPOSED`, `ACTIVE`, `SUPERSEDED`, or `REMOVED`.
Do not mutate Project Settings or provider state without the applicable authority. Temporary
guidance must be reverted after recovery or deliberately reviewed and made permanent; it may
not silently persist.

`STOP` is a conversational and operational interrupt: acknowledge, issue no new commands or
effects, listen to Marco's newest direction, state any in-flight atomic action without blindly
interrupting its safety/rollback path, and re-ground the plan. Do **not** terminate workers or
abandon the incident merely because Marco said `STOP`.

`CANCEL` or `STOP WORK` means stop commands and effects and terminate active workers
best-effort, then report anything in flight or not stoppable. A later `resume` starts from
current evidence and the canonical record; it never blindly continues queued effects.

## Known edge-maintenance window

The production edge replacement deliberately uses one short single-runtime outage. The operator
announces the exact attempt and its mode-`0600` `receipt.json` path before gating. That receipt's
`phase` and `status` are the machine-readable deployment state; do not infer maintenance merely
from a connection failure. The verified gate returns HTTP `503` with `Retry-After: 60` across the
Switchstand MCP, OAuth, and metadata paths. ChatGPT or Codex may collapse that response to generic
`McpServerError` or `Connection failed` text.

Only while that exact receipt says the deployment is running may agents wait and use bounded
backoff for connection establishment and read-only calls. Respect `Retry-After` when exposed;
otherwise retry after 2, 5, 10, then 20 seconds, bounded by 90 seconds from gate announcement.
Never automatically retry a write, OAuth transition, or `UNKNOWN` effect: preserve its OperationId
and use its specified readback/reconciliation path. If service is not restored within 90 seconds,
or the receipt becomes `UNKNOWN`, stop treating the error as planned maintenance and enter the
incident procedure above immediately.

After restoration, agents resume their exact owned reads and watches rather than abandoning them.
Client-visible schema or metadata changes still require the reinstall procedure in
[ChatGPT MCP edge](chatgpt-mcp-edge.md); maintenance does not waive it.

## Shared-ingress recovery

When multiple products share a public tunnel, proxy, DNS record, certificate, or gateway, a
single ingress failure can mimic independent service or OAuth failures. Treat the common boundary
as one hypothesis, not a conclusion. Capture current ingress status/configuration before mutation;
compare public and direct-origin results; and use the narrowest reversible repair that preserves
unrelated routes. Do not restart or reset every downstream service as a diagnostic shortcut.

After an ingress repair, read back the installed routes and prove each previously affected public
path separately. For Switchstand, proof still requires the fixed authenticated canary; for a
sibling product, use its own harmless functional check. Also verify that a service which was not
supposed to change still works. Record whether the repair changed shared configuration, which
services inherited the effect, and the owner of any needed isolation or monitoring follow-up.

## Rollback and forward recovery

A rollback is temporary incident mitigation, not completion and not permission to forget the
reverted package. Before or immediately after rollback, record:

- exact removed runtime/candidate/revision and the restored target;
- state changed while the removed candidate was live;
- the causal suspect or defect, separately from unrelated good work bundled in the package;
- an owned disposition obligation for every removed change: `ROLL_FORWARD`, `REDEPLOY`, or
  `RETIRE_EXPLICITLY`, with prerequisites and a return trigger.

Never restore an old snapshot across a forward-only authority, schema, effect-journal, token,
provider-write, or other irreversible boundary. Restore compatible code around current state,
then forward-fix. A rollback obligation remains open after service recovery until all bundled
good work is redeployed or explicitly reviewed and retired.

## Exit and closeout

Mitigation is established only when the affected authenticated path succeeds, the result
survives the relevant restart/refresh boundary, current logs show no recurrence for the stated
watch window, and the service posture is re-evaluated. `MITIGATED` is not `CLOSED`.

A user report such as `it worked` is valuable `CURRENT` recovery evidence, but it is a checkpoint,
not a stop signal and not full functional proof. Acknowledge it, timestamp it in the same incident
record, run the bounded verification above, and proceed directly through cleanup and closeout work.
Do not wait for Marco to ask again, open a second incident, or leave the remaining incident backlog
implicit. If verification would be disruptive or needs unavailable authority, preserve that exact
item as `NOT_RUN` with an owner and return trigger while continuing every other safe item. The
authenticated affected-path recovery proof is not deferrable: it must succeed across the relevant
restart/refresh boundary and the entire stated watch window must complete without recurrence before
`CLOSED`. If that proof cannot run or fails, remain `MONITORING` or `MITIGATED`; never residualize it.

Before `CLOSED`, complete the checklist in
[Incident record and closeout](operations-incident-record.md): authenticated recovery proof;
rollback/roll-forward and bundled-good-work disposition; removal or permanent adoption of every
temporary setting; technical and response-process RCA; causal regression tests; monitoring and
Wakeful follow-up; owned actions with dates or event triggers; and explicit residual
`NOT_RUN`, `MISSING_CAPABILITY`, `UNKNOWN`, or Human Input. No item may disappear because the
live symptom stopped.

Incident recovery, causal-model acceptance, correction ownership, and correction validation are
separate outcomes. Use the RCA procedure's exact parent/child identities and lifecycle; incident
closure never turns an unvalidated correction into proof or removes its active follow-up watch.

Monitoring must distinguish transport from authenticated function. Its authenticated probe uses
the fixed, pre-registered, read-only canary and never creates or reauthorizes identities. At
minimum alert on process crash loops, authenticated-canary failure or `MISSING_CAPABILITY`,
bounded OAuth refresh failures, and provider-read failure. Wakeful emits deduplicated state
transitions with durable acknowledgement; until that integration is activated and accepted, do
not claim an inactive chat will receive alerts.

## Research basis

- **USED — Google SRE, [Managing Incidents](https://sre.google/sre-book/managing-incidents/):**
  supports early declaration, one recognized command path, bounded operational ownership, frequent
  updates, a live incident document, restoring service before RCA, and tracking divergence from
  normal until it is reverted. Switchstand keeps those semantics but collapses the roles for its
  single-operator scale.
- **USED — Google SRE Workbook,
  [Postmortem Culture](https://sre.google/workbook/postmortem-culture/):** supports recording impact,
  trigger/root/contributing causes and recovery effort, and assigning concrete tracked prevention,
  mitigation, and detection actions rather than treating symptom recovery as done.
- **USED — PagerDuty,
  [Incident Commander training](https://github.com/PagerDuty/incident-response-docs/blob/master/docs/training/incident_commander.md)
  and [After an Incident](https://docs.pagerduty.com/ops-guides/incident-response-guide/after-an-incident):**
  independently support concise factual updates, explicit task acknowledgement and verification,
  and assigning the post-incident work. Switchstand rejects its larger-team ceremony and makes the
  single operator continue the owned backlog directly after recovery.
