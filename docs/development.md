# Development

- Install/sync: `uv sync --locked --all-extras --dev`
- Quality: `uv run ruff check . && uv run pyright && uv run pytest`
- Services: `docker compose up --build`
- Migration: `uv run alembic upgrade head`
- MCP server: set `DATABASE_URL`, `ASANA_TOKEN`, `ACTIVE_WORK_ID`, optional `REFERENCE_WORK_IDS`; run `uv run switchstand`

CI runs on Python 3.14 with PostgreSQL. Correctness, types, and tests block; formatting is reported without rewriting
review diffs. Stage branches and pull requests are based on the exact last accepted green SHA. Integration admits
State, Provider, then MCP and reruns affected plus full gates after each admission.
