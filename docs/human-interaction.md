# Human interaction

Read before reporting status, requesting human attention, or announcing major completion/activation.

Root reports the portfolio. Other agents report only their assigned work and explicitly named children.

When Marco must decide, state in plain language the outcome, exact change, consequence, size, and
recommendation.

Treat requests such as “clean”, “polish”, and “make sure” as bounded to the
named targets and their direct invariants. Do not expand them into archaeology,
a portfolio audit, or unrelated cleanup. Stop once the requested postcondition
is established and report it; route optional broader work separately.

Human Review follows [Human Review](human-review.md): make the real human-owned choice clear before implementation, use a natural short explanation rather than fixed headings, and do not ask again for an unchanged approved choice. Authorized progress and completion receive truthful status, not a new approval request. The formats below are for ordinary status and attention, not a substitute for a Human Review conversation.

Put the result, material delta, or exact action first; keep provenance behind it unless needed.
For a handoff, name the resolved recipient and truthful action state. Route it directly when
authorized and capable; otherwise name the stable recipient and provide one complete ready-to-copy
fenced block. Never use a task ID as a recipient or receipt, and never use destination-unclear
"dispatch", "run", "send", or "review" wording.

## Never use Marco as a command relay

Assume Marco will run any command an agent asks him to run without independently checking whether
it is safe. Asking him to execute a command is therefore an execution effect, not a safety gate.

- If an operation is routine, reversible, within the current assignment, and technically available,
  the agent performs it and reads back the result. Do not ask Marco to run it, approve it, or decide
  whether it is safe.
- A sandbox, permission, or missing-tool restriction that prevents routine assigned work is a
  capability defect to route, repair, or report. It never makes Marco the fallback operator.
- Ask Marco only for an actual consequential decision, authority that is genuinely absent, or an
  intrinsically human-only action. Prefer asking for the decision, not supplying a shell command.
- If a human-only command is genuinely unavoidable, present only the smallest exact command after
  establishing that its effect is safe and necessary; do not transfer unresolved safety analysis to
  Marco.

Keep exact WorkId and protected-effect authority boundaries. This rule removes routine execution
relay; it does not turn repository authority into deployment, credential, migration, or production
authority.

## Agent-owned decision carry-forward

Carry forward material settled directions, corrections, rejected remedies and deferrals. Quote human words only if actually visible; otherwise label the agent-authored working paraphrase. Surface a short reminder when naturally useful for correction, without requiring the human to revalidate ordinary records. Silence never grants new approval.

## Item update

```text
<Work> — <state>
Changed: <material change>
Constraint: <gate/blocker/none>
Next: <next observable checkpoint>
Effect: none / not sent / applied / unknown
```

No activity logs or invented percentages.

## Portfolio snapshot

| Work | Purpose | Worker | Milestone | Constraint | Next | Effect |
| --- | --- | --- | --- | --- | --- | --- |

One row per active item.

## Human attention

Use exactly one:

- ACTION REQUIRED — human action is required.
- HELP COULD UNBLOCK — help could materially accelerate recovery.
- INPUT VALUABLE — judgment is needed before a consequential choice hardens.

```text
<LABEL> — <subject>
Ask: <exact action/decision>
Why now: <why it matters now>
Blocked: <what cannot proceed, or none>
Continuing: <what still proceeds>
If answered: <what changes>
```

Put evidence, test detail, provenance, hook/runtime mechanics, paths, hashes, and continuation detail on the owning work unless the human needs them to act or asks for them.

Raise human attention immediately, outside the normal status cadence. Keep unresolved attention visible and repeat it at a frequency proportionate to urgency while other work continues, until the human acknowledges or defers it, ownership is explicitly transferred with pickup, or the condition is cleared. Silence and unrelated progress do not clear or reset it.

For progress reporting, use a percentage only when there is a stable milestone denominator and relevant calibration evidence; otherwise report known milestones and uncertainty.

Major completion/activation notices are stricter: keep them visible and repeat them at a frequency proportionate to importance until the human explicitly acknowledges them. Silence, deferral, transfer, unrelated progress, or ordinary clearing of an attention condition does not acknowledge a major completion/activation notice.

After landing, report the behavior separately as `LIVE`, `INERT`, `DEFAULT-OFF`, or `UNAVAILABLE`,
and name any remaining activation, adoption, readback, or proof obligation. Landing evidence never
silently becomes live-reliance evidence.

Acknowledging a notice does not clear an unfinished activation or human-action watch. Keep that
watch visible at meaningful active checkpoints and re-entry until the action completes or Marco
explicitly defers it.

Wakeful may own timers and deduplicated delivery for these rules, but it owns neither status truth nor authority, and this contract creates no parallel work-management store.
