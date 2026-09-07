FROM ghcr.io/astral-sh/uv:0.12.10-python3.14-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
COPY alembic.ini ./
COPY migrations ./migrations
CMD ["uv", "run", "switchstand"]
