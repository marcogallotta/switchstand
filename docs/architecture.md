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
durable message send/pending, and required-result saving. The repository Codex config explicitly allowlists this
ordinary inventory; edge registration and that allowlist must remain synchronized. `source_task`, `source_stories` and
`source_story` remain public transitional raw-Asana reads for legacy recovery/reference workflows; they are not
the preferred provider-neutral product vocabulary.

**Managed task-bound STDIO MCP** (`mcp.py::build_server`) binds one active WorkId plus bounded references from
trusted launch state. It exposes launch-bound work/history/attachment/event reads, the transitional exact-source reads,
bounded append/update where configured, and managed message pending/receive/recover/result/disposition operations.
The unbound server exposes no managed task authority. The smaller context server exposes only launch-bound
`work_get` and `work_history`.

**Development MCP** (`development.py`) is separate from product work authority. Its bound surface is
`check`, `commit_all_current_worktree`, `quality` and `run_status`, operating only on the
exact linked writer/run identity supplied by trusted launch state.

## Development and launch control

Managed launch is orchestration. `launch.py` validates the linked writer, sequences candidate
preflight/development preparation, supervises the launched process, and requests exact cleanup. `codex_runtime.py`
owns Codex CLI/App Server command construction, configuration/readback, and runtime profile validation.
`development.py` owns development image/network/database preparation plus focused/full workload mechanics and exact
owned cleanup; `docker.py` provides shared low-level Docker inspection/name/label/removal primitives. Launch still
decides when those lifecycle operations occur, so higher-level lifecycle-policy convergence remains a separate cleanup
boundary rather than a claim that ownership is fully finished.

The isolated-launch path keeps a host-side `launch_source.py` seam for protected task-source parsing/readback.
Exact Git repository/ref verification and materialization are delegated to `candidate.py::prepare_launch_source`
before launch preflight. That bridge is transitional current behavior, not a second provider architecture to copy into
new code.

Git landing reconciliation follows the repository's GitHub merge-commit model: the landing must be the current result
with exactly the reviewed base and reviewed candidate as its two ordered parents, the reviewed candidate must descend
from that base, and the landing tree must equal the reviewed candidate tree.

## Transitional compatibility

- raw `source_*` agent tools remain only until required ordinary/recovery/failback consumers have verified
  provider-neutral replacements and outstanding legacy references are drained;
- `scripts/switchstand-context` and `scripts/switchstand-start` are compatibility wrappers around current
  entry paths;
- provider IDs and credentials belong inside trusted provider/launch adapters, not normal agent-facing authority;
- the retired `switchstandold` tree is evidence only and is not an architectural ancestor.
