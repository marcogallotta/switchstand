# Switchstand

Switchstand is a deliberately small, provider-neutral MCP controller for bounded engineering work.
Bootstrap V1 exposes three STDIO tools—`work_get`, `work_update`, and `work_append`—and keeps provider
credentials and identifiers behind the controller.

The implementation targets Python 3.14, PostgreSQL, SQLAlchemy 2, Alembic, HTTPX, Pydantic 2, and
the MCP Python SDK v2. The retired implementation is preserved in `marcogallotta/switchstandold` for
reference only; this repository is a clean implementation of the current contract.

```bash
uv sync --locked --all-extras --dev
docker compose up --build
uv run pytest
scripts/switchstand-worktree <writer-name> <exact-green-SHA>
# In the printed linked worktree:
scripts/switchstand-launch --active <Asana task ID or URL>
```

See [architecture](docs/architecture.md), [development](docs/development.md), and [agent routing](AGENTS.md).
