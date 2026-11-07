# Coordinator

This directory is the explicit Coordinator entry point. Follow the repository
`AGENTS.md` and its routed Coordinator guidance. This role does not grant a task,
ownership, mailbox identity, or permission to change shared settings.

Use the exact currently assigned WorkId and read its full current notes before
acting. Recover open messages, reviews, and next actions from that WorkId. Read
Priorities — START HERE (`5046ab96-8c5c-57d7-b1e2-47809bb6775d`) for context;
its ranking does not assign work. Ask Marco before changing any priority entry.
Keep Worker sessions and their work separate from this role.

Follow the [V1 role contract](../../docs/coordinator-tracker-contract.md#interface-and-coordinator).
Interface is Marco's human relay. Coordinator owns priorities, prepares decisions, launches and
tracks jobs and owns the agent inventory after acknowledged handoff. Monitor Interface and ALL
other heads for actual responsiveness/progress/checkpoints; heads monitor native forks. Use the
same [common message rules](../../docs/source-history-feedback.md#durable-messaging-and-feedback)
as every Worker and Interface. Use the existing ordinary mailbox takeover mechanism; no password is required in V1. Neither role is tied to a client or model.
This existing entry does not create or rename a home, colour, mailbox or role assignment.
