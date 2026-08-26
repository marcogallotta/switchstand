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

Issue #3's native-agent-tree checkpoint is staged but not claimed complete. The separate
`agent_tree.py` protocol layer explicitly enumerates all documented root and subagent source
kinds, exhausts descendant pagination, validates spawned ancestry from `parentThreadId`,
preserves native runtime status, and exposes exact native start/steer/interrupt seams. Its
fixtures are documented protocol-shape fixtures, not live captures. The default read-only
`switchstand-stage-a` snapshot CLI now turns a real socket plus one exact root thread id into
redacted machine-readable evidence or a nonzero fail-closed reason. Its separate notification
mode requires an explicit runtime-loading subscription opt-in. Until that command observes a
real root plus spawned descendant, the synthetic two-role UI remains the main surface and
Stage B must not begin.

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
node --test tests/browser_focus.test.js
node --check src/switchstand/static/app.js
```

The Node commands are optional, cover the browser refresh regression, and check JavaScript
syntax; Node is not needed to run Switchstand.

## Native Stage A probe

Run one complete read-only snapshot against an exact known native root thread id:

```sh
PYTHONPATH=src python -m switchstand.stage_a_probe \
  --app-server-socket /path/to/codex-app-server.sock \
  --root-thread-id EXACT_ROOT_THREAD_ID
```

The default command does not discover or guess a root and does not resume, load, or mutate
threads. It prints one JSON object to stdout. See
[Development](docs/development.md#run-the-native-stage-a-probe) for bounded polling, the
explicit notification-subscription consequence, output fields, and exit codes.

## Repository layout

- `src/switchstand/engine.py` — flat-file state machine and reconciliation
- `src/switchstand/app_server.py` — minimal Codex app-server Unix WebSocket client
- `src/switchstand/agent_tree.py` — fail-closed native tree observation/control checkpoint
- `src/switchstand/stage_a_probe.py` — read-only machine-readable live checkpoint CLI
- `src/switchstand/service.py` — local HTTP/API/static-file process
- `src/switchstand/static/` — vanilla HTML, CSS, and JavaScript operator UI
- `tests/` — standard-library unit tests
- `docs/` — product boundary, architecture, development workflow, and decisions

## Documentation

- [Product](docs/product.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [Prototype boundary decision](docs/decisions/0001-prototype-boundary.md)
- [Native tree checkpoint decision](docs/decisions/0002-native-tree-checkpoint.md)

Switchstand is available under the [MIT License](LICENSE).
