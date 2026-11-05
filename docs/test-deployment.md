# Replace the existing TEST edge

Qualify an authorized candidate on the existing TEST service while preserving its OAuth connection.
Use [TEST lifecycle](chatgpt-mcp-edge.md#isolated-real-chatgpt-test-mcp) for identities and detailed checks.

1. Read current WorkId `9411a0ab-be9b-54ab-ab42-f9f649139b59` and reconcile its operator window.
   Obtain the exact candidate, compatible recovery target and action-specific resource admission.
2. Create a clean checkout with `scripts/switchstand-worktree <name> <full-sha>`.
   Match both dependency manifests and verify the retained environment against the lock before shutdown.
3. Read the TEST database owner, privilege flags and Alembic revision without printing credentials.
   A revision absent from the candidate blocks ordinary migration. Reset only explicitly disposable app-state.
4. Retain host `runtime.py` and logs; stage only its `WRITER`, `HEAD`, `VENV` assignments for review.
   Preserve the TEST database identity, OAuth configuration, `FASTMCP_HOME`, port and endpoint.
5. Stop only `switchstand-test-9411-527bd6f0.service` during the operator window.
   Apply the authorized TEST schema plan with readback, install the launcher, then start that same service.
6. Read `systemctl --user show switchstand-test-9411-527bd6f0.service -p MainPID -p InvocationID`.
   Bind that PID's executable, cwd and source to the clean SHA; verify loopback/public HTTP readiness.
7. Perform real authenticated `work_get` through the existing TEST connection, then test Mailbox behavior.
   Check the full tool result; record server authentication and fresh-client acceptance separately.
8. On failure retain diagnostics with `journalctl --user -u switchstand-test-9411-527bd6f0.service`.
   Stop the candidate, restore the compatible old launcher/schema, and repeat live checks for the old SHA.

Reconcile ambiguous effects before retrying. Preserve OAuth and shared resources during recovery.
Record exact commits, results and full elapsed time on current work; under 15 seconds requires measurement.
