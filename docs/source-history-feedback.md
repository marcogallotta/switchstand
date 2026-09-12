# Source, history and feedback

This first capability exposes `work_get`, `source_task`, `source_stories`,
`source_story` and `work_append`. It uses the existing state and Asana adapter;
it adds no search, task discovery, assignment, generic update or event store.

## Read the assignment and its evidence

Call `work_get(api_version="1")` for the launch-bound assignment. `item.id` is
the opaque WorkId. `item.source.provider` and `item.source.task_gid` identify
the underlying task. A source task GID is never a WorkId and never grants work
or write authority. WorkId reads remain limited to the active work and the
launch-bound references.

For an exact Asana task already identified by the assignment or its evidence,
call `source_task(api_version="1", task_gid=...)`. Source reads accept explicit
Asana GIDs and require canonical Switchstand project membership, directly or
through the existing bounded ancestor walk. Reading a source does not bind it
as active work. Requests outside that canonical boundary return `denied`.

Use the returned revision for `source_stories(api_version="1", task_gid=...,
observed_revision=..., limit=50)`. Each call reads at most 100 stories. Pass
only the returned `next_offset` into the next call for the same task/revision;
`next_offset=null` ends pagination. A changed task revision returns `stale`
with no stories: reread the task and restart the affected history read.
Malformed pages, mismatched targets or unusable cursors never mean complete
history. An expired provider cursor is an error; restart from a fresh read.

Task revision guards bracket each history page, but they are not an atomic
snapshot of all comment text. Before relying on material comments in a final
finding or decision, call `source_story(api_version="1", task_gid=...,
story_gid=..., observed_revision=...)` for each exact story used, and compare
the current content. Edited comments may not advance the task revision.
Changed, deleted, unavailable or retargeted evidence reopens the affected
reasoning. Reread the owner/task too. Do not infer approval from readability.

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

`scripts/switchstand-start --active <task>` now supplies Codex with an initial
request to read its bound work and follow the repository's Active inbox routine.
An optional caller prompt is preserved inside that same initial request. No
extra prompt from Marco is needed to begin inbox loading. The active source
task's comments are the shared ChatGPT/Codex message surface; other source tasks
remain read-only evidence. ChatGPT sends scoped messages there through its Asana
connector and reads Codex's receipts/results from the same task.

The routine checks comments during active work and reconstructs handled/pending
messages on re-entry, using the existing five tools. It critically checks incoming
requests and never treats message delivery as authority. Polling is instruction-led:
this change adds no daemon, enforced timer, inactive-session wake or remote launcher.

The existing [message canary](https://app.asana.com/0/0/1218432166274128/f)
must establish actual model follow-through separately from command-assembly tests:

1. On an authorized real task, leave a relevant inbound comment, then start managed
   Codex without a custom prompt. Observe automatic assignment/history loading and
   a receipt/disposition naming that exact message, without a manual relay.
2. During ordinary authorized work, send a second scoped message from ChatGPT to
   that inbox. Observe pickup, authority checking and exact feedback readback.
   Include an unrelated/unauthorized request and verify no unrelated effect occurs.
3. Re-enter the same assignment. Verify completed messages do not repeat effects,
   pending messages remain visible and an ambiguous result is reconciled rather
   than retried. Record the actual run/head, input and result story IDs and outcome.

Code presence and passing tests do not graduate this canary. An inactive run
still needs an actual start; a completed assignment does not listen in the background.

## First real use

Automated checks and fresh independent review establish code confidence.
They do not establish provider qualification or a live Codex run.

After the exact candidate is landed and callable on the approved Codex
execution surface, use the existing capability canary on real work:

1. Read the bound assignment; verify its source task identity with an exact
   task read. Read a needed source/history page and reread a material story.
2. Append one authorized, bounded feedback result to that active work and
   verify the exact returned story and task through the source tools.
3. Have the owner ingest the result. Record the capability's observed behavior,
   consequence, smallest correction and exact evidence on the existing canary.

Keep useful lower capabilities in use. Make the smallest demonstrated fix
before building on top; expand Codex's authoring/testing/delivery role only
when the corresponding capability has been exercised successfully.

Provider semantics: [task revisions](https://developers.asana.com/reference/tasks),
[paginated stories](https://developers.asana.com/reference/getstoriesfortask),
and [exact story reads](https://developers.asana.com/reference/getstory).
