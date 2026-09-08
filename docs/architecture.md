# Architecture

The STDIO MCP adapter projects exactly three tools onto a controller service. The service enforces launch-bound
read/write authority, stale revision checks, canonical-project membership, minimal provider effects, and
authoritative readback. A thin provider adapter owns Asana/HTTPX details. A thin state adapter maps opaque UUID
WorkIds to provider records and locks each mapping during writes.

PostgreSQL contains one application table, `work_handles`, stored on a named Compose volume. Local configuration
lives in `~/.config/switchstand/.env`; Compose passes it to the controller. The MCP surface exposes no raw provider
operations. A separate operator-only command validates explicit Asana tasks as canonical, binds them to stable opaque
WorkIds, and prints the launch configuration without exposing provider IDs to agents.

Bootstrap excludes inbound HTTP, UI, orchestration, remote execution, claims, leases, fences, audit storage, and
broad Asana APIs. The retired `switchstandold` tree supplies evidence only and is not an architectural ancestor.
