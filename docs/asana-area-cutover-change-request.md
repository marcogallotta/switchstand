# Asana area cutover change request

Status: review candidate only. This document proposes the exact process/settings edits for the Asana area migration. It makes no Asana change and grants no Asana write authority.

## Operating rule

- Task identity is the existing Asana task GID; bound WorkId identity is unchanged by project moves.
- Project membership is routing/discovery metadata, not identity or authority.
- Never clone, recreate or reparent a task merely to migrate it.
- Reviews, messages, checkpoints, canaries and similar structural children may remain as legacy residue; their old-project membership is not a current-work signal.

## Partial migration

The area registry owns one state per area:

- `LEGACY`: old root remains the authoritative discovery surface for that area's current work.
- `CUTOVER`: the area project is the authoritative discovery surface for that area's current work.

Exact assigned task/WorkId always wins. Source readability may span both surfaces during migration. A project's readability does not make it authoritative.

After the last area becomes `CUTOVER`, project `1218210259719507` is Strategy & Programme plus legacy residue only and is no longer the global current-work discovery surface.

## Exact Project Settings delta

Replace this line:

```text
Canonical project: Asana project 1218210259719507.
```

with:

```text
Canonical Asana topology: current area registry 1218432271807843. Exact assigned task/WorkId is primary. During partial migration, each area's registry state selects its authoritative discovery surface: CUTOVER -> its area project; LEGACY -> legacy root 1218210259719507. After final cutover, 1218210259719507 is Strategy & Programme plus legacy residue only, not global current-work discovery. Project membership routes/discovers work; it never replaces task GID/WorkId identity.
```

Add to `EVENT / MODE ROUTES`:

```text
* Asana area topology / migration discovery -> current area registry 1218432271807843.
```

No other Project Settings meaning changes are proposed by this cutover.

## Exact Agentic Docs / Asana change request

### Area registry `1218432271807843`

Replace the migration-root framing with:

```text
STATUS
CURRENT AREA TOPOLOGY REGISTRY. This registry selects the authoritative discovery surface for each Switchstand area. It routes; it does not grant authority.

CUTOVER SEMANTICS
Each area is exactly LEGACY or CUTOVER. LEGACY means current area work is still discovered through legacy root 1218210259719507. CUTOVER means current area work is discovered through that area's project. Exact task GID/WorkId remains unchanged and primary when already bound. Source readability may span both surfaces during migration; readability does not select authority.

FINAL STATE
When every area is CUTOVER, 1218210259719507 is Strategy & Programme plus legacy residue only. Legacy residue, review/message/checkpoint/canary children and historical evidence remaining there do not become current merely because they retain membership.

IDENTITY
Never clone, recreate or reparent work merely to migrate it. Preserve the same task GID and WorkId. Secondary cross-area visibility uses RELATED where applicable and never creates a second workflow-bearing home.
```

Retain the existing approved area project registry and durable one-home / RELATED / no-clone rules. Retire T0/T1/T2 and `MIGRATION / COMPATIBILITY` as operative discovery semantics once final cutover is declared; preserve them only as migration history/provenance where needed.

### START HERE `1218327002478382`

Add one route:

```text
- Asana area/project topology and migration state -> current area registry 1218432271807843
```

Do not use the legacy root as a generic current-work fallback after an area's CUTOVER state.

### Process/discovery owner `1218388417925266`

Add the following current discovery rule:

```text
AREA DISCOVERY
Exact known task/review/message IDs remain directly addressable. For unscoped current-work discovery, resolve the owning area and its current registry state. CUTOVER areas use their area project; LEGACY areas use the legacy root. Do not infer currentness from legacy-root membership alone. Legacy structural children may remain there without blocking cutover.
```

No message-bus, polling, acknowledgment or review-state semantics change.

### Instruction architecture owner `1218348614603868`

Record the cutover position only:

```text
ASANA AREA CUTOVER
Current entry/discovery follows exact task/WorkId first, then START HERE and the current area registry. The legacy root is not a universal operating surface after area cutover. This changes routing/discovery only; authority, Human Review, review independence, polling, message semantics and writable-surface rules are unchanged.
```

## Repository changes in this PR

- `AGENTS.md`: adds the stable same-GID/WorkId and partial-area-cutover invariant without hard-coding mutable area membership.
- `docs/source-history-feedback.md`: replaces the single-canonical-project source-read description with the approved mixed legacy/area source scope and separates readability from authoritative home/discovery.
- this file: exact reviewable Project Settings and Agentic Docs change request.

## Acceptance

The cutover is good enough when:

1. migrated task GIDs are unchanged;
2. each area has an explicit LEGACY/CUTOVER state;
3. CUTOVER areas no longer rely on old-root membership for current-work discovery;
4. exact source reads still work across the approved mixed project scope;
5. after all areas are CUTOVER, the old root is treated as Strategy/Programme + residue only;
6. no Human Review, message bus, polling, review independence or authority semantics changed accidentally.
