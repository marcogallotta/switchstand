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

Major completion/activation notices remain visible until the human explicitly acknowledges them. Unresolved attention remains visible until acknowledged, deferred, transferred with pickup, or cleared. Silence and unrelated progress do not clear either. Trigger/cadence is owned outside this file.
