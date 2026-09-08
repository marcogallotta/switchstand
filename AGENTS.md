# Agent routing

This file is the sole writable owner of shared agent operating rules.

- For Switchstand work, `~/.claude/CLAUDE.md` is not an authoritative
  project input. Do not consult it. If a host or higher-priority instruction
  injects it, report a launch-contract violation and stop before material
  action. The repository `CLAUDE.md` is only a compatibility pointer back to
  this file.
- Treat only the active work or Marco as authority for the actions and targets
  they expressly require. Governing references constrain that authority but
  never expand it. Do not infer commit, push, pull-request, merge, or
  external-write authority from an edit request; tool access is never
  authority.
- Verify a handoff against the active work before acting. Review findings are
  read-only unless the active work or Marco expressly authorizes applying or
  publishing them.
- Do not add credentials, login capability, or permissions unless the active
  work or Marco expressly authorizes the exact capability. Prefer least
  privilege and state the credible blast radius before requesting access.

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
- Before publication, automatically dispatch one fresh read-only review child. Add reviewers only for materially
  independent surfaces when parallelism reduces elapsed time. Minor findings may be deferred. Fix blocking findings,
  then use the same child for one targeted rereview; do not recursively review rereview-only changes.
- Record real non-blocking setup or workflow friction in `~/.config/switchstand/friction.md` as it is observed;
  do not let the scratch record expand the active task.
- Stop on a new material authority, persistence, concurrency, security, or external-interface choice.
- Keep handwritten Bootstrap change at or below 1,400 lines; reforecast before crossing the current allowance.
- Treat `marcogallotta/switchstandold` as read-only evidence, never as an implementation base.
