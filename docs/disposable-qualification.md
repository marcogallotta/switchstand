# Disposable qualification

Slice C keeps test support and its causal process test in one reviewable change: a
runner without the replay/recovery proof would not establish the assigned outcome.
The canonical owners remain PostgresState, GrantState, the existing edge entry point,
and the existing synthetic Provider. No production issuer or bridge is added.

Run from the private task writer with installed PostgreSQL tools and the bootstrapped
Python environment:

```
/home/marco/switchstand/.venv/bin/python tests/disposable_postgres.py scripts/check tests/test_chatgpt_edge_process.py
```

The runner generates its own authentication and explicit TEST_DATABASE_URL, initializes
an exclusive cluster, verifies its data directory, and runs the candidate migrations.
It never accepts a database URL or provider credential from the caller. The loopback
port is selected dynamically; a startup collision fails without adopting the listener.
A Linux subreaper scope collects orphaned descendants, including GNU timeout groups,
on cancellation; only children owned by this invocation are stopped. This requires
Linux pidfds and starts before any child processes. Existing source, other clusters,
and the owner-only `.qualification` evidence directories remain intact. Logs and
synthetic state are retained on success and failure. SIGKILL of the runner is outside
the cleanup guarantee; no later run adopts or kills residue.

The process journey covers real sockets, edge subprocesses and PostgreSQL persistence.
Identity verification and the provider are explicit synthetic substitutions. Its
`authenticated` principal and `real:disposable-switchstand-test` qualification are
fixtures required to exercise the existing edge contract, not evidence of real
identity, real Asana, or an authority grant to a live caller. Real isolated-Asana
qualification remains **NOT_RUN** until its existing external prerequisites exist.

Cleanup and protocol evidence remain one slice: separating cleanup would leave the
disposable qualification claim invalid. Cancellation tests include a TERM-resistant
child, a nested timeout group and an unrelated sentinel process.
