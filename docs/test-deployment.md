# Replace the existing TEST edge

Qualify an authorized candidate on the existing TEST service while preserving its OAuth connection.
Read current WorkId `9411a0ab-be9b-54ab-ab42-f9f649139b59` and [TEST lifecycle](chatgpt-mcp-edge.md#isolated-real-chatgpt-test-mcp) first.
Obtain the operator window, exact clean candidate/environment and compatible schema/recovery plan before effects.

Set `APP`, `VENV` to assigned absolute paths and `CANDIDATE_SHA` to the full assigned SHA; run this read-only preflight:
```bash
set -eu
test "$(git -C "$APP" rev-parse HEAD)" = "$CANDIDATE_SHA"
test -z "$(git -C "$APP" status --porcelain)"
UV=$("$APP/scripts/bootstrap" --print-uv)
UV_PROJECT_ENVIRONMENT="$VENV" "$UV" sync --project "$APP" --locked --all-groups --no-install-project --dry-run --offline
docker exec -i switchstand-postgres-1 psql -X -v ON_ERROR_STOP=1 -U switchstand -d switchstand_test_9411_527bd6f0 -At <<'SQL'
BEGIN READ ONLY;
SELECT current_database(),r.rolname,r.rolsuper,r.rolcreatedb,r.rolcreaterole,r.rolreplication,r.rolbypassrls,
       (SELECT version_num FROM alembic_version)
FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=current_database();
ROLLBACK;
SQL
```
Require uv to report no changes, the exact TEST database/owner, five false privilege flags and a candidate-recognized revision; otherwise stop.
The recorded `0027_agent_status` branch blocks ordinary candidate migration. Reset only explicitly disposable app-state.
Retain host `runtime.py` and logs; review only its `WRITER`, `HEAD`, `VENV` replacements. Preserve OAuth/`FASTMCP_HOME` and shared resources.
During the window, stop only `switchstand-test-9411-527bd6f0.service`; execute the authorized schema plan, install the launcher and start that service.
Bind its new MainPID/InvocationID, executable, cwd and source to the clean SHA; verify TEST loopback/public HTTP and real authenticated `work_get`, then Mailbox behavior.
On failure retain its journal, stop the candidate, restore the compatible old launcher/schema and repeat live checks. Reconcile ambiguous effects before retrying.
Record exact commits, server/client/recovery results and full elapsed time; under 15 seconds requires measurement.
