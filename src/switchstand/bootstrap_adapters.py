"""Read-only production adapters for bounded pre-migration bootstrap admission."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from .bootstrap_identity import MigrationReceipt, ProviderTaskStatus
from .core import Handle
from .grant_state import GrantState
from .grants import WorkGrant
from .provider import AsanaProvider
from .state import PostgresState, work_migration_receipts


class PostgresIdentityMapping:
    def __init__(self, state: PostgresState):
        self.state = state

    async def get(self, work_id: UUID) -> Handle | None:
        return await self.state.get(work_id)


class ExistingGrantAdapter:
    def __init__(self, grants: GrantState):
        self.grants = grants

    async def for_active_work(self, work_id: UUID) -> tuple[WorkGrant, ...]:
        return await self.grants.for_active_work(work_id)


class ExactAsanaTaskProbe:
    def __init__(self, provider: AsanaProvider):
        self.provider = provider

    async def status(self, provider_work_id: str) -> ProviderTaskStatus:
        task = await self.provider.source_task(provider_work_id)
        return ProviderTaskStatus(
            provider_work_id,
            exists=task is not None,
            current=task is not None and task.canonical and not task.completed,
        )


class PostgresMigrationReceiptReader:
    """Read the server-owned migration cutoff from canonical state."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def read(self, name: str) -> MigrationReceipt | None:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(work_migration_receipts).where(
                work_migration_receipts.c.name == name
            ))).mappings().all()
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]["source_digest"]) != 64:
            return MigrationReceipt(name=name, authenticated=False, complete=False)
        return MigrationReceipt(name=name, authenticated=True, complete=True)
