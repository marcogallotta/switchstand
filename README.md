# Switchstand

Switchstand is a deliberately small, provider-neutral MCP controller for bounded engineering work.
The source/history/feedback capability exposes `work_get`, `source_task`, `source_stories`,
`source_story` and `work_append`. Provider credentials stay behind the controller; opaque WorkIds
and explicit read-only Asana task/story identities remain separate. See the
[source and feedback usage guide](docs/source-history-feedback.md).

The implementation targets Python 3.14, PostgreSQL, SQLAlchemy 2, Alembic, HTTPX, Pydantic 2, and
the MCP Python SDK v2. The retired implementation is preserved in `marcogallotta/switchstandold` for
reference only; this repository is a clean implementation of the current contract.

## Current recovery status

The landed Stage-1 code-red tranche contains three bounded repairs:

- Git landing reconciliation now accepts the repository's one-parent squash flow only when the landing is directly
  based on the reviewed base, the reviewed candidate descends from that base, and the landed tree equals the reviewed
  candidate tree.
- Existing writer reuse now requires a clean registered linked worktree whose Git common directory is exactly the
  requesting repository's common directory, so a same-named worktree from another clone is rejected.
- `scripts/check` starts its shared 120-second deadline before bootstrap/setup, applies the remaining budget through
  setup, manifest verification and focused checks, forcibly terminates TERM-resistant timed-out setup, and does not
  fall back to a full container build.

These repairs do not close all code-red findings. Docker resource ownership/cancellation remains unresolved, and the
development launch/control path is still repository-supplied rather than an independently pinned CONTROL surface.

```bash
scripts/bootstrap
docker compose up --build
uv run pytest
scripts/switchstand-worktree <writer-name> <exact-green-SHA>
scripts/switchstand --active <Asana task ID or URL>
# In the printed linked worktree:
scripts/switchstand-launch --active <Asana task ID or URL> --commit <exact-candidate-SHA>

scripts/switchstand --isolated --active <Asana task ID or URL> --commit <exact-candidate-SHA>
```

See [architecture](docs/architecture.md), [development](docs/development.md), and [agent routing](AGENTS.md).
