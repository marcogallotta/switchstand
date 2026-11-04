# Tracker implementation

Tracker owns `src/switchstand/tracker/`: canonical work/relations/events and protected command
gateways. Start here when changing current work, its structure, or protected work effects. This describes
repository wiring, not the deployed revision or permission to operate a service. For agent use of
work tools and recovery, follow [source compatibility](source-history-feedback.md).

## Follow a request

The ordinary HTTP surface is `chatgpt_edge.py` → `chatgpt_mcp.py::build_ordinary_tools` →
`chatgpt.py::ChatGPTService`. The edge authenticates and wires dependencies; the tool wrappers
validate public arguments and obtain the admission grant version. The service resolves the
principal and read authority, then selects canonical owners when `canonical_work_active` is true.
The ordinary edge enables that branch; a direct service constructor defaults it off. Missing
canonical dependencies fail closed. Provider gateways are alternate branches, not additional
Asana writes on a canonical request.

| Change | Owner under `src/switchstand/` |
| --- | --- |
| Current work create/get/search/scalar update and work-item projection | [`tracker/runtime.py::CanonicalWorkRuntime`](../src/switchstand/tracker/runtime.py) (`protected_create`, `get`, `search`, `protected_update`, `_item`) |
| Current row storage, locking, optimistic version and legacy aliases | [`tracker/work.py::CanonicalWorkRepository`](../src/switchstand/tracker/work.py); `canonical_revision` produces the opaque `pg_` revision from WorkId and row version |
| Resultant routing/completion rules | [`tracker/policy.py`](../src/switchstand/tracker/policy.py), called before protected mutation; reuse `validate_resultant_state` rather than adding caller-specific policy |
| Parents, dependencies and project placements | [`tracker/relations.py`](../src/switchstand/tracker/relations.py); protected work relations enter through `CanonicalWorkRuntime.protected_relation` |
| Append/history/event storage and projection | `tracker/appends.py::CanonicalAppendGateway`, `tracker/events.py`, `tracker/event_reads.py` |
| Public request/result shape and scalar patch validation | Shared `contracts.py` and `grants.py`; public note preview is [`chatgpt_mcp.py::project_ordinary_work`](../src/switchstand/chatgpt_mcp.py) → `mcp.py::project_work` |
| Admission, grants and effect journal schema | Shared `workspace_admission.py`, `grant_state.py` and `grants.py`; identity and mailbox registration remain outside Tracker |

Managed launches use `mcp.py` and `core.py::Controller` with launch-bound authority, not ordinary
HTTP admission. `mcp.py::_CanonicalController` projects canonical reads. Inspect the actual
surface's constructor/callback wiring before changing an operation; shared contracts do not
make every tool available on every surface.

## Keep protected effects atomic

For a scalar update, `ChatGPTService.update` calls `CanonicalWorkRuntime.protected_update`.
That method holds the grant context and opens one `grants.engine.begin()` transaction. The
locked current row, `replace_locked` row-version increment, resulting-revision receipt and
`effect_intents` insert share the same connection. The successful return escapes only after
commit. Create and relation operations likewise journal their canonical effects atomically.
The repository is storage, not an authority gateway: direct `replace`/`replace_locked` does not provide protected grant/replay guarantees.
Use the protected boundary for ordinary writes.

| Scalar update result | Meaning and recovery |
| --- | --- |
| `stale` / `not_sent` / `refresh`, `source_revision_changed` | A fresh operation with an old observed revision did not write. Refresh current work and reconcile intent. |
| `denied` / `not_sent` / `none`, `operation_identity_conflict` | The identity was already used with a different principal or fingerprint. The original effect remains as recorded. |
| `unknown` / `unknown` / `reconcile`, `state_or_effect_unavailable` | Do not infer commit or rollback. Preserve the exact original identity and arguments for reconciliation/retry. |

The journal lookup precedes grant/revision validation on an exact replay. Matching principal
and fingerprint returns the recorded outcome without another version increment. The scalar
fingerprint includes WorkId, principal, grant version, observed revision and supplied patch.
A proven precommit failure rolls back both row and journal, allowing the exact request to apply
once on retry; an arbitrary SQL/commit failure still reports UNKNOWN conservatively. Keep public
status, effect, retry and receipt semantics intact. Pending-review confirmation and unresolved
effect fences also belong in the protected owner, not just in a tool wrapper.

## Find useful evidence

`tests/test_canonical_service_runtime.py` uses real disposable PostgreSQL at the service boundary:
look for `test_atomic_create_replays_and_persists_parent_without_provider`,
`test_atomic_update_replays_and_rejects_operation_conflict`,
`test_stale_and_ungranted_updates_do_not_write_or_journal`, and
`test_effect_journal_failure_rolls_back_work_and_exact_retry_is_safe`.
The last injects a specific precommit fault; it does not settle every real UNKNOWN outcome.
`test_canonical_work.py`, `test_canonical_relations.py`, `test_canonical_work_runtime.py` and
`test_canonical_event_reads.py` cover storage, revisions and projections. `test_chatgpt_mcp.py`
covers public schemas, admission forwarding and note previews.

For real HTTP/subprocess persistence, replay and restart evidence, use
[`tests/test_chatgpt_edge_process.py`](../tests/test_chatgpt_edge_process.py)
(`test_simple_completion_over_http_and_restart`); these local tests use synthetic authenticated identity,
not real ChatGPT/OAuth acceptance. Managed Controller/service checks are separate boundaries.
Use the disposable-database [development workflow](development.md#normal-local-workflow), select
checks for the changed boundary, and record the exact candidate and untested limits. Migration,
deployment and live reliance need their own authority and qualification.
