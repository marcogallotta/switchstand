# Resource-governed agent workers

This is a default-off trial architecture, not the active worker launcher.

- Layer 1 (current, inert): one per-user lease authority at `~/.local/state/switchstand/agent-broker` for hostile-spool validation, pressure admission, nested budgets, recursive cancellation, and durable status; workers see only their exact inbox, it never starts/signals processes, and activation remains separately authorized.
- Layer 2 (current, inert): an opt-in host-operator transient systemd service applies each reserved leaf lease's hard cgroup-v2 limits and read-only sandbox. The worker cannot reach the Docker socket or user-manager bus. Timeout cleanup fails UNKNOWN rather than releasing a lease when systemd cannot confirm the stop.
- Layer 3 (current, inert): an explicit canary command prepares a two-light-worker proof, a recursive ledger-cancellation rehearsal, deterministic third-worker denial, read-only output evidence, Docker-socket denial, and durable pressure/status capture. Running it is a separate activation effect: first confirm the host pressure guard is green, then authorize the live systemd proof; do not treat landing this package as activation.

Adoption requires that exact separately authorized canary to pass and an explicit decision assigning
the active runtime owner. A failed, incomplete, or `UNKNOWN` proof authorizes neither partial
reliance nor blind retry; it leaves an explicit diagnose-or-retire decision. Retirement requires
recorded non-adoption and bounded disposal of broker state and any transient units. The repository
architecture map owns the corresponding edit and retirement boundaries.
