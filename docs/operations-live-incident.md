# Live-incident operations

Use this runbook for any credible live-user failure. `CODE RED` is a useful signal,
not a required incantation. The immediate objective is to establish current truth and
restore the smallest safe useful path. Root-cause analysis and unrelated work follow.

## First minute

1. **Acknowledge immediately.** Say: `ACK. Incident mode. One operator. Checking
   current service state and newest logs now.` If urgency is explicit, send a concise
   factual update at least every 10 seconds during interactive diagnosis. Do not wait
   for a complete answer.
2. **Use one operator.** Pause broad delegation and unrelated work. Add a worker only
   after a current causal hypothesis exists and the worker has one independent bounded
   lane.
3. **Read current evidence first.** For the ChatGPT edge:

   ```console
   systemctl --user status --no-pager switchstand-chatgpt-mcp.service
   journalctl --user -u switchstand-chatgpt-mcp.service --since "10 minutes ago" --no-pager
   journalctl --user -u switchstand-chatgpt-mcp.service > /tmp/switchstand-chatgpt-mcp.log
   ```

   Give the requested command or log path before analysis. Never substitute an old
   pasted trace for the newest journal window.
4. **Classify every material datum.** `CURRENT` is tied to the active process and
   incident window. `HISTORICAL` predates it. `UNKNOWN` lacks enough identity or time
   evidence. Keep process-up, transport-up, authentication, tool execution, provider
   read, and provider write as separate claims.
5. **Test the user path.** A listener, HTTP 200, OAuth challenge, or metadata document
   is not functional health. Use a bounded authenticated canary that lists tools and
   performs a harmless representative read with the affected identity class.
6. **Mitigate before expanding.** Prefer the smallest reversible causal mitigation.
   Preserve state before mutation, read back the result, and never blindly retry an
   ambiguous effect. If no proved mitigation exists, say so plainly.

## Communication and control

Each update should fit four lines: impact; current evidence; action in progress; next
check. State `no change` when true. Do not present a hypothesis as a cause, announce a
deployment that has not happened, or call a service healthy from transport evidence.

`STOP` is an interrupt: acknowledge it before cleanup, issue no new commands or
effects, cancel active workers and tool sessions best-effort, then report residual work
that could not be stopped. A later `resume` starts from current evidence; it does not
blindly continue queued effects.

Delegate only after the operator has bounded the failure. Good parallel lanes are an
independent candidate review, a copied-state reproducer, or monitoring design. The
operator retains the live log, mitigation, and deployment lane.

## Exit and follow-up

Mitigation is complete only when the affected authenticated path succeeds, the result
survives the relevant restart or refresh boundary, and current logs show no recurrence.
Record exact runtime identity, evidence window, changes, readback, residual risk, and
rollback or forward-recovery boundary. Then perform the technical RCA, process RCA,
regression work, and monitoring follow-up.

Monitoring must distinguish transport from authenticated function. At minimum alert on
process crash loops, authenticated-canary failure, bounded OAuth refresh failures, and
provider-read failure. Wakeful integration is follow-up work: emit state transitions
with deduplication and durable acknowledgement so a future Coordinator can be awakened
for a new failure. Until that path is implemented and accepted, do not claim an
inactive chat will receive alerts.
