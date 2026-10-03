# Resource-governed agent workers

Managed parent Workers now enter through this resource-governed runtime by default. The live
systemd/cgroup qualification remains a distinct, separately authorized canary effect.

- Layer 1 (active): one per-user lease authority at `~/.local/state/switchstand/agent-broker` performs pressure admission, nested-budget accounting, atomic reservation, and durable status. Initial admission obtains a bounded second pressure sample so recent swap movement is known.
- Layer 2 (active path, live proof pending): a sealed manifest is the executor's only production input. A transient systemd service applies the aggregate lease's cgroup-v2 limits and exact path sandbox. Timeout or signal cleanup remains `UNKNOWN` unless the exact unit is terminal and its cgroup is positively read as empty.
- Layer 3 (qualification): the explicit canary exercises limits, recursive cancellation, excess denial, sandboxing, and durable pressure/status capture. Running it remains a separate effect and landing does not claim that NOT_RUN proof.

The production path fails closed on pressure, identity, reservation, runtime, and cleanup ambiguity.
Native children share the parent's aggregate cgroup but do not pass through child-count admission.
A failed, incomplete, or `UNKNOWN` canary authorizes neither a live-proof claim nor blind retry.
