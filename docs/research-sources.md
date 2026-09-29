# Research sources

Last updated: 2026-09-29

A seed list of where to research, for any Switchstand task. It is a lookup, not a report and not
authority: it grants nothing and settles no design question. Entries are sites, orgs and tools, not
individual posts; search inside a source for the current task.

## How to use it

1. Find the area your task touches, pick the two or three closest sources, and search there. Do not
   read the whole list.
2. If you cannot find enough on something important, request a deeper search by Claude Code, seeded
   from this list; a ChatGPT or Codex agent should not keep searching badly. A direct
   agent-to-Claude-Code route is not established yet (see the roadmap), so make the request to Marco.
3. If a source matters and you cannot reach it, ask Marco to retrieve it.

Whether something is important or you have "enough" is your judgment; when unsure, escalate.

## Keeping it current

Any research that finds, verifies, downgrades or rejects a source, or fills a gap listed at the end,
updates this file in the same change. Do not leave findings only in a task or chat.

## How to read an entry

- **Use:** mechanics (docs, specs, tool behavior; follow them), evidence (postmortems, measured
  results; learn from them), or opinion/vendor voice (read critically; do not adopt the model).
- **Skip:** caution applies per topic, not per org. A source can be right for one topic and wrong for
  another, often because of scale.
- **Fit:** against Marco's model: anti-ceremony, anti-black-box, anti-blocking.
- Tier 1 is highest leverage for Switchstand's core problems: agents not following written process,
  false completion claims, review-process failure modes, and Marco's attention.

Status: built 2026-09-29 by research agents. Entry URLs were checked reachable, except three that
refused automated fetches (OpenAI harness post, Microsoft Research blog, Uber blog). Topic notes are
agent-reported and not independently verified.

## Tier 1

- **Anthropic Engineering** — https://www.anthropic.com/engineering. Go for: harness design and
  removable scaffolding, agent evals, long-running agents, permission and containment design,
  postmortems. Skip: product and API announcements. Use: evidence and mechanics; vendor voice on
  Claude itself. Fit: strong.
