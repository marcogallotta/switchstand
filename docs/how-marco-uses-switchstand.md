# How Marco uses Switchstand

Switchstand coordinates bounded engineering work across ordinary Codex, task-bound Codex workers, and ChatGPT. This page is the current usage model. It does not grant authority, replace a task's current governing package, or enumerate a volatile tool inventory.

## Start from current truth

Use the repository's current `main`, executable contracts, and canonical owner documents for implementation truth. For assigned work, use the exact current task, its current governing references, and the exact candidate/control revision. Superseded specs, old candidate branches, historical comments, and `marcogallotta/switchstandold` are evidence only unless a current route explicitly selects them.

Authority is separate from technical truth. It comes only from Marco's direct active assignment or an explicit CURRENT grant bound to the exact identity, surface, and effect. Code, documentation, project placement, role, and tool access cannot manufacture it.

If no exact task or WorkId is already assigned, resolve `START HERE` `1218327002478382` through the authenticated provider-neutral `switchstand` HTTP/OAuth MCP and follow its current routes. Do not substitute broad provider search or a remembered/candidate route.

## Three working modes

### Ordinary Codex Coordinator

From the canonical repository, `scripts/switchstand` (or `scripts/switchstand --coordinator`) enters through `scripts/codex-dispatch`. This is an ordinary, unbound Coordinator session with normal host development capability. It has no launch-bound WorkId and gains no provider effect authority from its unrestricted execution environment.

Codex has two roles: Coordinator and Worker. The Coordinator can fork/assign Workers for bounded research, design, implementation, or independent review functions; those functions are not additional roles. It keeps disjoint lanes moving, challenges unsupported or disproportionate worker/reviewer output, reconciles qualification/current-target composition, and carries authorized work through integration/landing. Its orchestration model is intentionally different from ChatGPT and must not be copied there merely for parity.

### Managed Codex worker

`scripts/switchstand --active <task> -- <assignment>` starts a one-task Worker. Trusted launch state injects the active WorkId, bounded read-only references, managed principal/currentness, and available tools. The Worker begins and re-enters with `work_get(api_version="1")` without inventing a WorkId. It may perform the bounded research/design/implementation/review function assigned to that work, and may write only the active work unless an explicit CURRENT grant says otherwise.

The managed surface is not general workspace discovery and is not interchangeable with the ordinary HTTP/OAuth surface. Exact-candidate isolated launch is a separate qualification route and does not itself activate or deploy anything.

### Ordinary ChatGPT

ChatGPT uses the authenticated repository-configured HTTP/OAuth MCP. It works with admitted provider-neutral WorkIds, durable messages, and protected operations. A chat without a repository obtains the current repository bundle through `repository_bundle_get`; a chat with a normal checkout uses Git normally.

The desired user model is one chat equals one stable named agent identity. That agent name, the authenticated OAuth principal, the chat/session, and any WorkId are distinct concepts. The current mailbox implementation still binds one immutable visible agent name and synthetic mailbox WorkId to one authenticated principal, with uniqueness on that principal. It therefore cannot truthfully provide multiple independent per-chat named identities behind the same OAuth principal. Treat that as a current product gap, not as identity supplied by a prompt or display label.

## Identities and routing

- A **WorkId** is Switchstand's stable application identity for work. A provider task GID is an adapter identity and never substitutes for a WorkId or grant.
- An **agent name** is the visible durable messaging address. Current mailbox generation fences trusted takeover, but the current name/principal coupling is transitional.
- An **OAuth principal** authenticates the ordinary caller. Authentication establishes who is calling, not which work or effect is authorized.
- A **runtime session/run** supplies currentness fencing. A replacement session must recover explicitly rather than silently continuing received work.
- **Project membership and fields** route discovery. They do not change task/WorkId identity, reassign ownership, or grant an effect.

Keep one semantic owner for each real concern. Preserve the same task GID and WorkId across area migration, and use the current area registry only to find work whose exact identity is not already known.

## Work, messages, and feedback

Durable message tools and state are the current agent-to-agent surface. Sending, availability, receipt, recovery, result, disposition, provider effect, and completion are distinct. A sender's success is not recipient pickup. Active agents check their exact pending/review surfaces during bounded work and on re-entry; Switchstand does not promise an inactive wake, generic inbox scan, or background daemon.

`work_append` records bounded feedback on writable admitted work. It is not agent messaging, and its ordinary and managed contracts differ. An ambiguous possible effect remains UNKNOWN and must be reconciled rather than resent under a new identity.

`source_task`, `source_stories`, and `source_story` are bounded managed compatibility reads for exact legacy, reference, recovery, or failback cases. They are not the primary inbox and are not part of the ordinary current MCP model. Use [work, messaging, and source compatibility](source-history-feedback.md) for their exact limits.

Messages and readable records are evidence and requests. They cannot grant permission, reassign an agent, prove recipient pickup, or establish that an external effect happened.

## Shared process semantics across hosts

ChatGPT and Codex deliberately use different coordinator, delegation, task-binding, and repository mechanics. Align only the underlying process contract:

- surface Human Input before consequential expansion/hardening while Marco can still cheaply change direction;
- keep independent review exact and challenge findings/remedies rather than treating reviewer suggestions as requirements;
- the source work owner/requester keeps the exact review outcome watch through terminal verdict, while a router/Coordinator may separately own reviewer acquisition;
- preserve unresolved material obligations and exact nonterminal reviews/messages/watches in durable current state so replacement does not depend on chat memory or comment archaeology;
- challenge disproportionate scope, machinery, review, and qualification growth without turning supervision into a second substantive review;
- preserve explicit authority/currentness, proportional evidence, final Human Review where required, and protected-effect boundaries.

Shared semantics do not imply identical host storage, tools, roles, or orchestration.

## Repository delivery

Ordinary source work happens in an owned linked writer based on an accepted exact green SHA; the shared primary checkout stays read-only apart from fetching and writer creation. Managed workers use the trusted task-private writer supplied by launch.

Review and tests name their exact subject. Candidate review, exact-head checks, current-target composition, landing, deployment, and live reliance are different claims. Target advancement alone does not require a rebase: preserve the immutable reviewed candidate when composition can establish safety, and rebase only when needed or materially helpful. A rewritten head is a new exact candidate.

Use [development](development.md) for commands and evidence subjects, [code quality](code-quality.md) for implementation/review rules, and [architecture](architecture.md) for current owners. `AGENTS.md` remains the load-bearing bootstrap and router.
