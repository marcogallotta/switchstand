"""Trusted task-bound managed identity and launch grant rotation."""

from datetime import UTC, datetime
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


async def rotate_managed_grant(grants: GrantState, authority: LaunchAuthority) -> WorkGrant:
    principal = managed_principal(authority.active_work_id)
    current = await grants.current(principal.key)
    expected = None if current is None else current.version
    grant = WorkGrant(
        id=uuid4(), version=(expected or 0) + 1, principal=principal,
        authority=authority, scope="launch",
        operations=frozenset({"work_get", "work_append", "work_update", "message"}),
        issuer=MANAGED_ISSUER,
        provenance=f"trusted managed owner for WorkId {authority.active_work_id}",
        expires_at=datetime.max.replace(tzinfo=UTC),
        append_qualification="managed:task-bound",
        update_qualification="managed:task-bound",
    )
    await grants.issue(grant, expected)
    return grant
