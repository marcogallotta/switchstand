# Switchstand

Switchstand is an experimental local operator surface for talking directly to two durable
logical Codex roles working on one Work. It preserves message order and role checkpoints,
exposes exact attempt controls, and reports uncertain or stale execution state instead of
inventing completion.

This repository is a clean prototype, not a production orchestrator. Its purpose is to make
one implemented vertical slice easy to run, inspect, and change: flat JSON/JSONL durability,
direct and queued messages, exact stop/redirect/replace operations, restart reconciliation,
and result fencing by role generation plus attempt identity.

## Status and checkpoint

The dependency-free engine, service, browser UI, and Codex Unix-socket adapter are
implemented and covered by local unit tests. The adapter speaks the real Codex app-server
v2 thread/turn protocol. The **live checkpoint has not been run** in this standalone
repository: no claim is made that a real local Codex daemon, authentication, or model turn
has been exercised here.

## Non-goals

- General-purpose multi-agent orchestration or arbitrary role counts
- A database, distributed queue, account system, hosted service, or production hardening
- Repository, issue-tracker, release, deploy, or other lifecycle automation
- Inferring completion, replay safety, or authority when acknowledgements are unavailable
- Abstracting multiple model providers behind a common adapter

## Quick start

Requirements: Python 3.11+ and a running Codex app-server Unix socket. No third-party runtime
packages or frontend build are required.

```sh
cd switchstand
PYTHONPATH=src python -m switchstand.service \
  --app-server-socket /path/to/codex-app-server.sock \
  --workspace /path/to/operator/workspace
```

Then open <http://127.0.0.1:4180/>. State defaults to
`~/.local/state/switchstand/state.json`; use `--state` to choose another file. Run
`PYTHONPATH=src python -m switchstand.service --help` for all options.

Run the dependency-free checks:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src tests
node --check src/switchstand/static/app.js
```

The Node command is optional and only checks JavaScript syntax; Node is not needed to run
Switchstand.

## Repository layout

- `src/switchstand/engine.py` — flat-file state machine and reconciliation
- `src/switchstand/app_server.py` — minimal Codex app-server Unix WebSocket client
- `src/switchstand/service.py` — local HTTP/API/static-file process
- `src/switchstand/static/` — vanilla HTML, CSS, and JavaScript operator UI
- `tests/` — standard-library unit tests
- `docs/` — product boundary, architecture, development workflow, and decisions

## Documentation

- [Product](docs/product.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [Prototype boundary decision](docs/decisions/0001-prototype-boundary.md)

Switchstand is available under the [MIT License](LICENSE).
