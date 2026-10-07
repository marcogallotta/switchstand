# Human interaction

Read before reporting status, requesting human attention, or announcing major completion/activation.

Root reports the portfolio. Other agents report only their assigned work and explicitly named children.

When Marco must decide, state in plain language the outcome, exact change, consequence, size, and
recommendation.

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

Wakeful may own timers and deduplicated delivery for these rules, but it owns neither status truth nor authority, and this contract creates no parallel work-management store.
