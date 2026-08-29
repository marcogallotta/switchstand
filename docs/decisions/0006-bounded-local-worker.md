# ADR 0006: Keep local Codex execution behind the frozen worker protocol

- Status: Accepted
- Date: 2026-08-29
- Authority: Asana task 1217968676811774 and reviewed worker-v2-g3 contract

## Context

ChatGPT can coordinate short bounded requests, but it is not a reliable transport for generated
file bytes or a durable process supervisor. A coordinator therefore needs a finite external Codex
worker without granting that worker publication authority or exposing host credentials to model
processes. Earlier host qualifications proved selective bubblewrap isolation, bounded checkout
delivery, candidate manifests, lease fencing, real process-group cleanup, and the initial Codex
thread-creation crash gap.

## Decision

Add a separately packaged local worker that consumes exactly `worker-v2`. PostgreSQL-backed
coordinator state remains external. The worker claims immutable admitted authority, renews its
15-second lease independently every second, and kills the Codex process group after stale
authority, cancellation, two unavailable renewals, or three seconds without renewal success.

Before task work, it validates and atomically materializes one coordinator-supplied frozen
checkout. A provisional Codex thread receives only `Respond exactly READY.`; the worker adopts its
identifier only after the bootstrap finishes and complete `thread/list(useStateDbOnly=true)`
pagination finds it. Later work resumes only that exact adopted thread. Failure to resume before a
candidate returns scope rather than starting a replacement. A reclaimed accepted candidate skips
Codex and completes only that exact candidate.

The bubblewrap child sees its workspace, isolated provider state, required read-only runtime and
Codex authentication, and network. It receives no coordinator or GitHub authority. Verified Git
changes become a canonical bounded UTF-8 manifest with exact bytes, hashes, deletions, base SHA,
and checks. Candidate acceptance is inert; the coordinator alone owns publication.

## Consequences

The worker is useful with a deterministic local coordinator fixture before PostgreSQL g5 lands,
but fixture evidence is not database integration evidence. Provider state remains a user-owned
host dependency for exact resume. The worker adds no UI, database, hosted service, GitHub client,
service manager, root process, arbitrary command surface, or general orchestration framework.
