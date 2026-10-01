# Resource-governed agent workers
- Layer 1 (current, inert): one per-user lease authority at `~/.local/state/switchstand/agent-broker` for hostile-spool validation, pressure admission, nested budgets, recursive cancellation, and durable status; workers see only their exact inbox, it never starts/signals processes, and activation remains separately authorized.
- Layer 2 (later): a host-operator executor using transient systemd user scopes and cgroup v2; workers never receive a Docker socket, while selectively justified heavy isolation may use Docker.
- Layer 3 (after review): canary at most two disposable read-only light workers and prove limits, output, cancellation, cleanup, and rejection.
