"""Additive grant/intent storage; issuance and reconciliation are trusted host APIs."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import Column, Integer, Table, Text, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .grants import GuardOutcome, WorkGrant
from .state import metadata, work_handles

work_grants = Table(
    "work_grants", metadata,
    Column("principal_key", Text, primary_key=True),
    Column("document", JSONB, nullable=True),
)
effect_intents = Table(
    "effect_intents", metadata,
    Column("operation_id", Text, primary_key=True),
    Column("fingerprint", Text, nullable=False),
    Column("principal_key", Text, nullable=False),
    Column("work_id", Text, nullable=False, index=True),
    Column("grant_id", Text, nullable=False),
    Column("grant_version", Integer, nullable=False),
    Column("intent", JSONB, nullable=False),
    Column("outcome", JSONB, nullable=False),
)


class GrantState:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def current(self, principal_key: str) -> WorkGrant | None:
        async with self.engine.connect() as connection:
            value = (await connection.execute(select(work_grants.c.document).where(
                work_grants.c.principal_key == principal_key
            ))).scalar_one_or_none()
        return None if value is None else WorkGrant.model_validate(value)

    async def issue(self, grant: WorkGrant, expected_version: int | None) -> None:
        """Trusted control path only; compare-and-replace also handles revoke/terminal."""
        key = grant.principal.key
        async with self.engine.begin() as connection:
            await connection.execute(insert(work_grants).values(
                principal_key=key, document=None
            ).on_conflict_do_nothing())
            value = (await connection.execute(select(work_grants.c.document).where(
                work_grants.c.principal_key == key
            ).with_for_update())).scalar_one()
            old = None if value is None else WorkGrant.model_validate(value)
            if (None if old is None else old.version) != expected_version:
                raise ValueError("stale grant issuance")
            if grant.version != (expected_version or 0) + 1:
                raise ValueError("grant version must advance exactly once")
            await connection.execute(update(work_grants).where(
                work_grants.c.principal_key == key
            ).values(document=grant.model_dump(mode="json")))

    @asynccontextmanager
    async def locked(
        self, principal_key: str, work_id: UUID | None = None,
    ) -> AsyncGenerator[WorkGrant | None]:
        async with self.engine.begin() as connection:
            value = (await connection.execute(select(work_grants.c.document).where(
                work_grants.c.principal_key == principal_key
            ).with_for_update())).scalar_one_or_none()
            grant = None if value is None else WorkGrant.model_validate(value)
            if work_id is not None:
                await connection.execute(select(work_handles.c.id).where(
                    work_handles.c.id == work_id
                ).with_for_update())
            yield grant

    async def previous(
        self, operation_id: UUID, work_id: UUID,
    ) -> tuple[str, str, GuardOutcome] | None:
        async with self.engine.connect() as connection:
            # Called under the work lock: unresolved sends block the entire target,
            # including changed payloads, principals or new OperationIds.
            rows = (await connection.execute(select(effect_intents).where(
                (effect_intents.c.operation_id == str(operation_id))
                | ((effect_intents.c.work_id == str(work_id))
                   & (effect_intents.c.outcome["effect"].astext == "unknown"))
            ))).mappings().all()
        if not rows:
            return None
        row = next((r for r in rows if r["operation_id"] == str(operation_id)), rows[0])
        return str(row["principal_key"]), str(row["fingerprint"]), GuardOutcome.model_validate(
            row["outcome"]
        )

    async def prepare(
        self, request: dict[str, object], grant: WorkGrant, fingerprint: str,
        unknown: GuardOutcome,
    ) -> None:
        # Separate transaction: intent survives a crash/rollback of the held locks.
        # No FK to those locked rows: a FK insertion could wait on our own locks.
        async with self.engine.begin() as connection:
            await connection.execute(insert(effect_intents).values(
                operation_id=str(unknown.operation_id), fingerprint=fingerprint,
                principal_key=grant.principal.key, work_id=str(unknown.work_id),
                grant_id=str(grant.id), grant_version=grant.version,
                intent=request, outcome=unknown.model_dump(mode="json"),
            ))

    async def finish(self, outcome: GuardOutcome) -> None:
        async with self.engine.begin() as connection:
            result = await connection.execute(update(effect_intents).where(
                effect_intents.c.operation_id == str(outcome.operation_id)
            ).values(outcome=outcome.model_dump(mode="json")).returning(
                effect_intents.c.operation_id
            ))
            if result.scalar_one_or_none() is None:
                raise ValueError("effect intent missing")
