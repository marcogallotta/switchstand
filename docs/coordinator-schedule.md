# Coordinator manual schedule (V1)

This file is a **manually triggered due-work list**, not a background scheduler. Marco can tell the Coordinator **“check the schedule”** whenever he chooses. No polling cadence, service, or wake-up mechanism is implied or required. Inactive sessions do not execute entries.

## Schedule

| ID | Next due (UTC, ISO 8601) | Action and success evidence | WorkId | Responsible owner | State | Last completion (UTC) | Result reference |
| --- | --- | --- | --- | --- | --- | --- | --- |
| _No authorized dated entries yet_ | — | — | — | — | — | — | — |

Use one row per approved scheduled action. The responsible owner sets a concrete due time after the action, scope and recurrence (if any) are approved. Do not invent due dates. If no due date is known, track the decision in the exact Work rather than treating it as executable schedule work.

## On “check the schedule”

1. Read the complete current schedule and the current clock. Find all **pending or overdue** entries; include missed entries after Coordinator downtime. Report no due entries briefly if there are none.
2. For each due entry, read its **current WorkId** and applicable grant/approval. A schedule row never supplies missing assignment or effect authority. Where approval, owner, input, or dependency is missing, leave it pending and report the precise blocker.
3. Perform each currently authorized action; reconcile uncertain or interrupted effects using its original idempotent identity before attempting another. Record actual result evidence in the Work.
4. Update the row with the last verified result, a completed/blocked state, and the **next due** only if an existing approved recurrence defines it. Failed or missed work must remain visible rather than disappearing or being marked complete.
5. If file edits cannot be persisted safely from the Coordinator's current session, preserve the result on the corresponding Work and report that this schedule remains stale. Never claim a row was updated without readback.

## Limits

- **Execution happens only when Marco asks an active Coordinator to check the schedule.** “Guaranteed follow-up” here means only durable visibility and catch-up upon the next manual check—not a guaranteed execution time.
- This file must live on the currently adopted repository branch and be writable through the authorized repository workflow. Changes in an unmerged PR are proposals, not a live schedule.
- Do not treat recurring work or reviews as authorization to merge, deploy, bypass protected gates, or act on a production provider. That authority remains on the exact Work and current grant.
