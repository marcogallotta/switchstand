# Switchstand

Switchstand is a deliberately small, provider-neutral MCP controller for bounded engineering work.
The source/history/feedback capability exposes `work_get`, `source_task`, `source_stories`,
`source_story` and `work_append`. Provider credentials stay behind the controller; opaque WorkIds
and explicit read-only Asana task/story identities remain separate. See the
[source and feedback usage guide](docs/source-history-feedback.md).

The implementation targets Python 3.14, PostgreSQL, SQLAlchemy 2, Alembic, HTTPX, Pydantic 2, and
the MCP Python SDK v2. The retired implementation is preserved in `marcogallotta/switchstandold` for
reference only; this repository is a clean implementation of the current contract.

```bash
scripts/bootstrap
docker compose up --build
uv run pytest
scripts/switchstand-worktree <writer-name> <exact-green-SHA>
# In the printed linked worktree:
scripts/switchstand-launch --active <Asana task ID or URL>

# Or create the task worktree and launch it in one operation:
scripts/switchstand-start --active <Asana task ID or URL>
```

See [architecture](docs/architecture.md), [development](docs/development.md), and [agent routing](AGENTS.md).