- **Claude Code docs** — https://code.claude.com/docs/en/overview. Go for: hooks, AGENTS.md and
  CLAUDE.md handling, subagents, sandboxing, and how much instruction to load and when (the memory
  and skills pages under https://code.claude.com/docs/en/). Use: mechanics. Fit: good.
- **OpenAI Codex docs** — https://learn.chatgpt.com/docs. Go for: AGENTS.md, approvals and sandbox,
  hooks, code review, automations. Use: mechanics. Fit: good.
- **OpenAI harness engineering** — https://openai.com/index/harness-engineering/. Go for: repository
  as system of record, a short AGENTS.md index, few blocking gates. Skip: the "no manual code"
  framing as a target. Use: evidence, vendor voice. Fit: partial; its minimal human review conflicts
  with selective code review. Not fetchable automatically; read it in a browser.
- **OpenAI Symphony** — https://github.com/openai/symphony. Go for: tracker-driven runs, restart
  recovery from tracker state, proof-of-work, human review before merge, and the repo-versus-tracker
  split (work items in the tracker; rules and prompt in a versioned WORKFLOW.md in the repo, per
  https://github.com/openai/symphony/blob/main/SPEC.md). Use: mechanics, read
  critically (an experimental preview; its proof-of-work is agent-produced, so check it
  independently). Fit: good.
- **Stripe Dev Blog** — https://stripe.dev/blog. Go for: end-to-end agent path with hard round limits,
  real CI gating, human review before merge. Skip: monorepo-scale test infrastructure. Use:
  evidence. Fit: good.
- **Databricks Consort** — https://github.com/databricks-solutions/consort. Go for: a state machine
  the agent cannot edit, review separation. Use: mechanics and opinion; tiny and unproven. Fit:
  mixed; nothing advances without human approval conflicts with anti-blocking.
- **Google eng-practices** — https://google.github.io/eng-practices/. Go for: review standards, small
  changes. Use: mechanics. Fit: good; predates AI-era review load.
- **Thoughtworks Technology Radar** — https://www.thoughtworks.com/radar. Go for: spec-driven
  development and agent instruction bloat critique. Use: opinion. Fit: strong; supports anti-ceremony.
- **Martin Fowler** — https://martinfowler.com. Go for: agentic engineering commentary, whether to
  review all generated code, coordination. Use: opinion. Fit: good.
- **Simon Willison, agentic engineering patterns** —
  https://simonwillison.net/guides/agentic-engineering-patterns/. Go for: test-first practice with
  agents. Use: opinion and practice. Fit: good.
- **arXiv 2605.29442** — https://arxiv.org/abs/2605.29442. Go for: a study of about 20,000 real agent
  sessions on constraint violations, inaccurate self-reporting and false completion. Use: evidence.
  Fit: directly on point.

## Tier 2

- **Cloudflare Blog** — https://blog.cloudflare.com. Go for: incident write-ups, AI code review at
  scale. Skip: product launches. Use: evidence. Fit: good, but its review blocks merges.
- **Scott Logic** — https://blog.scottlogic.com. Go for: agent safety by design, measured spec-driven
  trials. Use: opinion. Fit: partial.
- **Google SRE** — https://sre.google. Go for: postmortem culture, canarying, blameless learning.
  Skip: Google-scale capacity material. Use: mechanics. Fit: good.
- **Cursor Blog** — https://cursor.com/blog. Go for: running many agents, long-run efficiency. Use:
  vendor voice. Fit: mixed; autonomy-forward.
- **Cognition Blog** — https://cognition.com/blog. Go for: multi-agent arguments (writes stay
  single-threaded), review tooling. Skip: partnership and funding news. Use: evidence and opinion.
  Fit: mixed.
- **Spotify Engineering** — https://engineering.atspotify.com. Go for: coding-agent feedback loops,
  LLM judges. Skip: unrelated data posts. Use: evidence. Fit: good.
- **Shopify Engineering** — https://shopify.engineering. Go for: harness design that outlasts the
  model, durable event logs. Use: evidence. Fit: strong for a replaceable harness.
- **Uber engineering** — https://www.uber.com/us/en/blog/ureview/. Go for: multi-stage AI review and
  its measured usefulness. Skip: its diff-volume scale. Use: evidence. Not fetchable automatically.
- **GitHub Docs (merge queue)** —
  https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue.
  Use: mechanics.
- **GitLab Docs (merge trains)** — https://docs.gitlab.com/ci/pipelines/merge_trains/. Use: mechanics.
- **GitHub Blog, AI and ML** — https://github.blog/ai-and-ml/. Go for: parallel-agent technique. Use:
  vendor voice.
- **AI SDK tool approvals** — https://ai-sdk.dev/docs/agents/policy-tool-approvals. Go for:
  deterministic policy in front of tool calls. Use: mechanics. Fit: good.
- **Open Policy Agent** — https://www.openpolicyagent.org. Go for: policy as reviewable code. Use:
  mechanics.
- **OWASP GenAI, agentic security** — https://genai.owasp.org. Go for: agentic threats and control
  standards. Use: mechanics and opinion. Fit: good for authority and safety.
- **DORA** — https://dora.dev/ai/. Go for: measuring delivery with AI. Use: evidence. Partial.
- **agent-postmortems** — https://swarmproof.github.io/agent-postmortems/. Go for: sourced incident
  catalogue by failure category. Use: evidence; an aggregator, so follow citations to the primary.
- **METR** — https://metr.org/blog. Go for: honest measurement of AI productivity and monitorability.
  Use: evidence.
- **Goose (Block)** — https://github.com/aaif-goose/goose. Go for: MCP extension model. Use:
  mechanics. Fit: mixed; its Smart Approval mode is an LLM judging its own gate.
- **OpenHands** — https://docs.openhands.dev. Go for: agent SDK, sandbox server. Use: mechanics.
- **DBOS** — https://www.dbos.dev/blog. Go for: Postgres-backed durable execution. Use: vendor voice.
- **Temporal** — https://temporal.io/blog. Go for: durable agent workflows. Use: vendor voice. Fit:
  only if a workflow engine is ever wanted; the North Star rejects a rewrite for its own sake.
- **Kubernetes KEPs and Rust RFCs** — https://github.com/kubernetes/enhancements/blob/master/keps/README.md
  and https://rust-lang.github.io/rfcs/. Go for: design docs kept in a repo with a tracking issue for
  status, and change control for those docs. Skip: their full ceremony, heavy for one person; see
  Nick Cameron's critique (https://ncameron.org/blog/the-problem-with-rfcs): slow, one template for
  every change size, no record of what was built. Use: mechanics and opinion. Fit: partial.
- **arXiv 2606.15828** — https://arxiv.org/abs/2606.15828. Go for: measured smells in agent
  instruction files (bloat, conflicting rules, rules a linter already enforces). Use: evidence, read
  at abstract level. Fit: on point for instruction bloat.

## Tier 3 and use with caution

- **LangGraph / LangChain** — https://docs.langchain.com. Use only for pause, persist and resume
  concepts; framework lock-in risk.
- **Aider** — https://aider.chat/docs. Go for: the run-real-tests loop. Slowed maintenance.
- **Sourcegraph** — https://sourcegraph.com/blog. Go for: agent evaluation; vendor voice.
- **GitLab Blog** — https://about.gitlab.com/blog/. Marketing-heavy.
- **OpenTelemetry** — https://opentelemetry.io/blog. GenAI conventions unconfirmed; use selectively.
- **Meta Engineering** — https://engineering.fb.com. No relevant review or agent posts found;
  revisit only for a specific topic.
- **Microsoft Research** — https://www.microsoft.com/en-us/research/blog/. Low value found; not
  fetchable automatically.
- **GitHub Spec Kit** — https://github.com/github/spec-kit. Popular, but a sequential ceremony
  workflow. Do not treat as enforcement.
- **BMAD-METHOD** — https://github.com/bmad-code-org/BMAD-METHOD. Roles and phases with no
  enforcement. Treat as ceremony.
- **Open-source AI-contribution policies** — Ghostty
  (https://github.com/ghostty-org/ghostty/blob/main/AI_POLICY.md), QEMU
  (https://www.qemu.org/docs/master/devel/code-provenance.html). Evidence of maintainer review
  overload, not of team process.
- Not to follow as design sources: MetaGPT and ChatDev (stale), AutoGPT (a failure case), Devin
  marketing, vendor "AI governance" blogs.

## Gaps with no good source yet

Syncing a tracker with repo docs, and reviewing changes to agent policy docs (Symphony's split and
KEPs are the nearest). Intake and requirements (ask versus proceed), kill switch and rollback,
stall and loop detection, first-party spend limits, audit cadence, agent aging and replacement, first-party guidance on
design and spec review, measuring the whole system, and false "done" claims beyond the arXiv study
above. When research fills one, update this list.
