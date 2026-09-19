"""Trusted task-bound managed-agent identity and durable grant rotation."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from .contracts import LaunchAuthority
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant

MANAGED_ISSUER = "switchstand-managed-agent"
MANAGED_CLIENT_ID = "switchstand-codex"
MANAGED_APPEND_QUALIFICATION = "managed:task-bound"
MANAGED_EXPIRES_AT = datetime.max.replace(tzinfo=UTC)


def managed_principal(active_work_id: UUID) -> PrincipalContext:
    """Stable task-owner identity; runtime/run IDs are deliberately excluded."""
    return PrincipalContext(
        issuer=MANAGED_ISSUER,
        subject=str(active_work_id),
        client_id=MANAGED_CLIENT_ID,
        assurance="authenticated",
    )


async def rotate_managed_grant(
    grants: GrantState, authority: LaunchAuthority,
) -> tuple[PrincipalContext, WorkGrant]:
    """Rotate one durable task-bound grant when a trusted managed run is provisioned."""
    principal = managed_principal(authority.active_work_id)
    current = await grants.current(principal.key)
    expected_version = None if current is None else current.version
    version = 1 if current is None else current.version + 1
    replacement = WorkGrant(
        id=uuid4(),
        version=version,
        principal=principal,
        authority=authority,
        scope="launch",
        operations=frozenset({"work_get", "work_append"}),
        issuer=MANAGED_ISSUER,
        provenance=f"trusted managed owner for WorkId {authority.active_work_id}",
        expires_at=MANAGED_EXPIRES_AT,
        append_qualification=MANAGED_APPEND_QUALIFICATION,
    )
    await grants.issue(replacement, expected_version)
    return principal, replacement
