# Switchstand

Switchstand is an experimental local operator surface for Codex work. Its explicitly selected
native mode observes one real Codex root and its descendants, lets the operator select one exact
current agent for direct input, and can request cancellation of one exact active turn after
explicit confirmation.
The default legacy mode remains a fixed two-role reliability spike with durable messages,
checkpoints, attempt controls, and conservative failure handling.

This repository is a bounded supervised-agent infrastructure build, not a throwaway
prototype or production orchestrator. It keeps two slices easy to inspect: truthful native-tree
observation with exact current-target input and one emergency control, and the legacy flat-file
reliability mechanisms for ordered messages, restart reconciliation, controls, and result
fencing.

## Status and checkpoint

The dependency-free engine, service, browser UI, and Codex Unix-socket adapter are covered by
local tests. PR #37 passed the exact-head live coordinator-worker journey and was merged as
`c593a3479033b09712874b4898a08b33e92d7a7e`: one exact spawned descendant, content-free exact
turn observation, independent native thread/turn status, and one exact Stop that reconciled an
ambiguous acknowledgement to confirmed/interrupted without retry or retarget. The reviewed head
also passed Quality run #60, 133 Python tests, AF_UNIX suites, Node/JavaScript checks, and 11 real
Chromium journeys.

Issue #9 adds Stage B1 as an explicitly selected native view. It polls one exact root with
`thread/read(includeTurns=false)` and all descendants with paginated
`thread/list(useStateDbOnly=true)`. It displays native lineage, native status, observer
freshness, consecutive observed-active time, and the latest 50 endpoint differences. It does not resume or
subscribe to threads or mutate conversation history. The legacy two-role engine remains
available as a reliability spike. The native operator candidate composes the current board,
safe browser selection, exact start-or-steer input, and one emergency control through one closed
HTTP dispatcher. Stop uses explicit two-step confirmation for one exact active turn. It does
not undo work or stop background processes or descendants. The live PR #37 run also showed a
current App Server limitation: direct `turn/steer` to the active spawned v2 worker was rejected.
Switchstand reported `not_sent` and did not retry, fall back, or retarget; accepted active-worker
steering is not currently claimed.

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
  --native-root-thread-id EXACT_NATIVE_ROOT_ID \
  --port 0
```

Open the exact URL printed by the process. Port `0` asks the operating system for a free
loopback port; an explicit port such as `--port 4180` also works. Switchstand never discovers
or guesses the root. Omit
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
- `src/switchstand/native_http.py` — closed native HTTP routing and response contract
- `src/switchstand/native_workbench.py` — in-process facade over the native ports
- `src/switchstand/service.py` — local HTTP/API/static-file process
- `src/switchstand/static/` — vanilla HTML, CSS, and JavaScript operator UI
- `scripts/stage_b1_live_check.py` — exact-head, read-only native-board live evidence runner
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
