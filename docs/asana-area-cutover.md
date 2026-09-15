# Asana area cutover change request

Status: **review candidate only**. This document specifies the exact process/settings edits to make after the task-membership migration is verified. It does not authorize or perform any Asana mutation.

## Operating invariant

- Preserve the same Asana task GID and bound WorkId. Project migration changes membership/routing only; never clone, recreate, delete, or reparent work merely to migrate it.
- Exact task/WorkId identity wins over project placement.
- During a partial migration, each concern is either `CUTOVER` or `LEGACY` in the current area registry. `CUTOVER` concerns use their area project for current-work discovery. `LEGACY` concerns may still use the old root until their cutover is recorded.
- Old-root membership alone does not make a legacy review/message/checkpoint/canary/currentness artifact active work.
- After the final concern cuts over, project `1218210259719507` is Strategy & Programme plus legacy residue only, not a global current-work discovery surface.

## Exact Asana / Project Settings edits

### 1. Project Settings owner `1218221031538597`

Replace the current opening line:

```text
Canonical project: Asana project 1218210259719507.
```

with:

```text
Canonical Asana topology: current area registry 1218432271807843. Asana project 1218210259719507 is Strategy & Programme plus legacy residue; after an area's cutover it is not that area's current-work discovery surface.
```

Insert immediately after the opening authority paragraph:

```text
AREA CUTOVER / IDENTITY
Exact task/WorkId identity wins over project placement. Project membership routes work; it never replaces task identity or grants authority. Never clone, recreate, delete or reparent work merely to migrate it. During partial migration, follow the current area registry for each concern: CUTOVER uses the area project for current-work discovery; LEGACY may still use the old root until its cutover is recorded. Old-root membership alone does not make legacy residue current work.
```

No other Project Settings meaning changes in this cutover package.

### 2. Area registry `1218432271807843`

Replace the current migration-status / compatibility-root framing with this controlling header:

```text
STATUS
CURRENT MUTABLE PROCESS REFERENCE — AREA TOPOLOGY AND CUTOVER STATE. This records the current area registry and per-area discovery state. It grants no authority, locks or approval.

IDENTITY
One semantic work item keeps one exact Asana task GID and bound WorkId. Project membership is routing, not identity. Never clone/recreate/reparent work merely to migrate it.

CUTOVER SEMANTICS
Each concern is explicitly CUTOVER or LEGACY. CUTOVER means its area project is authoritative for current-work discovery. LEGACY means the old root may still be used for that concern until its cutover is recorded. Exact bound task/WorkId always wins. Legacy review/message/checkpoint/canary residue may remain in the old root without becoming current work.

FINAL STATE
After all concerns are CUTOVER, project 1218210259719507 is Strategy & Programme plus legacy residue only and is not a global current-work discovery surface.
```

Keep the eight-project registry and the durable same-GID / one-home / RELATED-only rules. Retire T0/T1/T2 and `MIGRATION / COMPATIBILITY` as migration history once they no longer govern live rows; do not delete their provenance.

### 3. START HERE `1218327002478382`

Add one route under `ROUTES`:

```text
- Asana area/project topology and partial-cutover state -> area registry 1218432271807843
```

Do not add a fallback instruction to scan old-v2 for current work.

### 4. Agent instruction architecture owner `1218348614603868`

Add this current-position block after migration execution/readback:

```text
AREA CUTOVER POSITION
Current Asana discovery follows area registry 1218432271807843. Exact task/WorkId remains primary. For each concern, CUTOVER uses its area project; LEGACY may still use the old root until recorded cutover. Old-v2 membership is not by itself evidence that a task is current. Same-GID identity is preserved; no clone/recreate/reparent migration semantics are permitted.
END AREA CUTOVER POSITION
```

This does not alter Human Review, reviewer independence, polling, message transport, canary semantics, Lifecycle ownership, or effect authority.

### 5. Process-loop owner `1218388417925266`

Add this discovery rule:

```text
AREA DISCOVERY
For current-work/review/message discovery, first use exact known IDs and bound WorkIds. Otherwise consult area registry 1218432271807843 and use the concern's current CUTOVER/LEGACY surface. Do not treat old-v2 as a global current-work index after partial cutover. Legacy children remaining there are residue unless their exact current owner/process says otherwise.
```

Existing exact message/review watches remain valid by ID across project moves.

## Final cutover declaration

When the migration manifest has been executed and read back, record one durable declaration using this meaning:

```text
AREA CUTOVER COMPLETE FOR RECORDED CONCERNS. Area projects marked CUTOVER in registry 1218432271807843 are authoritative for current-work discovery. Exact task GIDs/WorkIds are unchanged. Project 1218210259719507 remains authoritative only for Strategy & Programme and any concerns still explicitly LEGACY; residual review/message/checkpoint/canary history may remain there without blocking current operation.
```

When the final concern becomes CUTOVER, replace the last sentence with:

```text
Project 1218210259719507 is now Strategy & Programme plus legacy residue only and is no longer a global current-work discovery surface.
```

## Explicit non-changes

Do not redesign Human Review, Review Guidelines, roles, polling/continuity, message bus, canary architecture, Lifecycle, MCP authority, task hierarchy, or review-subtask topology as part of this cutover. Those require their own evidence/authority if changed.
