"""Append-only storage and authenticated server facade for activation continuity."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .activation_continuity import (
    ActivationContract,
    ContinuityResult,
    Obligation,
    RuntimeBinding,
    TechnicalBasis,
    TransitionIntent,
    TransitionProof,
    actor_ref,
    advance_obligation,
    authorized,
    intent_digest,
    open_obligation,
    seal_obligation,
)
from .grants import PrincipalContext, WorkGrant


@dataclass(frozen=True)
class ActivationContinuity:
    """Server-owned creator and transition facade. Construction is the feature gate."""

    engine: AsyncEngine
    contracts: Mapping[UUID, ActivationContract]

    async def _rows(self, obligation_id: UUID) -> list[dict[str, object]]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT operation_id,generation,record FROM activation_obligation_revisions "
                            "WHERE obligation_id=:id ORDER BY generation"
                        ),
                        {"id": obligation_id},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    @staticmethod
    def _chain(rows: list[dict[str, object]]) -> list[Obligation] | None:
        try:
            values = [Obligation.model_validate(row["record"]) for row in rows]
            previous = None
            for generation, (row, value) in enumerate(zip(rows, values, strict=True), 1):
                if (
                    value.generation,
                    value.predecessor,
                    value.operation_id,
                    seal_obligation(value).digest,
                ) != (
                    generation,
                    previous,
                    row["operation_id"],
                    value.digest,
                ):
                    return None
                previous = value.digest
            return values
        except KeyError, TypeError, ValueError:
            return None

    async def get(self, obligation_id: UUID) -> ContinuityResult:
        try:
            chain = self._chain(await self._rows(obligation_id))
        except SQLAlchemyError:
            return ContinuityResult(status="UNKNOWN", reason="state_unavailable")
        if chain is None:
            return ContinuityResult(status="UNKNOWN", reason="corrupt_revision_chain")
        if not chain:
            return ContinuityResult(status="MISSING", reason="obligation_missing")
        return ContinuityResult(status="CURRENT", obligation=chain[-1])

    async def for_owner(
        self, owner_work_id: UUID,
    ) -> tuple[Obligation, ...] | Literal["UNKNOWN"]:
        try:
            async with self.engine.connect() as connection:
                rows = (await connection.execute(text(
                    "SELECT obligation_id,operation_id,generation,record "
                    "FROM activation_obligation_revisions "
                    "WHERE record->'binding'->>'return_owner_work_id'=:owner "
                    "ORDER BY obligation_id,generation"
                ), {"owner": str(owner_work_id)})).mappings().all()
            grouped: dict[UUID, list[dict[str, object]]] = {}
            for row in rows:
                grouped.setdefault(row["obligation_id"], []).append(dict(row))
            chains = [self._chain(values) for values in grouped.values()]
            if any(chain is None for chain in chains):
                return "UNKNOWN"
            return tuple(chain[-1] for chain in chains if chain)
        except (SQLAlchemyError, KeyError, TypeError, ValueError):
            return "UNKNOWN"

    async def transition(
        self,
        principal: PrincipalContext,
        grant: WorkGrant,
        runtime: RuntimeBinding,
        intent: TransitionIntent,
        technical: TechnicalBasis | None = None,
        proof: TransitionProof | None = None,
    ) -> ContinuityResult:
        contract = self.contracts.get(intent.obligation_id)
        if contract is None:
            return ContinuityResult(status="DENIED", reason="contract_not_installed")
        if grant.principal != principal or not grant.current():
            return ContinuityResult(status="STALE", reason="actor_binding_not_current")
        if (
            runtime.currentness != "CURRENT"
            or runtime.actor_work_id != grant.authority.active_work_id
        ):
            return ContinuityResult(status="STALE", reason="runtime_binding_not_current")
        if not authorized(contract, runtime.actor_work_id, intent.transition):
            return ContinuityResult(status="DENIED", reason="transition_not_authorized")
        actor = actor_ref(principal, grant, runtime)
        try:
            async with self.engine.begin() as connection:
                rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT operation_id,generation,record FROM activation_obligation_revisions "
                                "WHERE obligation_id=:id ORDER BY generation FOR UPDATE"
                            ),
                            {"id": intent.obligation_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                chain = self._chain([dict(row) for row in rows])
                if chain is None:
                    return ContinuityResult(status="UNKNOWN", reason="corrupt_revision_chain")
                if chain and chain[-1].binding != contract.stored():
                    return ContinuityResult(status="CONFLICT", reason="stored_binding_mismatch")
                decision = (
                    open_obligation(
                        contract, intent, actor, runtime.binding_token, technical
                    )
                    if not chain
                    else advance_obligation(
                        contract, chain[-1], intent, actor,
                        runtime.binding_token, technical, proof,
                    )
                )
                if decision.status != "APPLIED" or decision.obligation is None:
                    return decision
                value = decision.obligation
                await connection.execute(
                    text(
                        "INSERT INTO activation_obligation_revisions "
                        "(obligation_id,operation_id,generation,record) "
                        "VALUES (:id,:op,:gen,CAST(:record AS jsonb))"
                    ),
                    {
                        "id": intent.obligation_id,
                        "op": value.operation_id,
                        "gen": value.generation,
                        "record": value.model_dump_json(),
                    },
                )
                return decision
        except SQLAlchemyError:
            replay = await self.get(intent.obligation_id)
            if (
                replay.obligation is not None
                and replay.obligation.operation_id == intent.operation_id
            ):
                status = (
                    "REPLAYED"
                    if replay.obligation.intent_digest == intent_digest(intent, actor)
                    else "CONFLICT"
                )
                return replay.model_copy(update={"status": status})
            if replay.obligation is not None:
                return replay.model_copy(update={"status": "STALE", "reason": "revision_changed"})
            return ContinuityResult(status="UNKNOWN", reason="state_unavailable")
