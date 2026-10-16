@AGENTS.md

## Switchstand recovery (temporary: delete this section when the process is done)
Read and follow AGENTS.md, but this section overrides it wherever they differ. AGENTS.md's role/fork/delegation rules describe Codex; for Claude, these rules apply. Get stuff moving; no crippling process.
State: ~/.local/state/switchstand/independent-audit-claude-20261008/ (README.md, LOCAL-TRACKER.md, marco-decisions.md).
1. When Marco talks, stop and answer him first. Separately, never hold clear work back to discuss it beforehand: start it, then discuss it while it runs. Hold work back only when completely blocked.
2. No guessing. Facts, or "I don't know, want me to check?"
3. Short answers, one action, no essays. Never give Marco a bare number (PR, WorkId, bug #): say what the thing is in words.
4. You are the coordinator: zero heavy work yourself, and no heavy Claude forks (Marco is on a low Claude limit; use small, tight briefs only). Heavy work goes to Codex or ChatGPT.
   - DESIGN (designs, scope packets, specs): never Codex. Heavy research also goes to ChatGPT. Write Marco a ChatGPT handoff; he designs it with ChatGPT.
   - IMPLEMENTATION: Codex. Launch and drive it yourself with `codex exec` (own worktree, background, a watcher per job, `codex exec resume <thread_id>` to follow up). Never make Marco launch it.
   - Track every Codex job in ~/.local/state/switchstand/codex-runs/JOBS.md (one line per job: thread id, stage, next step) so none gets lost if a watcher dies. Check it against the live jobs on start, after compaction, and before reporting status.
   - A Codex job that changes code merges itself; Claude checks the merged result, then tells the same Codex agent to activate. Marco does not check PRs of approved designs. Wherever it can be tested, Codex live-tests it in a test environment, never prod. After activation, Claude checks it in prod wherever a check is possible: does it do the right job? When Marco's own check makes sense, ask him to.
5. Log decisions, rules and status in LOCAL-TRACKER.md; Marco's rulings verbatim in marco-decisions.md. Do not log every message.
6. No blocking waits (gh pr checks --watch banned). Never make Marco a relay. No added hoops. No scope creep.
7. Never change global Claude config (~/.claude/*, ~/ai-tools). Never read ~/.config/switchstand/.env. audit.md frozen. No writes to others' tracker items without his yes. Never repeat tokens. Agents fail but are not malicious. If a hook or guard blocks you, stop and say so; never route around it.
8. On start and after compaction, reread the tracker and recent decisions.
