# Architecture

Switchstand keeps provider identity behind stable WorkIds. `PostgresState` owns WorkId and opaque event bindings;
`AsanaProvider` owns Asana HTTP/provider semantics; controllers and application gateways consume those
provider-neutral bindings rather than exposing provider credentials to agents.

## Durable state and authority

PostgreSQL is not a single-table store. Current durable application state includes:

- `work_handles` and `work_event_handles` for provider-neutral work/event identity;
- `work_grants` and `effect_intents` for current caller authority and durable protected-effect
  reconciliation;
- `messages`, `message_deliveries` and `message_projection` for durable agent-message identity,
  receipt and provider projection state;
- `lifecycle_obligations` for required-result continuation/persistence state.

`WorkGrant` is the authority contract. Provider writes are mediated by bounded gateways that require current
principal/grant/target authority, preserve one operation identity across retries, record possible sends before external
effects, and require authoritative readback before claiming an applied result. UNKNOWN is a durable recovery state, not
permission to resend with a new identity.

## MCP surfaces

There are multiple deliberately different MCP surfaces; there is no three-tool global MCP contract.

**Authenticated ChatGPT HTTP MCP** (`chatgpt_edge.py`) exposes the workspace/grant-aware ordinary surface:
provider-neutral work discovery/read/structure/history/attachment/event operations, protected append/create/update,
durable message send/pending, and required-result saving. legacy task references are resolved through `work_resolve_reference`; history and exact-event rereads remain
provider-neutral through `work_history` and `work_event`. Raw `source_*` methods remain internal provider/service
implementation details only and are no longer agent-facing tools.

**Managed task-bound STDIO MCP** (`mcp.py::build_server`) binds one active WorkId plus bounded references from
trusted launch state. It exposes launch-bound work/history/attachment/event reads, the transitional exact-source reads,
bounded append/update where configured, and managed message pending/receive/recover/result/disposition operations.
Launch-bound active/reference WorkIds are read with `work_get`, `work_history`, and `work_event`; raw provider task
or story GIDs are not a managed public interface. The unbound server exposes no managed task authority. The smaller
context server exposes only launch-bound `work_get` and `work_history`.

**Development MCP** (`development.py`) is separate from product work authority. Its bound surface is
`check`, `commit_all_current_worktree`, `quality` and `run_status`, operating only on the
exact linked writer/run identity supplied by trusted launch state.

## Development and launch control

Managed launch is orchestration, but current ownership is still broader than the desired end state:
`launch.py` validates the linked writer, prepares the candidate development image/network/database, performs
Codex App Server configuration/readback, supervises the launched process and cleans up its prepared resources.
`development.py` owns focused/full workload execution and exact workload-container cleanup, while `docker.py`
provides the shared exact-owned Docker inspection/name/label/removal primitives. This overlap is current repository
truth and remains a cleanup boundary; do not infer that Docker/runtime ownership has already fully converged.

The isolated-launch path still has a host-side `launch_source.py` seam that performs protected Asana source
reads and Git remote/ref verification before candidate materialization. That path is transitional current behavior, not
a second provider architecture to copy into new code.

Git landing reconciliation follows the repository's GitHub merge-commit model: the landing must be the current result
with exactly the reviewed base and reviewed candidate as its two ordered parents, the reviewed candidate must descend
from that base, and the landing tree must equal the reviewed candidate tree.

## Transitional compatibility

- raw `source_*` agent tools are retired; internal provider source reads remain behind provider-neutral WorkId
  history/event APIs;
- `scripts/switchstand-context` and `scripts/switchstand-start` are compatibility wrappers around current
  entry paths;
- provider IDs and credentials belong inside trusted provider/launch adapters, not normal agent-facing authority;
- the retired `switchstandold` tree is evidence only and is not an architectural ancestor.
