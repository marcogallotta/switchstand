"""Server-owned ordinary workspace admission independent of managed WorkGrant rows."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import LaunchAuthority
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant
from .state import work_handles

PrincipalResolver = Callable[[], Awaitable[PrincipalContext | None]]
WORKSPACE_ADMISSION_NAMESPACE = UUID("5ea05770-b919-4f7a-96c3-91bfe65074f4")
WORKSPACE_CONTEXT = UUID(int=0)


class WorkspaceAdmissionState(GrantState):
    """Effect-journal-compatible admission that never reads or writes work_grants."""

    def __init__(self, engine: AsyncEngine, principal: PrincipalResolver):
        super().__init__(engine)
        self.principal = principal

    @staticmethod
    def admission(principal: PrincipalContext) -> WorkGrant:
        prefix = "test" if principal.assurance == "test" else "real"
        return WorkGrant(
            id=uuid5(WORKSPACE_ADMISSION_NAMESPACE, principal.key),
            version=1,
            principal=principal,
            authority=LaunchAuthority(active_work_id=WORKSPACE_CONTEXT),
            scope="workspace",
            operations=frozenset({
                "work_get", "work_search", "work_append", "work_create", "work_update",
                "work_relate", "priority_claim",
            }),
            issuer="switchstand-ordinary-authenticated",
            provenance="server-owned ordinary authenticated workspace admission",
            expires_at=datetime.max.replace(tzinfo=UTC),
            append_qualification=f"{prefix}:ordinary-workspace",
            create_qualification=f"{prefix}:ordinary-workspace",
            update_qualification=f"{prefix}:ordinary-workspace",
            relation_qualification=f"{prefix}:ordinary-workspace",
            priority_claim_qualification=f"{prefix}:ordinary-workspace",
        )

    async def current(self, principal_key: str) -> WorkGrant | None:
        principal = await self.principal()
        if principal is None or principal.key != principal_key:
            return None
        return self.admission(principal)

    @asynccontextmanager
    async def locked(
        self, principal_key: str, work_id: UUID | None = None,
    ) -> AsyncGenerator[WorkGrant | None]:
        principal = await self.principal()
        if principal is None or principal.key != principal_key:
            yield None
            return
        async with self.engine.begin() as connection:
            if work_id is not None:
                await connection.execute(select(work_handles.c.id).where(
                    work_handles.c.id == work_id
                ).with_for_update())
            yield self.admission(principal)
