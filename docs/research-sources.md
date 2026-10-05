# Research sources

Last updated: 2026-09-30

A seed list of where to research, for any Switchstand task. It is a lookup, not a report and not
authority: it grants nothing and settles no design question. Entries are sites, orgs and tools, not
individual posts; search inside a source for the current task.

## How to use it

1. For each material question the task depends on, name the two or three closest sources before you
   start searching. Do not read the whole list.
2. On completion, disposition every named source as USED, REJECTED, or UNREACHABLE, each with a
   reason. A consequential conclusion needs at least two independently-sourced dispositions unless the
   question is purely about one authoritative specification; when a genuine opposing position exists,
   include a source capable of contradicting the favored design. One confirming source is not a
   completed search — do not stop at the first source that supports the hypothesis you already hold.
3. If evidence for an important question is still insufficient, deepen the bounded search with
   current web-research capability, directly or through an authorized Worker. Keep the source
   dispositions; do not involve Marco merely to choose a research provider.
4. If a source matters and you cannot reach it, ask Marco to retrieve it.

Whether something is important, "enough," or genuinely uncontested is your judgment; when unsure,
escalate. The disposition in step 2 is the completion condition for research on a material question —
record it with the conclusion it supports (task, chat, or PR description), not only in this file.

This is enforced the same way everything else non-mechanical in this repo is: at review, not by a
linter. A reviewer of a PR or task whose conclusion depended on research checks that the disposition is
actually present and reasoned before accepting the conclusion, and treats a missing, vague, or
single-source disposition on a consequential question as a review finding, not a style nit. An
implementer who cannot point to the disposition when asked has not completed the research step,
regardless of what conclusion they reached.

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
  removable scaffolding, agent evals, long-running agents, permission and containment design
  (including managed-agent sandboxing and Git-proxy credential isolation — see "How We Contain
  Claude Across Products"), postmortems. Skip: product and API announcements. Use: evidence and
  mechanics; vendor voice on Claude itself. Fit: strong.
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
- **GitHub Docs (rulesets and required status checks)** —
  https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets.
  Go for: branch protection and required status checks as an enforced merge gate rather than a written
  policy. Use: mechanics. Fit: strong; the enforcement counterpart to the Tier 2 merge-queue entry.
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
- **arXiv 2606.15828** — https://arxiv.org/abs/2606.15828. Go for: measured smells in agent
  instruction files (bloat, conflicting rules, rules a linter already enforces) — direct evidence for
  replacing prose rules with enforcement. Use: evidence, read at abstract level. Fit: directly on
  point.

## Tier 2

- **Cloudflare Blog** — https://blog.cloudflare.com. Go for: incident write-ups, AI code review at
  scale. Skip: product launches. Use: evidence. Fit: good, but its review blocks merges.
- **Scott Logic** — https://blog.scottlogic.com. Go for: agent safety by design, measured spec-driven
  trials. Use: opinion. Fit: partial.
- **Google SRE** — https://sre.google. Go for: incident command and live-state documents,
  postmortem culture, canarying, blameless learning, and owned follow-up actions. Skip:
  Google-scale capacity and role machinery. Use: mechanics. Fit: good. Incident-management and
  postmortem chapters verified reachable and used for the Code Red runbook on 2026-09-30.
- **PagerDuty Incident Response** — https://response.pagerduty.com. Go for: an independent
  operational playbook capable of challenging Google-derived incident roles, status, and
  lifecycle. Use: mechanics and vendor voice. Fit: good after removing its larger-team ceremony.
  The legacy page was not fetchable by the automated research client on 2026-09-30, but the
  first-party GitHub source and current PagerDuty Ops Guide were reachable and used for the Code
  Red update/verification/follow-up lifecycle.
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

### MCP external and stable authentication

Use this group when evaluating an authorization service outside the replaceable MCP edge. The
current dispositions and remaining proof are summarized in
[deferred zero-downtime authentication options](deferred-zero-downtime-auth.md).

- **MCP authorization specification and RFC 8707** —
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization and
  https://www.rfc-editor.org/info/rfc8707/. Go for: required OAuth, protected-resource, `resource`,
  and audience behavior. Skip: product topology advice. Use: normative mechanics. Fit: strong;
  verified and USED as the protocol baseline.
- **FastMCP remote OAuth providers** — https://gofastmcp.com/servers/auth/remote-oauth,
  https://gofastmcp.com/python-sdk/fastmcp-server-auth-providers-descope, and
  https://gofastmcp.com/python-sdk/fastmcp-server-auth-providers-workos. Go for: external
  authorization-server and local resource-server seams. Skip: treating OAuth Proxy or the
  full-server helpers as a ready stable auth boundary. Use: framework mechanics. Fit: strong for a
  disposable spike; remote providers USED, embedded proxy/full-server route REJECTED for now.
- **Descope MCP and inbound authorization server** — https://docs.descope.com/mcp and
  https://docs.descope.com/identity-federation/inbound-apps/authorization-server. Go for: managed
  MCP authorization, CIMD/DCR, resources, JWTs, and a custom GitHub upstream. Skip: assuming exact
  GitHub numeric-ID claims or refresh-family replay semantics without live proof. Use: provider
  mechanics. Fit: best managed spike candidate; verified and USED with those unknowns preserved.
- **WorkOS AuthKit MCP** — https://workos.com/docs/authkit/mcp. Go for: managed CIMD/DCR and
  resource-bound MCP tokens. Skip: assuming its email/user-centered identity model preserves the
  current GitHub numeric-ID-plus-scope trust claim. Use: provider mechanics. Fit: viable managed
  runner-up; verified and USED, exact identity preservation still requires proof.
- **Pomerium MCP** — https://www.pomerium.com/docs/capabilities/mcp/protect-mcp-server. Go for: a
  stable self-hosted gateway, client compatibility, policy, audit, and upstream token handling.
  Skip: default-memory state or assuming a not-yet-qualified GitHub stable-ID release. Use: gateway
  mechanics. Fit: best self-hosted candidate but operationally larger; verified and USED, deferred.
- **GitHub OAuth docs** —
  https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps and
  https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps. Go for:
  upstream identity, scope, token, and authorization constraints. Skip: using mutable login or email
  as the Switchstand trust anchor. Use: provider mechanics. Fit: strong and USED to define the exact
  numeric-ID/scope qualification boundary.
- **Keycloak MCP authorization** — https://www.keycloak.org/securing-apps/mcp-authz-server. Go for:
  tracking future standards support. Skip now: incomplete RFC 8707 support, experimental CIMD, and
  disproportionate single-host operations. Use: implementation-status evidence. Fit: weak today;
  verified and REJECTED for the current shortlist.

- **CNCF CloudEvents** — https://github.com/cloudevents/spec. Go for: a small vendor-neutral event
  envelope, stable source/event identity, and transport-independent producer/consumer boundaries.
  Skip: claiming CloudEvents conformance when only its concepts are needed. Use: specification
  mechanics. Fit: strong for Wakeful's agent-neutral event seam; verified and used for the Wakeful
  product draft on 2026-09-30.
- **GitHub Docs (webhooks)** — https://docs.github.com/en/webhooks. Go for: signed event intake,
  stable delivery identity, quick acceptance with asynchronous work, and explicit redelivery. Use:
  provider mechanics. Fit: strong for CI and repository event producers; verified and used for the
  Wakeful product draft on 2026-09-30.
- **OpenAI Agents API sessions/webhooks** —
  https://developers.openai.com/api/docs/guides/agents-api/sessions. Go for: persistent session
  continuation, terminal lifecycle states, and webhook-driven agent lifecycle integration. Use:
  first-party mechanics, not as proof that the local Codex host exposes the same delivery surface.
  Fit: useful candidate adapter evidence; verified and used for the Wakeful product draft on
  2026-09-30.
- **DBOS** — https://www.dbos.dev/blog. Go for: Postgres-backed durable execution. Use: vendor voice.
  Fit: use when a concrete checkpoint/resume consumer exists, not as generic inspiration.
- **Temporal** — https://temporal.io/blog. Go for: durable agent workflows. Use: vendor voice. Fit:
  use when a concrete checkpoint/resume consumer exists, not as generic inspiration; the North Star
  rejects a rewrite for its own sake.
- **Kubernetes KEPs and Rust RFCs** — https://github.com/kubernetes/enhancements/blob/master/keps/README.md
  and https://rust-lang.github.io/rfcs/. Go for: design docs kept in a repo with a tracking issue for
  status, and change control for those docs. Skip: their full ceremony, heavy for one person; see
  Nick Cameron's critique (https://ncameron.org/blog/the-problem-with-rfcs): slow, one template for
  every change size, no record of what was built. Use: mechanics and opinion. Fit: partial.

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
