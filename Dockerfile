FROM python:3.14-slim-bookworm AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
CMD ["uv", "run", "--no-sync", "switchstand"]

FROM base AS development
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
COPY tests ./tests
RUN uv sync --locked --all-groups && uv run --no-sync pyright --version

FROM base AS runtime
RUN uv sync --locked --no-dev
