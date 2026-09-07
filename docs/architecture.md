# Architecture

The STDIO MCP adapter projects exactly three tools onto a controller service. The service enforces launch-bound
read/write authority, stale revision checks, canonical-project membership, minimal provider effects, and
authoritative readback. A thin provider adapter owns Asana/HTTPX details. A thin state adapter maps opaque UUID
WorkIds to provider records and locks each mapping during writes.

PostgreSQL contains one table: `work_handles`. Provider credentials remain in the controller process environment;
agents see neither credentials nor raw provider operations. This is accidental-scope containment, not a hardened
multi-user security boundary.

Bootstrap excludes inbound HTTP, UI, orchestration, remote execution, claims, leases, fences, audit storage, and
broad Asana APIs. The retired `switchstandold` tree supplies evidence only and is not an architectural ancestor.
