# Agent routing

This file is the sole writable owner of shared agent operating rules.

- Start by calling `work_get` without a WorkId, then read its advertised bounded references and verify the exact
  green repository SHA.
- Use `~/.config/switchstand/.env` for setup. Ask once for any missing value, write it there, and reuse it automatically.
- Asana REST uses `ASANA_TOKEN` from that file. Any OAuth layer is GitHub-only; never use Asana OAuth.
- Read the active work and its governing references before material edits or child dispatch.
- One agent owns each writable surface. Children receive bounded objectives, files, tests, and stop conditions.
- Inspect a child after roughly a minute or when behavior looks suspicious; steer or stop scope drift.
- Only the active WorkId is writable. Reference WorkIds are read-only.
- Use only `work_get`, `work_update`, and `work_append`; raw provider access is outside agent authority.
- Reread before replacement writes, preserve stale-state failures, and read back successful effects.
- Never retry an append after an ambiguous response; record the outcome as unknown.
- Keep changes inside the assigned stage and file ownership. Shared config, migrations, CI, and guidance belong
  to the integration owner unless explicitly delegated.
- Run affected tests and the full quality gate before publication. Review AI-authored tests for the fault they catch.
- Record real non-blocking setup or workflow friction in `~/.config/switchstand/friction.md` as it is observed;
  do not let the scratch record expand the active task.
- Stop on a new material authority, persistence, concurrency, security, or external-interface choice.
- Keep handwritten Bootstrap change at or below 1,400 lines; reforecast before crossing the current allowance.
- Treat `marcogallotta/switchstandold` as read-only evidence, never as an implementation base.
