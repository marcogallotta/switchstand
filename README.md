# Switchstand

Switchstand is an experimental local operator surface for Codex work. Its explicitly selected
native mode observes one real Codex root and its descendants and can request cancellation of one
exact active turn after explicit confirmation.
The default legacy mode remains a fixed two-role reliability spike with durable messages,
checkpoints, attempt controls, and conservative failure handling.

This repository is a clean prototype, not a production orchestrator. It keeps two bounded
slices easy to inspect: truthful native-tree observation with one emergency control, and the legacy
flat-file reliability mechanisms for ordered messages, restart reconciliation, controls, and
result fencing.

## Status and checkpoint

The dependency-free engine, service, browser UI, and Codex Unix-socket adapter are covered by
local tests. Stage A passed live on exact PR4 head `8670f50b629ae3f201d5eed3aa04fc92afa9888b`
and was merged: one root plus one descendant, native `active` to `idle` notifications, exit 0,
and no conversation-history mutation. That proves the tested PR4 head, not a fresh live run of
this later main head.

Issue #9 adds Stage B1 as an explicitly selected native view. It polls one exact root with
`thread/read(includeTurns=false)` and all descendants with paginated
`thread/list(useStateDbOnly=true)`. It displays native lineage, native status, observer
freshness, consecutive observed-active time, and the latest 50 endpoint differences. It does not resume or
subscribe to threads or mutate conversation history. The legacy two-role engine remains
available as a reliability spike. Issue #12 adds one native-only emergency control: explicit
two-step confirmation requests interruption of one exact active turn. It does not undo work or
stop background processes or descendants.

## Non-goals

- General-purpose multi-agent orchestration or native-agent lifecycle control
- A database, distributed queue, account system, hosted service, or production hardening
- Repository, issue-tracker, release, deploy, or other lifecycle automation
- Inferring completion, progress, failure, staleness, intent, or a complete event history
- Abstracting multiple model providers behind a common adapter

## Quick start: native observation mode

Requirements: Python 3.11+ and a running Codex app-server Unix socket. No third-party runtime
packages or frontend build are required.

```sh
cd switchstand
PYTHONPATH=src python -m switchstand.service \
  --app-server-socket /path/to/codex-app-server.sock \
  --native-root-thread-id EXACT_NATIVE_ROOT_ID
```

Then open <http://127.0.0.1:4180/>. Switchstand never discovers or guesses the root. Omit
`--native-root-thread-id` to run the legacy two-role reliability spike; its state defaults to
`~/.local/state/switchstand/state.json`. Run
`PYTHONPATH=src python -m switchstand.service --help` for all options. Both modes are
loopback-only and run as the invoking user; no `sudo` or system service is required.

Run the dependency-free checks:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src tests
node --test tests/browser_focus.test.js tests/native_selection_contract.test.js
node --check src/switchstand/static/app.js
```

The Node commands are optional, cover the browser refresh regression, and check JavaScript
syntax; Node is not needed to run Switchstand.

Install the pinned development tools, then run the canonical code-quality gate:

```sh
python -m pip install -r requirements-dev.lock
npm ci --ignore-scripts --no-audit --no-fund
./scripts/quality
```

This runs Ruff, Pyright, duplication detection, and repository size ratchets. See
[Development](docs/development.md#code-quality-gate) for the exact policy.

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
- `src/switchstand/stage_a_evidence.py` — strict retained-evidence projection and validation
- `src/switchstand/stage_a_probe.py` — bounded collection orchestration and checkpoint CLI
- `src/switchstand/native_stop.py` — exact-turn native stop receipts and outcome projection
- `src/switchstand/native_selection.py` — pure closed `native-selection-v1` resolution boundary
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
- [Deterministic code-quality decision](docs/decisions/0003-code-quality.md)
- [Native read-only flight-board decision](docs/decisions/0004-native-read-only-flight-board.md)
- [Exact-turn native stop decision](docs/decisions/0005-exact-turn-native-stop.md)
- [Native selection v1 contract](docs/native-selection-v1.md)

Switchstand is available under the [MIT License](LICENSE).
