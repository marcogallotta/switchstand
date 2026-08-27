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

### Code-quality gate

Install the exact development-tool versions from the committed locks:

```sh
python -m pip install -r requirements-dev.lock
npm ci --ignore-scripts --no-audit --no-fund
```

Then run the same command used by CI:

```sh
./scripts/quality
```

It requires Ruff 0.16.2, Pyright 1.1.411 in basic mode, and jscpd 4.2.5. It rejects Ruff
E9/F/B findings, Pyright diagnostics, and qualifying duplication of at least 10 lines and 80
tokens. New or newly oversized Python files cannot exceed 500 physical nonblank lines; legacy
files already above that threshold cannot grow. Nonignored files above 100,000 bytes warn and
those above 200,000 bytes fail. Gate, config, lock, and workflow changes require human review.

### Full verification

```sh
./scripts/quality
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src tests
node --test tests/browser_focus.test.js
node --test tests/native_selection_contract.test.js
node --check src/switchstand/static/app.js
```

The first two commands are required for every code change. Run the JavaScript syntax check when
Node is available and for every browser-code change. The Node regression is an honest fake-DOM
seam. `./scripts/quality` also rejects disabled, focused, or retried tests.

Run the two real local boundaries separately:

```sh
PYTHONPATH=src python tests/integration/app_server_transport_test.py -v
./node_modules/.bin/playwright install chromium
./node_modules/.bin/playwright test tests/browser/focus_chromium.spec.js
```

The transport test uses a temporary local Unix socket and a scripted WebSocket/JSON-RPC peer; it
does not launch Codex or use a network. The Chromium journey serves the production browser assets
on loopback, forces 50 one-second textarea-replacing refreshes, and verifies draft, focus, and
selection every time. Playwright is pinned, Chromium-only, one-worker, and zero-retry. Setup,
timeout, or assertion failure is nonzero; neither boundary retries to green.

## Run

Run the read-only native flight board for one exact root:

```sh
PYTHONPATH=src python -m switchstand.service \
  --app-server-socket /path/to/codex-app-server.sock \
  --native-root-thread-id EXACT_NATIVE_ROOT_ID \
  --port 4180
```

Omit `--native-root-thread-id` to run the default legacy reliability spike; that mode also
accepts `--workspace` and `--state`. Native mode never discovers a root or resumes a thread. Its
emergency stop requires active current board evidence, browser confirmation, JSON plus
`X-Switchstand-Control: native-stop-v1`, and same-origin loopback Host/Origin.

The socket must already be provided by a Codex app-server daemon. The service does not launch,
authenticate, or supervise that daemon. Native startup rejects non-loopback binding. Bind to the
default `127.0.0.1`; the prototype is not designed for network exposure. The native boundary
rejects simple/cross-origin requests and permissive CORS but does not authenticate same-user
local processes or browser automation.

Use a user-owned virtual environment, Unix socket, state/cache directory, and process. Do not
run Switchstand with `sudo`, install its dependencies system-wide, create root-owned project
files, bind privileged ports, or require a root-managed service.

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
  --subscribe-status-notifications \
  --notification-wait-seconds 30 \
  --require-status-notification \
  > stage-a-evidence-with-status-event.json
```

App Server does not subscribe a connection when it calls `thread/read` or `thread/list`.
`--subscribe-status-notifications` is therefore an explicit state-changing opt-in: the probe
calls `thread/resume` for only the exact root and descendants in its completed snapshot, then
fully re-reads and validates the same tree before waiting. Resume changes runtime loaded and
connection-subscription state but does not add conversation history or start a turn. The JSON
records this under `subscriptionEvidence`, sets `readOnly` to `false`, and keeps
`conversationHistoryMutated` false. Without this flag, default snapshot/polling mode never
resumes or loads a thread.

If subscription setup fails partway through, the failure JSON retains sanitized attempted and
exact-id-acknowledged counts. Any acknowledgement makes the runtime-state field `true`. A sent
request without an exact acknowledgement sets `mayHaveChanged` and reports runtime state as
`unknown`; it never claims the failed run stayed read-only. Raw attempted thread ids are omitted
from failure disclosure. Failures before the first resume attempt remain read-only.

A captured event appears only under `notificationEvidence.statusChanged`, with its local receive
time and whether its thread belonged to the observed tree. Snapshot facts remain under
`snapshots`. Both `--notification-wait-seconds` and `--require-status-notification` require the
explicit subscription flag; invalid combinations exit with usage error `2` before connecting.
The output never calls native `idle` done or silence stale. Exact native identifiers and cursor
values stay in memory: retained evidence uses consistent run-local thread/session references,
records only cursor presence, and counts unrelated-thread status events without retaining their
identifiers, statuses, or other details.

Exit `0` means all evidence requested by that invocation was observed. Exit `3` is a transport
failure, exit `4` is unavailable/incomplete evidence, and argparse usage errors exit `2`. Error
objects are JSON for runtime failures and omit the socket path. Their `code`, fixed `message`,
and allowlisted `phase` distinguish safe categories including invalid/missing roots or
descendant records with absent session evidence, selected non-roots, absent descendants,
invalid pagination, duplicate threads, missing parent edges or intermediate parents, lineage
cycles, invalid or unsupported native statuses, and missing protocol timestamps. They never
include protocol ids, paths, raw statuses or flags, cursors, prompts/outputs, or exception
strings. Successful evidence also projects source metadata through a fixed field allowlist:
unknown nested fields are dropped rather than copied, and an approved path field retains only
the constant `[redacted]`. Run the following for the complete flag reference; an editable
install also provides the `switchstand-stage-a` console command.

```sh
PYTHONPATH=src python -m switchstand.stage_a_probe --help
```

### Live evidence record

The Stage A gate passed on exact PR4 head
`8670f50b629ae3f201d5eed3aa04fc92afa9888b` and PR4 was merged. The live run observed one root,
one spawned descendant, and `active` to `idle` status notifications; it exited 0 with `ok: true`
and no conversation-history mutation. Its accompanying checks were 44 Python tests, compilation,
one browser test, and JavaScript syntax. This record proves that exact PR4 head only; it is not a
claim that the later main head was freshly exercised against a live App Server.

After a protocol-affecting change, use the same exact-head gate against the reviewed checkout:

```sh
PYTHONPATH=src python -m switchstand.stage_a_probe \
  --app-server-socket "$PWD/.switchstand/codex.sock" \
  --root-thread-id EXACT_ROOT_THREAD_ID \
  --subscribe-status-notifications \
  --notification-wait-seconds 30 \
  --require-status-notification \
  > stage-a-evidence.json
test "$?" -eq 0
```

Retain generated evidence only when it came from the exact reviewed head and exits zero. Do not
substitute the PR4 result or fixtures for a later exact-head claim.
