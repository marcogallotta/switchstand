# Human interaction

Read before reporting status, requesting human attention, or announcing major completion/activation.

Root reports the portfolio. Other agents report only their assigned work and explicitly named children.

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
