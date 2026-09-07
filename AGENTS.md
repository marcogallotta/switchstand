# Agent routing

This file is the sole writable owner of shared agent operating rules.

- Start from the assigned active WorkId, bounded reference WorkIds, and an exact green repository SHA.
- Before launch, read and cross off the host-setup checklist in canonical Bootstrap owner `1218242783900077`;
  never guess an auth provider or treat a connector OAuth session as a controller credential.
- Read the active work and its governing references before material edits or child dispatch.
- One agent owns each writable surface. Children receive bounded objectives, files, tests, and stop conditions.
- Inspect a child after roughly a minute or when behavior looks suspicious; steer or stop scope drift.
- Only the active WorkId is writable. Reference WorkIds are read-only. Never expose provider IDs or credentials.
- Use only `work_get`, `work_update`, and `work_append`; raw provider access is outside agent authority.
- Reread before replacement writes, preserve stale-state failures, and read back successful effects.
- Never retry an append after an ambiguous response; record the outcome as unknown.
- Keep changes inside the assigned stage and file ownership. Shared config, migrations, CI, and guidance belong
  to the integration owner unless explicitly delegated.
- Run affected tests and the full quality gate before publication. Review AI-authored tests for the fault they catch.
- Stop on a new material authority, persistence, concurrency, security, or external-interface choice.
- Keep handwritten Bootstrap change at or below 1,000 lines; reforecast before crossing the current allowance.
- Treat `marcogallotta/switchstandold` as read-only evidence, never as an implementation base.
