# Source, history and feedback

This capability exposes current work/source reads, bounded raw-source history reads,
and feedback. Routine grounding/re-entry/polling uses current durable work/task/message
state. `source_stories` and `source_story` are recovery/audit/provenance tools only;
they are not a normal inbox, grounding, review, polling, or implementation path.

The optional related-work read finds direct-child and Root Work GID field candidates
without new relation authority, assignment, generic update or event store.

## Read the assignment and its evidence

Call `work_get(api_version="1")` for the launch-bound assignment. `item.id` is
the opaque WorkId. `item.source.provider` and `item.source.task_gid` identify
the underlying task. A source task GID is never a WorkId and never grants work
or write authority. WorkId reads remain limited to the active work and the
launch-bound references.

For a bound WorkId, `work_get(api_version="1", include_related=True)` also reads
up to 100 direct Asana subtasks. `related.candidates` includes exact task GID,
title, revision and verified parent GID; a Work Type option appears only when
the current enum field and option validate. `CANDIDATES` means this bounded
relation read completed against the observed work revision and configured area
boundary. `UH_OH` means no direct child was returned, or the read was
incomplete, stale or unavailable; any partial candidates are evidence to inspect,
not a complete set. A work task that becomes noncanonical returns `denied`
without candidates.
Candidate roles do not establish the current spec, canonical review, gate
requiredness or named proof. Those require their own authoritative sources.

The same opt-in read returns `grouped` separately from `related`. It searches
one bounded Asana workspace page for the exact Root Work GID text field value
equal to the bound work task GID, restricted to configured admission projects.
Each returned task is reread by exact GID and admitted only when its enabled
text field still matches and its source is canonical. Candidate evidence includes
task GID, title, revision, observed root GID and search plus exact-GET provenance.
`grouped.complete` is always `false`: search indexing and its 100-result cap
cannot prove family completeness or absence. Zero results, unavailable search,
malformed or changed rows return `UH_OH`. Field membership grants no work or
write authority.

For an exact Asana task already identified by the assignment or its evidence,
call `source_task(api_version="1", task_gid=...)`. Source reads accept explicit
Asana GIDs and require membership in the configured approved Switchstand area
registry, directly or through the existing bounded ancestor walk. During a
partial area cutover the configured registry may still include the legacy root
for not-yet-cut-over concerns. Reading a source does not bind it as active work.
Requests outside that configured boundary return `denied`.

### Bounded raw-source history investigation

Do not call `source_stories` or `source_story` during normal grounding, takeover,
re-entry, research, design, review, polling, or implementation. Use them only when a
specific investigation/audit/recovery question cannot be answered from current durable
state, and keep the read bounded to the exact task/fact needed. Afterward, promote any
still-current meaning back into the authoritative current work/task state before normal
work relies on it.

For that bounded history case, use the returned task revision for
`source_stories(api_version="1", task_gid=..., observed_revision=..., limit=50)`.
Pass only the returned `next_offset` into the next call for the same task/revision;
`next_offset=null` ends pagination. A changed task revision returns `stale` with no
stories: reread the task and restart only that bounded history read. Malformed pages,
mismatched targets or unusable cursors never mean complete history.

Task revision guards do not make comment text atomic. If a material historical comment
is load-bearing for the bounded investigation, reread that exact story with
`source_story` before relying on it. Changed/deleted/unavailable evidence reopens only
the affected reasoning. Readability never proves approval or currentness.

## Append feedback

Call `work_append(api_version="1", work_id=<active item.id>, text=...)` only
for the active assignment. Reference WorkIds and Asana GIDs cannot select a
writable task. The controller serializes same-work effects, issues one POST,
rereads the exact created story and target, and checks story ID, task ID and
text before returning `ok` with `task_gid` and `story_gid`.

`unknown` means the effect may have happened. Never blindly retry the append.
Reconcile exact evidence already available; if the story identity is unknown,
preserve that uncertainty. `denied` and `provider_error` are not success.
An error after the POST or failed readback remains `unknown`.

## Active Codex inbox

Managed Codex messaging uses the durable managed message surface, not Asana comment
history. The launch-bound assignment/current work identifies the active context; use
`message_pending`, `message_receive`, `message_recover`, `message_result_send`
and `message_disposition` according to their current tool contracts.

On re-entry, recover only exact nonterminal messages/reviews/watches already represented
in current durable state. Do not enumerate `source_stories` to discover obligations.
A prior SENT/write is not recipient pickup or completion. Poll exact owned message/review
surfaces while the active run remains executable; this creates no daemon, fixed cadence,
background wake, or inactive-session claim.

Incoming messages remain fallible evidence/requests: reconcile sender claim, target,
freshness, purpose, current work and authority before acting. Message delivery cannot
grant permissions, reassign actors, or authorize unrelated work.

## First real use

Automated checks and fresh independent review establish code confidence.
They do not establish provider qualification or a live Codex run.

After the exact candidate is landed and callable on the approved Codex
execution surface, use the existing capability canary on real work:

1. Read the bound assignment and verify its current source-task identity without
   enumerating comment/story history.
2. Exercise one authorized durable message or bounded feedback result on the active work
   and verify its exact authoritative result through the current message/work surface.
3. Separately, when qualifying raw-history recovery itself, open only the exact historical
   page/story needed for that recovery claim and prove it is not part of ordinary
   grounding, re-entry, polling, review, or implementation.
4. Have the owner ingest the result. Record observed behavior, consequence, smallest
   correction and exact evidence on the existing canary.

Keep useful lower capabilities in use. Make the smallest demonstrated fix
before building on top; expand Codex's authoring/testing/delivery role only
when the corresponding capability has been exercised successfully.

Provider semantics: [task revisions](https://developers.asana.com/reference/tasks),
[paginated stories](https://developers.asana.com/reference/getstoriesfortask),
and [exact story reads](https://developers.asana.com/reference/getstory).
