"""One durable append gateway, shared independently of the calling MCP surface."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .canonical_work import canonical_revision, canonical_work
from .core import (
    Provider,
    ProviderError,
    State,
    UnknownEffect,
    observed_revision_matches,
    provider_rejection_reason,
)
from .grant_state import GrantState, effect_intents
from .grants import (
    EffectBlocker,
    EffectOutcomeView,
    EffectReceipt,
    GuardOutcome,
    PrincipalContext,
    ProtectedAppend,
    WorkGrant,
)
from .mutation_effect import blocked_effect_next_action
from .work_events import OperationConflictError, StaleWorkVersion, WorkEventRepository


class AppendGateway:
    def __init__(self, state: State, grants: GrantState, providers: dict[str, Provider]):
        self.state, self.grants, self.providers = state, grants, providers

    @staticmethod
    def fingerprint(principal: PrincipalContext, request: ProtectedAppend) -> str:
        payload = [principal.key, str(request.work_id), request.grant_version,
                   request.observed_revision, request.text]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def guard(
        request: ProtectedAppend, status: str, reason: str, *, possible_send: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome.model_validate({'status': status, 'operation': "work_append", 'work_id': request.work_id, 'operation_id': request.operation_id, 'reason': reason, 'effect': "unknown" if possible_send else "not_sent", 'retry': "reconcile" if possible_send else "refresh" if status == "stale" else "none", 'next_action': "Reconcile the recorded effect; do not send a new operation."
                         if possible_send else "Refresh work/grant or ask the trusted issuer."})

    @staticmethod
    def admitted(principal: PrincipalContext, grant: WorkGrant | None) -> bool:
        return grant is not None and grant.principal == principal and grant.current()

    async def append(self, principal: PrincipalContext, request: ProtectedAppend) -> GuardOutcome:
        possible_send = False
        history_known = False
        try:
            async with self.grants.locked(principal.key, request.work_id) as grant:
                if not self.admitted(principal, grant) or grant is None:
                    return self.guard(request, "denied", "no_current_grant")
                if (not grant.can_write(request.work_id)
                        or "work_append" not in grant.operations):
                    return self.guard(request, "denied", "operation_or_work_not_granted")
                if request.grant_version != grant.version:
                    return self.guard(request, "stale", "grant_version_changed")
                qualification = grant.append_qualification
                if (qualification is None
                        or (principal.assurance == "test") != qualification.startswith("test:")):
                    return self.guard(request, "denied", "append_not_qualified_for_this_surface")
                fingerprint = self.fingerprint(principal, request)
                previous = await self.grants.previous(
                    request.operation_id, request.work_id,
                )
                history_known = True
                if previous is not None:
                    owner, previous_fingerprint, outcome = previous
                    # The context manager can fail while releasing its transaction,
                    # even after this branch chooses a previously recorded result.
                    possible_send = outcome.effect != "not_sent"
                    if owner == principal.key and previous_fingerprint == fingerprint:
                        return outcome
                    if outcome.operation_id == request.operation_id:
                        return self.guard(request, "denied", "operation_identity_conflict")
                    assert outcome.operation_id is not None and outcome.work_id is not None
                    return self.guard(
                        request, "unknown", "target_has_unresolved_effect",
                    ).model_copy(update={
                        "next_action": blocked_effect_next_action(outcome.operation),
                        "blocked_by": EffectBlocker(
                            operation=outcome.operation,
                            operation_id=outcome.operation_id,
                            work_id=outcome.work_id,
                            outcome=EffectOutcomeView(
                                status=outcome.status, reason=outcome.reason,
                                effect=outcome.effect, retry=outcome.retry,
                            ),
                        ),
                    })
                handle = await self.state.get(request.work_id)
                if handle is None:
                    return self.guard(request, "denied", "work_not_bound")
                provider = self.providers[handle.provider]
                current = await provider.source_task(handle.provider_work_id)
                if current is None:
                    return self.guard(request, "not_applied", "source_read_unavailable")
                if not current.canonical:
                    return self.guard(request, "denied", "source_not_canonical")
                if current.completed:
                    return self.guard(request, "denied", "work_is_terminal")
                if not await observed_revision_matches(
                    self.state, request.work_id, request.observed_revision, current.revision,
                    provider_notes=current.notes, provider_context=current.context,
                ):
                    return self.guard(request, "stale", "source_revision_changed")
                unknown = self.guard(request, "unknown", "prepared_or_unconfirmed_send",
                                     possible_send=True)
                await self.grants.prepare({
                    "request": request.model_dump(mode="json"),
                    "provider": handle.provider, "task_gid": handle.provider_work_id,
                    "qualification": qualification,
                }, grant, fingerprint, unknown)
                possible_send = True
                if not grant.current():
                    outcome = self.guard(request, "not_applied", "grant_expired_before_send")
                else:
                    outcome = await self._send(principal, grant, request, handle.provider,
                                               handle.provider_work_id, provider, qualification)
                await self.grants.finish(outcome)
                return outcome
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            # A prepared intent survives cancellations/crashes too. Its UNKNOWN
            # remains the durable barrier until an exact trusted reconciliation.
            return self.guard(request, "unknown", "state_or_effect_unavailable",
                              possible_send=possible_send or not history_known)

    async def _send(
        self, principal: PrincipalContext, grant: WorkGrant, request: ProtectedAppend,
        provider_name: str, task_gid: str, provider: Provider, qualification: str,
    ) -> GuardOutcome:
        try:
            story_gid = await provider.append(task_gid, request.text)
        except UnknownEffect:
            return self.guard(request, "unknown", "ambiguous_provider_send", possible_send=True)
        except ProviderError as error:
            return self.guard(request, "not_applied", provider_rejection_reason(error))
        if story_gid is not None:
            story = await provider.source_story(task_gid, story_gid)
            task = await provider.source_task(task_gid)
            if (story is not None and story.story_gid == story_gid and story.task_gid == task_gid
                    and story.text == request.text and task is not None and task.canonical):
                receipt = EffectReceipt(
                    operation_id=request.operation_id, principal=principal,
                    grant_id=grant.id, grant_version=grant.version, work_id=request.work_id,
                    provider=provider_name, task_gid=task_gid, story_gid=story_gid,
                    text=request.text, qualification=qualification,
                )
                return GuardOutcome(
                    status="ok", operation="work_append", work_id=request.work_id,
                    operation_id=request.operation_id, reason="exact_effect_verified",
                    effect="applied", retry="none", next_action="Use the recorded receipt.",
                    receipt=receipt,
                )
        return self.guard(request, "unknown", "effect_readback_unconfirmed", possible_send=True)


class CanonicalAppendGateway:
    """DB-native append owner; event, revision, and receipt commit atomically."""

    def __init__(self, grants: GrantState, events: WorkEventRepository):
        self.grants, self.events = grants, events

    async def append(self, principal: PrincipalContext, request: ProtectedAppend) -> GuardOutcome:
        fingerprint = AppendGateway.fingerprint(principal, request)
        try:
            async with self.grants.locked(principal.key) as grant:
                if not AppendGateway.admitted(principal, grant) or grant is None:
                    return AppendGateway.guard(request, "denied", "no_current_grant")
                if (not grant.can_write(request.work_id)
                        or "work_append" not in grant.operations):
                    return AppendGateway.guard(
                        request, "denied", "operation_or_work_not_granted"
                    )
                if request.grant_version != grant.version:
                    return AppendGateway.guard(request, "stale", "grant_version_changed")
                qualification = grant.append_qualification
                if (qualification is None or (principal.assurance == "test")
                        != qualification.startswith("test:")):
                    return AppendGateway.guard(
                        request, "denied", "append_not_qualified_for_this_surface"
                    )
                async with self.grants.engine.begin() as connection:
                    current = (await connection.execute(select(
                        canonical_work.c.row_version, canonical_work.c.completed,
                    ).where(canonical_work.c.work_id == request.work_id).with_for_update())).one_or_none()
                    if current is None:
                        return AppendGateway.guard(request, "denied", "work_not_bound")
                    exact = (await connection.execute(select(effect_intents).where(
                        effect_intents.c.operation_id == str(request.operation_id)
                    ))).mappings().one_or_none()
                    if exact is not None:
                        if (exact["principal_key"] != principal.key
                                or exact["fingerprint"] != fingerprint):
                            return AppendGateway.guard(
                                request, "denied", "operation_identity_conflict"
                            )
                        return GuardOutcome.model_validate(exact["outcome"])
                    blocked = (await connection.execute(select(effect_intents).where(
                        (effect_intents.c.work_id == str(request.work_id))
                        & (effect_intents.c.outcome["effect"].astext == "unknown")
                    ).limit(1))).mappings().one_or_none()
                    if blocked is not None:
                        prior = GuardOutcome.model_validate(blocked["outcome"])
                        assert prior.operation_id is not None and prior.work_id is not None
                        return AppendGateway.guard(
                            request, "unknown", "target_has_unresolved_effect"
                        ).model_copy(update={
                            "next_action": blocked_effect_next_action(prior.operation),
                            "blocked_by": EffectBlocker(
                                operation=prior.operation, operation_id=prior.operation_id,
                                work_id=prior.work_id, outcome=EffectOutcomeView(
                                    status=prior.status, reason=prior.reason,
                                    effect=prior.effect, retry=prior.retry,
                                ),
                            ),
                        })
                    if current.completed:
                        return AppendGateway.guard(request, "denied", "work_is_terminal")
                    if request.observed_revision != canonical_revision(
                        request.work_id, current.row_version
                    ):
                        return AppendGateway.guard(
                            request, "stale", "source_revision_changed"
                        )
                    appended = await self.events.append_locked(
                        connection, request.work_id, observed_version=current.row_version,
                        operation_id=request.operation_id, subtype="comment_added",
                        text=request.text, created_at=datetime.now(UTC), actor=principal.subject,
                    )
                    receipt = EffectReceipt(
                        operation_id=request.operation_id, principal=principal,
                        grant_id=grant.id, grant_version=grant.version,
                        work_id=request.work_id, provider="postgres",
                        task_gid=str(request.work_id), story_gid=str(appended.event.id),
                        text=request.text, qualification=qualification,
                    )
                    outcome = GuardOutcome(
                        status="ok", operation="work_append", work_id=request.work_id,
                        operation_id=request.operation_id, reason="exact_effect_verified",
                        effect="applied", retry="none",
                        next_action="Use the recorded receipt.", receipt=receipt,
                    )
                    await connection.execute(insert(effect_intents).values(
                        operation_id=str(request.operation_id), fingerprint=fingerprint,
                        principal_key=principal.key, work_id=str(request.work_id),
                        grant_id=str(grant.id), grant_version=grant.version,
                        intent={"request": request.model_dump(mode="json"),
                                "authority": "postgres"},
                        outcome=outcome.model_dump(mode="json", exclude_none=True),
                    ))
                    return outcome
        except IntegrityError:
            return AppendGateway.guard(request, "denied", "operation_identity_conflict")
        except StaleWorkVersion:
            return AppendGateway.guard(request, "stale", "source_revision_changed")
        except (OperationConflictError, TypeError, ValueError, KeyError):
            return AppendGateway.guard(request, "denied", "invalid_database_append")
        except SQLAlchemyError:
            return AppendGateway.guard(
                request, "unknown", "state_or_effect_unavailable", possible_send=True
            )
