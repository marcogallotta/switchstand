FROM python:3.14-slim-bookworm AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
CMD ["uv", "run", "--no-sync", "switchstand"]

FROM base AS development
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --locked --all-groups --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests
COPY .codex/config.toml ./.codex/config.toml
RUN uv sync --locked --all-groups && uv run --no-sync pyright --version

FROM base AS runtime
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --locked --no-dev
