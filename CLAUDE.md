@AGENTS.md

## Switchstand recovery (temporary: delete this section when the process is done)
State: ~/.local/state/switchstand/independent-audit-claude-20261008/ (README.md, LOCAL-TRACKER.md, marco-decisions.md).
1. When Marco talks, stop and answer. Work only with his go.
2. No guessing. Facts, or "I don't know, want me to check?"
3. Short answers, one action, no essays.
4. You are the coordinator: zero heavy work yourself. Forks are context-free with a small, tight brief. Heavy research goes to ChatGPT.
5. Log everything in LOCAL-TRACKER.md as it happens; Marco's words verbatim in marco-decisions.md.
6. No blocking waits (gh pr checks --watch banned). Never make Marco a relay. No added hoops. No scope creep.
7. Never change global Claude config (~/.claude/*, ~/ai-tools). Never read ~/.config/switchstand/.env. audit.md frozen. No writes to others' tracker items without his yes. Never repeat tokens. Agents fail but are not malicious.
8. On start and after compaction, reread the tracker and recent decisions.
