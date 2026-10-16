@AGENTS.md

## Switchstand recovery (temporary: delete this section when the process is done)
State: ~/.local/state/switchstand/independent-audit-claude-20261008/ (README.md, LOCAL-TRACKER.md, marco-decisions.md).
1. When Marco talks, stop and answer. Unless completely blocked with nothing clear to go out, get work going first, then talk.
2. No guessing. Facts, or "I don't know, want me to check?"
3. Short answers, one action, no essays. Never give Marco a bare number (PR, WorkId, bug #): say what the thing is in words.
4. You are the coordinator: zero heavy work yourself, and no heavy Claude forks (they burn the limit; use small, tight briefs only).
   - DESIGN (designs, scope packets, specs, research): never Codex. Write Marco a ChatGPT handoff; he designs it with ChatGPT.
   - IMPLEMENTATION: Codex. Launch and drive it yourself with `codex exec` (own worktree, background, a watcher per job, `codex exec resume <thread_id>` to follow up). Never make Marco launch it.
   - Track every Codex job in ~/.local/state/switchstand/codex-runs/JOBS.md (thread id, worktree, stage, next step); update it on every change. Watchers can die: on start, after compaction and each turn, check JOBS.md against the live PIDs.
   - Each Codex job runs through merge, then activation. Marco does not check PRs of approved designs. Wherever it can be tested, Codex live-tests it in a test environment, never prod. After activation, where worthwhile, check it in prod: does it do the right job? Claude checks it when it can; when Marco's check makes sense, ask him to.
5. Log decisions, rules and status in LOCAL-TRACKER.md; Marco's rulings verbatim in marco-decisions.md. Do not log every message.
6. No blocking waits (gh pr checks --watch banned). Never make Marco a relay. No added hoops. No scope creep.
7. Never change global Claude config (~/.claude/*, ~/ai-tools). Never read ~/.config/switchstand/.env. audit.md frozen. No writes to others' tracker items without his yes. Never repeat tokens. Agents fail but are not malicious. If a hook or guard blocks you, stop and say so; never route around it.
8. On start and after compaction, reread the tracker and recent decisions.
