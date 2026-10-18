# Human Input

Human Input is how the human shapes consequential work while choices are still cheap to change. It is not an approval stage and it is not reserved for when the agent is stuck.

## Core rule

Before hardening a materially new capability or a remedy that materially expands the work,
make a bounded check of current capabilities, relevant existing/in-flight work and its owners,
and Marco's current decisions, corrections and rejected alternatives. Use current source
references rather than treating a supplied summary as fresh. Distinguish working capability,
approved design and UNKNOWN; overlap alone does not prove the requested benefit is delivered.

Use those facts in the existing task/plan to identify the useful gap and compare a smaller
benefit-preserving route before developing the mechanism. A manual substitute that loses the
agreed benefit or shifts work to Marco is not automatically simpler. Do enough bounded research
to understand the real problem, plausible routes and important unknowns; do not turn this into
a broad inventory, new artifact or routine human question. The first existing material-design
review starts on the provisional route under [code quality](code-quality.md), while useful
early Human Input and unaffected work continue.

Raise Human Input before the next material commitment would harden a choice about product outcome, architecture, scope, scale, cost or support burden, material risk, workflow, or a mechanism that materially governs agent behavior, the human's workflow, or durable operating policy — when human judgment could materially change that choice.

When Marco explicitly signals availability, a useful diagnostic question can be asked after bounded research without turning the entire task into a hold. Never infer his availability from silence or response latency.

Broadness alone is not a trigger. Narrow, local, reversible work and questions that cheap evidence can settle stay agent-owned.

Continue research and option-preserving work while waiting. Hold only the branch that would consume the material choice.

After direction is clear, continue deeper work. Raise Human Input again only when new evidence, uncertainty, consequence, scale, cost, or a materially different route changes the choice.

## Grounding

When the decision materially depends on current usage, operating scale, concurrency, workflow, support, or deployment shape, read current product/usage evidence before hardening around assumptions.

Current observed use is a baseline, not a permanent cap. If moving beyond it materially changes architecture, coordination/reliability, support burden, implementation size, or another consequential choice, raise Human Input before hardening.

Distinguish scale dimensions. Directly steered top-level agents are not the same as total workers, reviewers, or children.

Do not substitute generic best practice for current product reality.

## What to surface

Use plain language:

- the exact choice forming and why it matters now;
- what was learned and any load-bearing fact, inference, assumption, or unknown;
- genuinely different alternatives, when they exist;
- the recommendation and decisive reason;
- material consequence, size, cost, burden, or risk;
- what safely continues while waiting.

For new or materially changed work, also expose the intended operating scale, introduced complexity and why it is required now, the smallest credible alternative, and deliberate V1 deferrals. Prefer the smallest safe useful vertical stage. If the proposed first stage is foundation-only or inert, explain why a vertical stage is unsafe or impractical, why the foundation is independently stable and non-speculative, and the immediate route to representative useful end-to-end proof.

Do not manufacture a menu when there is one clear recommendation.

## Do not ask

Do not raise Human Input for local realization inside an agreed design, reversible preferences without meaningful consequence, questions cheap evidence can answer first, settled direction unless material evidence changes it, or process checkpoints merely because they exist.

Silence is not approval.

## Agent-owned decision continuity

Agents accurately carry forward material decisions, rejected alternatives, corrections, and deferrals. Quote human words only when directly visible; otherwise mark the working summary as an agent paraphrase, with referent and available source pointer. A paraphrase is not independently authenticated and grants no new effect authority.

Missing independent verification of original messages must not block otherwise authorized work or require routine human confirmation. When material ambiguity or conflict could change direction, investigate what is available and ask one useful question; continue unaffected work. At the next naturally relevant discussion, briefly surface important carried-forward decisions without requiring a response.

## Durable trajectory, when available

For substantive reviewed work, use `human_trajectory_get` on the exact WorkId to recover the current human outcome, settled/rejected choices, open questions, current slice and remainder. On a material correction or decision, the responsible agent uses `human_trajectory_record` with the same WorkId, expected generation/predecessor and stable operation ID. Record directly visible words accurately, or label an agent-authored paraphrase and give an available source pointer. Neither tool authenticates original human wording or grants any effect authority; **never ask Marco to validate routine records**. If typed storage is unavailable, continue authorized work using the current owner notes and mark typed continuity UNKNOWN; do not block the whole task.

## Continuity

An unresolved Human Input question must survive handoff/replacement with the exact decision, why it matters, current recommendation, safe continuing work, and the action that would harden the choice.
