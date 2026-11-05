# Coordfix execution and activation test plan (2026-11-05)

Outcome: fresh Codex Coordinator reads Situation, focus-now Priorities, notes first; owns its mailbox; replies to exact claude-coordinator mail; wakes from idle and replies again. Never resume 01a1a811.

Scope: reuse PR1123 direct cgroup admission with systemctl fallback; explicit --fresh launcher. Preserve thresholds, count gate, caller exclusion and inherited finite memory limits. One PR because admission and deterministic fresh start jointly qualify this recovery. No provider deployment/database changes, no ~/.claude.

Before landing: independent exact-head review PASS; six focused tests; real cgroup/bus-denied old WAIT/new GO; admission E2E if a fresh GO admits its 768 MiB allocation. CI/current-main composition check.

Activation: record old canonical commit and thread-id; deploy only reviewed admission script/start adapter from the owned candidate to their canonical tracked paths via landing and fast-forward (integration owner handles shared main). Do not restart shared daemon. Run fresh 512 MiB admission immediately before one `PYTHONPATH=/home/marco/switchstand/src /home/marco/switchstand/.venv/bin/python /home/marco/switchstand/recovery/coordinator/start --fresh`. No automatic retry of ambiguous thread/start or turn/start. Read back thread-id and inspect exact new thread turns. Verify first work_get calls are the three exact IDs in order; verify old thread not resumed.

Parent provides full current Marco confirmation message to new Coordinator for its own agent_takeover. Parent asks claude-coordinator to send unique nonce mail; target replies from exact Codex Coordinator identity and acknowledges. Wait until new thread idle; send second distinct nonce from claude-coordinator; prove turn starts through inbound wake, pending is received and replied to with same nonce, not a manual turn/start. Inspect actual wake admission and outcome.

Failure: on a failed production change rollback exactly once, stop and report. Restore only changed files/thread pointer from recorded preimage as appropriate; interrupt only the newly launched turn if necessary; never whole DB restore, never resume old coordinator. Ambiguous effects require reconciliation, never blind launch retry. Parent handles all mailbox operations for parent identity; target handles its own mailbox.
