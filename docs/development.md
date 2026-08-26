# Development

## Setup

Use Python 3.11 or newer. Runtime and tests use only the standard library.

```sh
cd switchstand
python --version
```

Optional editable installation (this may ask the packaging tool for build dependencies):

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Installation is not required. The canonical dependency-free commands use `PYTHONPATH=src`.

## Test and static checks

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src tests
node --test tests/browser_focus.test.js
node --check src/switchstand/static/app.js
```

The first two commands are required for every code change. Run the JavaScript syntax check when
Node is available and for every browser-code change. The browser regression uses Node's built-in
test runner and a minimal DOM fixture, so it adds no package-manager or framework dependency.

## Run

```sh
PYTHONPATH=src python -m switchstand.service \
  --app-server-socket /path/to/codex-app-server.sock \
  --workspace "$PWD" \
  --state ./state/state.json \
  --port 4180
```

The socket must already be provided by a Codex app-server daemon. The service does not launch,
authenticate, or supervise that daemon. Bind to the default `127.0.0.1`; the prototype has no
authentication or CSRF protection and is not designed for network exposure.

## Review expectations

Review engine changes as state-machine changes. Check exact target validation, generation and
attempt fencing, transition persistence before external calls, replay behavior after ambiguous
acknowledgements, FIFO delivery, and preservation of stale/unknown evidence. Tests should cover
both the expected transition and the unsafe alternative that must not occur.

Review adapter changes against the concrete Codex method names and request fields. A mock-backed
protocol test proves request construction only. Report a live checkpoint only when a real Unix
socket was used and the observed thread/turn evidence is recorded; otherwise state explicitly
that it was not run.

Lost-acknowledgement tests must use the documented app-server v2 history shape:
`userMessage.content` with `text` entries. Do not recover from private correlation fields. Retry
is permitted only for complete history, an absent durable message marker, and exact idle status.

Update documentation when behavior, constraints, commands, state fields, or the prototype
boundary changes.

## Native agent-tree checkpoint

The tests under `tests/test_agent_tree.py` and `tests/fixtures/app_server/` prove request
construction and fail-closed handling of documented protocol shapes only. They are not a live
checkpoint. A Stage A claim requires a real App Server socket and recorded evidence for:

- one exact root plus at least one spawned descendant with `parentThreadId` lineage;
- explicit coverage of every root/subagent/unknown source kind and `nextCursor` exhaustion;
- native `active`, `idle`, `systemError`, and `notLoaded` status where exercised, plus actual
  `thread/status/changed` events (without calling idle done); and
- exact idle `turn/start`, active `turn/steer(expectedTurnId)`, and `turn/interrupt` behavior.

If the socket, root/descendant tree, ancestry, pagination, or state evidence is unavailable,
record the gap and stop. Do not replace the browser surface or synthesize missing evidence.

### Run the native Stage A probe

The probe requires an exact root id. It intentionally has no auto-selection or heuristic root
discovery. This one-shot command prints redacted JSON and exits nonzero unless the root, at
least one spawned descendant, complete `parentThreadId` lineage, every source-kind filter on
every exhausted page, native sources/statuses, protocol timestamps, and a local observation
window are all present:

```sh
PYTHONPATH=src python -m switchstand.stage_a_probe \
  --app-server-socket /path/to/codex-app-server.sock \
  --root-thread-id EXACT_ROOT_THREAD_ID \
  > stage-a-evidence.json
```

For multiple independent complete snapshots, add `--poll-count 3` and
`--poll-interval-seconds 1`. These remain polling evidence. They are not status-change
notifications.

To require an actually received `thread/status/changed` event for a thread in the observed
tree, keep that tree active while running a bounded notification window:

```sh
PYTHONPATH=src python -m switchstand.stage_a_probe \
  --app-server-socket /path/to/codex-app-server.sock \
  --root-thread-id EXACT_ROOT_THREAD_ID \
  --notification-wait-seconds 30 \
  --require-status-notification \
  > stage-a-evidence-with-status-event.json
```

The initialized App Server connection supplies notifications; no explicit subscription RPC is
invented or claimed. The probe does not resume/load a thread to provoke an event. A captured
event appears only under `notificationEvidence.statusChanged`, with its local receive time and
whether its thread belonged to an observed snapshot. Snapshot facts remain under `snapshots`.
The output never calls native `idle` done or silence stale.

Exit `0` means all evidence requested by that invocation was observed. Exit `3` is a transport
failure, exit `4` is unavailable/incomplete evidence, and argparse usage errors exit `2`. Error
objects are JSON for runtime failures and omit the socket path. Run the following for the
complete flag reference; an editable install also provides the `switchstand-stage-a` console
command.

```sh
PYTHONPATH=src python -m switchstand.stage_a_probe --help
```

### 2026-08-26 blocked live attempt

In the current Work workspace, Codex `0.150.0-alpha.10` successfully generated the
experimental TypeScript protocol schema, but no real checkpoint was possible. No daemon socket
was exposed. Starting a Unix listener and the stdio initialize path exited with
`Codex executable path is not configured`; the daemon socket path was unavailable with
`Operation not permitted`. Consequently, no live root/descendant payload or status transition
was observed. Stage A did not pass and Stage B was not started.
