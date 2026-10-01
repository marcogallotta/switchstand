"""Provider-neutral recovery of an existing durable UNKNOWN effect."""

from typing import cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .core import ProviderError
from .grant_state import EffectRecord, GrantState
from .grants import (
    EffectInspection,
    EffectIntentView,
    EffectOutcomeView,
    EffectRecoveryResult,
    GuardOutcome,
    PrincipalContext,
    ProtectedUpdate,
    WorkGrant,
)
from .updates import UpdateGateway


def outcome_view(outcome: GuardOutcome) -> EffectOutcomeView:
    return EffectOutcomeView(
        status=outcome.status,
        reason=outcome.reason,
        effect=outcome.effect,
        retry=outcome.retry,
    )


class EffectRecovery:
    """Reconcile stored scalar-update intent without accepting a replacement payload."""

    def __init__(
        self, grants: GrantState, updates: UpdateGateway,
    ):
        self.grants, self.updates = grants, updates

    @staticmethod
    def denied(reason: str) -> EffectRecoveryResult:
        return EffectRecoveryResult(status="denied", reason=reason)

    @staticmethod
    def unavailable() -> EffectRecoveryResult:
        return EffectRecoveryResult(status="unknown", reason="effect_recovery_unavailable")

    @staticmethod
    def _authorized(
        principal: PrincipalContext, grant: WorkGrant | None, record: EffectRecord,
        operation: str, work_id: UUID,
    ) -> bool:
        return bool(
            grant is not None
            and grant.principal == principal
            and grant.current()
            and grant.can_write(work_id)
            and operation in grant.operations
            and record.principal_key == principal.key
        )

    @staticmethod
    def _request(record: EffectRecord) -> ProtectedUpdate:
        raw = record.intent.get("request")
        if not isinstance(raw, dict):
            raise TypeError("durable effect request missing")
        raw_values = cast(dict[object, object], raw)
        if record.outcome.operation == "work_update":
            if any(not isinstance(name, str) for name in raw_values):
                raise TypeError("durable update request invalid")
            values = {cast(str, name): value for name, value in raw_values.items()}
            patch = values.get("patch")
            if not isinstance(patch, dict):
                raise TypeError("durable update patch missing")
            raw_patch = cast(dict[object, object], patch)
            if any(not isinstance(name, str) for name in raw_patch):
                raise TypeError("durable update patch invalid")
            values["patch"] = {
                cast(str, name): value
                for name, value in raw_patch.items() if value is not None
            }
            request = ProtectedUpdate.model_validate(values)
        else:
            raise ValueError("effect recovery operation unsupported")
        if (
            request.operation_id != record.outcome.operation_id
            or request.work_id != record.outcome.work_id
        ):
            raise ValueError("durable effect identity invalid")
        return request

    @staticmethod
    def _inspection(
        record: EffectRecord, request: ProtectedUpdate,
        current: GuardOutcome,
    ) -> EffectInspection:
        readback = (
            "matched" if current.effect == "applied"
            else "unavailable" if current.reason in {
                "effect_recovery_unavailable", "state_or_effect_unavailable",
            }
            else "unconfirmed"
        )
        return EffectInspection(
            operation="work_update",
            operation_id=request.operation_id,
            work_id=request.work_id,
            intent=EffectIntentView(
                observed_revision=request.observed_revision,
                patch=request.patch,
            ),
            original_outcome=outcome_view(record.outcome),
            current_outcome=outcome_view(current),
            readback=readback,
        )

    async def reconcile(
        self, principal: PrincipalContext, operation_id: UUID,
    ) -> EffectRecoveryResult:
        try:
            initial = await self.grants.exact(operation_id)
            if initial is None or initial.outcome.work_id is None:
                return self.denied("effect_not_found")
            work_id = initial.outcome.work_id
            async with self.grants.locked(principal.key, work_id) as grant:
                record = await self.grants.exact(operation_id)
                if record is None or record.outcome.work_id != work_id:
                    return self.denied("effect_not_found")
                operation = record.outcome.operation
                if not self._authorized(principal, grant, record, operation, work_id):
                    return self.denied("effect_recovery_not_granted")
                if operation != "work_update":
                    return self.denied("effect_recovery_not_supported")
                try:
                    request = self._request(record)
                except (TypeError, ValueError, ValidationError):
                    return self.denied("stored_effect_intent_invalid")
                if record.outcome.effect == "applied":
                    return EffectRecoveryResult(
                        status="ok", reason=record.outcome.reason,
                        inspection=self._inspection(record, request, record.outcome),
                    )
                if record.outcome.effect != "unknown":
                    return self.denied("effect_not_unresolved")
                try:
                    current = await self.updates.reconcile_record(principal, request, record)
                except (SQLAlchemyError, ProviderError, TypeError, ValueError, KeyError):
                    current = GuardOutcome(
                        status="unknown", operation=record.outcome.operation,
                        work_id=work_id, operation_id=operation_id,
                        reason="effect_recovery_unavailable", effect="unknown",
                        retry="reconcile",
                        next_action="Retry this OperationId after recovery becomes available.",
                    )
                inspection = self._inspection(record, request, current)
                return EffectRecoveryResult(
                    status="ok" if current.effect == "applied" else "unknown",
                    reason=current.reason,
                    inspection=inspection,
                )
        except (SQLAlchemyError, ProviderError, TypeError, ValueError, KeyError):
            return self.unavailable()
