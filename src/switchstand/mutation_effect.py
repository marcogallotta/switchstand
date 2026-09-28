"""Shared durable-effect lifecycle for scalar updates and relation mutations."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from .core import ProviderError
from .grant_state import EffectRecord, GrantState
from .grants import GuardOutcome, PrincipalContext, WorkGrant


class MutationRequest(Protocol):
    operation_id: UUID
    work_id: UUID
    grant_version: int


Guard = Callable[[str, str, bool], GuardOutcome]
Reconcile = Callable[[EffectRecord], Awaitable[GuardOutcome]]


@dataclass(frozen=True)
class PreparedMutation:
    intent: dict[str, object]
    send: Callable[[], Awaitable[GuardOutcome]]


Prepare = Callable[[WorkGrant, str], Awaitable[PreparedMutation | GuardOutcome]]


async def run_update_or_relation(
    grants: GrantState,
    principal: PrincipalContext,
    request: MutationRequest,
    fingerprint: str,
    operation: Literal["work_update", "work_relate"],
    qualification_field: Literal["update_qualification", "relation_qualification"],
    qualification_denial: str,
    guard: Guard,
    prepare: Prepare,
    reconcile: Reconcile,
) -> GuardOutcome:
    """Own the common journal/admission/send lifecycle; callers own mutation semantics."""
    possible_send = False
    history_known = False
    try:
        async with grants.locked(principal.key, request.work_id) as grant:
            record = await grants.exact(request.operation_id)
            history_known = True
            if record is not None:
                possible_send = record.outcome.effect != "not_sent"
                if record.principal_key != principal.key or record.fingerprint != fingerprint:
                    return guard("denied", "operation_identity_conflict", False)
                if record.outcome.effect != "unknown":
                    return record.outcome
                return await reconcile(record)

            blocked = await grants.previous(request.operation_id, request.work_id)
            if blocked is not None:
                return guard("unknown", "target_has_unresolved_effect", True)
            if grant is None or grant.principal != principal or not grant.current():
                return guard("denied", "no_current_grant", False)
            if not grant.can_write(request.work_id) or operation not in grant.operations:
                return guard("denied", "operation_or_work_not_granted", False)
            if request.grant_version != grant.version:
                return guard("stale", "grant_version_changed", False)

            qualification = getattr(grant, qualification_field)
            if (
                qualification is None
                or (principal.assurance == "test") != qualification.startswith("test:")
            ):
                return guard("denied", qualification_denial, False)

            prepared = await prepare(grant, qualification)
            if isinstance(prepared, GuardOutcome):
                return prepared
            unknown = guard("unknown", "prepared_or_unconfirmed_send", True)
            await grants.prepare(prepared.intent, grant, fingerprint, unknown)
            possible_send = True
            outcome = (
                guard("not_applied", "grant_expired_before_send", False)
                if not grant.current()
                else await prepared.send()
            )
            await grants.finish(outcome)
            return outcome
    except (SQLAlchemyError, ProviderError, TypeError, ValueError, KeyError):
        return guard(
            "unknown", "state_or_effect_unavailable", possible_send or not history_known
        )
