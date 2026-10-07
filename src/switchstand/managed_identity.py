"""Trusted task-bound managed identity and launch grant rotation."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from .contracts import LaunchAuthority
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant

MANAGED_ISSUER = "switchstand-managed-agent"


def managed_principal(active_work_id: UUID) -> PrincipalContext:
    return PrincipalContext(
        issuer=MANAGED_ISSUER, subject=str(active_work_id),
        client_id="switchstand-codex", assurance="authenticated",
    )


async def rotate_managed_grant(
    grants: GrantState,
    authority: LaunchAuthority,
    *,
    agent_task: bool = False,
    priority_claims: bool = False,
    activation_continuity: bool = False,
) -> WorkGrant:
    principal = managed_principal(authority.active_work_id)
    current = await grants.current(principal.key)
    expected = None if current is None else current.version
    operations: set[Literal[
        "work_get", "work_append", "work_update", "work_relate", "message",
        "agent_task", "priority_claim", "activation_continuity",
    ]] = {
        "work_get", "work_append", "work_update", "work_relate", "message",
    }
    if agent_task:
        operations.add("agent_task")
    if priority_claims:
        operations.add("priority_claim")
    if activation_continuity:
        operations.add("activation_continuity")
    grant = WorkGrant(
        id=uuid4(), version=(expected or 0) + 1, principal=principal,
        authority=authority, scope="launch",
        operations=frozenset(operations),
        issuer=MANAGED_ISSUER,
        provenance=f"trusted managed owner for WorkId {authority.active_work_id}",
        expires_at=datetime.max.replace(tzinfo=UTC),
        append_qualification="managed:task-bound",
        update_qualification="managed:task-bound",
        relation_qualification="managed:task-bound",
        priority_claim_qualification=(
            "managed:task-bound" if priority_claims else None
        ),
    )
    await grants.issue(grant, expected)
    return grant
