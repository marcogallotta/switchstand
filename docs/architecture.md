# Architecture

The STDIO MCP adapter projects exactly three tools onto a controller service. The service enforces launch-bound
read/write authority, stale revision checks, approved-area membership, minimal provider effects, and
authoritative readback. A thin provider adapter owns Asana/HTTPX details. A thin state adapter maps opaque UUID
WorkIds to provider records and locks each mapping during writes.

PostgreSQL contains one application table, `work_handles`, stored on a named Compose volume. Local configuration
lives in `~/.config/switchstand/.env`; Compose passes it to the controller. The MCP surface exposes no raw provider
operations. A separate operator-only command validates explicit Asana tasks against the configured approved-area
registry, preserves exact task identity across project membership changes, binds them to stable opaque WorkIds,
and prints the launch configuration without exposing provider IDs to agents.

## Current development-control boundaries

The landed Stage-1 repairs narrow three previously unsafe development paths without claiming the wider code-red
recovery is complete. Git landing reconciliation follows the repository's one-parent squash model: the landing must be
the current result directly parented by the reviewed base, the reviewed candidate must descend from that base, and the
landing tree must equal the reviewed candidate tree. Existing writer reuse is repository-bound: a reusable target must
be the expected clean linked worktree registered by the requesting repository, with the same absolute Git common
directory, so another clone's same-named worktree is rejected. The `scripts/check` focused host path uses one
120-second deadline beginning before bootstrap/setup and applies the remaining budget through manifest verification
and the focused commands, with a forced-kill phase for TERM-resistant timeouts and no full-build fallback.

Important recovery defects remain outside those repairs. Docker cleanup/cancellation still lacks exact ownership and
cancellation proof for the affected resources. The development launcher/control path is still supplied
from this repository and ambient host inputs rather than from an independently pinned CONTROL release, so the current
development path must not be described as independent CONTROL.

Bootstrap excludes inbound HTTP, UI, orchestration, remote execution, claims, leases, fences, audit storage, and
broad Asana APIs. The retired `switchstandold` tree supplies evidence only and is not an architectural ancestor.
