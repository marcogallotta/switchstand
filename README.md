# Switchstand

Switchstand is a deliberately small, provider-neutral MCP controller for bounded engineering work.
The source/history/feedback capability exposes `work_get`, `source_task`, `source_stories`,
`source_story` and `work_append`. Provider credentials stay behind the controller; opaque WorkIds
and explicit read-only Asana task/story identities remain separate. See the
[source and feedback usage guide](docs/source-history-feedback.md).

The implementation targets Python 3.14, PostgreSQL, SQLAlchemy 2, Alembic, HTTPX, Pydantic 2, and
the MCP Python SDK v2. The retired implementation is preserved in `marcogallotta/switchstandold` for
reference only; this repository is a clean implementation of the current contract.

## Current repository truth

- Git landing reconciliation models the repository's reviewed two-parent GitHub merge-commit flow: the landing must
  have the reviewed base and candidate as its ordered parents, the candidate must descend from that base, and the
  landing tree must exactly match the reviewed candidate tree. Squash landings are rejected.
- `launch.py` owns managed-launch orchestration and process supervision. `codex_runtime.py` owns Codex CLI/App
  Server command construction, configuration/readback, and runtime profile validation. `development.py` owns the
  development-resource/workload mechanics used by launch; `docker.py` supplies exact low-level Docker identity and
  removal primitives. Remaining lifecycle-policy convergence is tracked separately rather than hidden in this summary.
- `candidate.py` owns persistent candidate/worktree identity and exact remote Git-ref materialization for isolated
  launch. `launch_source.py` owns the protected task-source parsing/bridge that supplies those exact identities.
- The authenticated ordinary HTTP/OAuth MCP exposes provider-neutral search/read/structure/history/attachment/event
  operations, protected append/create/update, message send/pending, and required-result saving. Raw `source_*` reads
  remain transitional compatibility until their proof-gated retirement. Repository `.codex/config.toml` explicitly
  allowlists the ordinary tool inventory.
- Full Quality prints skipped/xfail reasons. Landing-reconciliation tests are mandatory CI evidence: absence of a Git
  implementation capable of the production `--no-lazy-fetch` contract is a `MISSING_CAPABILITY` failure, not a
  green skip.

```bash
scripts/bootstrap
docker compose up --build
uv run pytest
scripts/switchstand-worktree <writer-name> <exact-green-SHA>
scripts/switchstand --active <Asana task ID or URL> -- <exact initial assignment>
# In the printed linked worktree:
scripts/switchstand-launch --active <Asana task ID or URL> --commit <exact-candidate-SHA>

scripts/switchstand --isolated --active <Asana task ID or URL> --commit <exact-candidate-SHA>
```

See [architecture](docs/architecture.md), [development](docs/development.md), and [agent routing](AGENTS.md).
