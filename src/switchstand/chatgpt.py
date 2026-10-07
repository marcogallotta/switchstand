"""Authenticated-caller seam; authentication adapters are trusted host code, never tools."""

import hashlib
from collections.abc import Awaitable, Callable
from typing import Literal, cast
from uuid import UUID, uuid5

from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from .activation_continuity import (
    ContinuityResult,
    RuntimeBinding,
    TechnicalBasis,
    Transition,
    TransitionIntent,
    TransitionProof,
)
from .activation_continuity_store import ActivationContinuity
from .canonical_event_reads import CanonicalEventReader
from .canonical_work_runtime import CanonicalWorkRuntime
from .contracts import (
    ClosedModel,
    LaunchAuthority,
    WorkEventRequest,
    WorkEventResult,
    WorkGetRequest,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkResolveReferenceRequest,
    WorkSearchRequest,
    WorkSearchResult,
)
from .core import (
    Controller,
    Provider,
    ProviderError,
    State,
)
from .creates import CreateGateway
from .discovery import DiscoveryProvider, WorkDiscovery
from .effects import AppendGateway, CanonicalAppendGateway
from .grant_state import GrantState
from .grants import (
    GrantedWorkResult,
    GrantResult,
    GuardOutcome,
    PrincipalContext,
    ProtectedAppend,
    ProtectedCreate,
    ProtectedRelation,
    ProtectedUpdate,
    ScalarPatch,
    UpdateReceipt,
    WorkGrant,
)
from .implementation_requests import ImplementationRequestResult, ImplementationRequestState
from .lifecycle import LifecycleEvent, ProfileState, RequiredResultPersistence
from .messages import (
    MessagePendingRequest,
    MessagePendingResult,
    MessageSendRequest,
    MessageState,
    MessageSubmitResult,
    pending_messages,
    send_message,
)
from .outcome_state import OutcomeItem, OutcomeWrite
from .priority_claim_service import (
    HumanPriorityClear,
    HumanPrioritySet,
    PriorityClaimReadResult,
    PriorityClaimService,
)
from .priority_claims import SubjectKind
from .priority_context import PriorityContextProjection, PriorityContextResult
from .product_currentness import ProductCurrentness
from .relations import RelationGateway
from .reviews import ReviewService
from .task_control import (
    DurableControlCapsule,
    TaskControlCheckpointResult,
    TaskControlReadResult,
    TaskControlState,
)
from .task_ref import parse_legacy_task_reference
from .updates import UpdateGateway
from .workspace_admission import WorkspaceAdmissionState

PrincipalResolver = Callable[[], Awaitable[PrincipalContext | None]]
ActivationTechnicalResolver = Callable[
    [PrincipalContext, UUID], Awaitable[TechnicalBasis | None]
]
ActivationRuntimeResolver = Callable[
    [PrincipalContext, WorkGrant], Awaitable[RuntimeBinding | None]
]
ActivationProofResolver = Callable[
    [PrincipalContext, WorkGrant, TransitionIntent], Awaitable[TransitionProof | None]
]
REQUIRED_RESULT_NAMESPACE = UUID("12ddf4c9-f608-46b6-9150-3be7841e85da")
REQUIRED_RESULT_HEADING = "## Current required result"


def _required_result_notes(current: str, result: str) -> str:
    """Preserve current notes while promoting one controlling result."""
    block = f"{REQUIRED_RESULT_HEADING}\n\n{result}"
    promoted = f"{current.rstrip()}\n\n{block}" if current.strip() else block
    if len(promoted) > 8000:
        raise ValueError("required result does not fit without replacing current notes")
    return promoted


class RequiredResultSaveRequest(ClosedModel):
    api_version: Literal["1"]
    work_id: UUID
    grant_version: int = Field(ge=1)
    observed_revision: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=8000)


class ChatGPTService:
    def __init__(
        self, principal: PrincipalResolver, state: State,
        grants: GrantState, providers: dict[str, Provider], messages: MessageState | None = None,
        required_results: RequiredResultPersistence | None = None,
        ordinary_workspace_admission: bool = False,
        canonical_work: CanonicalWorkRuntime | None = None,
        canonical_events: CanonicalEventReader | None = None,
        task_control: TaskControlState | None = None,
        canonical_work_active: bool = False,
        outcome_state_enabled: bool = False,
        priority_claims: PriorityClaimService | None = None,
        priority_claims_enabled: bool = False,
        priority_context: PriorityContextProjection | None = None,
        priority_context_enabled: bool = False,
        implementation_requests: ImplementationRequestState | None = None,
        reviews: ReviewService | None = None,
        product_currentness: Callable[[PrincipalContext], Awaitable[ProductCurrentness]] | None = None,
        product_currentness_enabled: bool = False,
        activation_continuity: ActivationContinuity | None = None,
        activation_technical: ActivationTechnicalResolver | None = None,
        activation_runtime: ActivationRuntimeResolver | None = None,
        activation_proof: ActivationProofResolver | None = None,
    ):
        self.principal, self.state, self.grants, self.providers = principal, state, grants, providers
        self.admission_grants = (
            WorkspaceAdmissionState(
                grants.engine, principal, task_control_enabled=task_control is not None,
            )
            if ordinary_workspace_admission and type(grants) is GrantState
            else grants
        )
        self.gateway = AppendGateway(state, self.admission_grants, providers)
        self.canonical_append = (
            None if canonical_events is None
            else CanonicalAppendGateway(self.admission_grants, canonical_events.events)
        )
        self.create_gateway = CreateGateway(state, self.admission_grants, providers)
        self.update_gateway = UpdateGateway(state, self.admission_grants, providers)
        self.relation_gateway = RelationGateway(state, self.admission_grants, providers)
        self.messages = messages
        self.required_results = required_results
        self.canonical_work = canonical_work
        self.canonical_events = canonical_events
        self.task_control = task_control
        self.canonical_work_active = canonical_work_active
        self.outcome_state_enabled = outcome_state_enabled
        self.priority_claims = priority_claims
        self.priority_claims_enabled = priority_claims_enabled
        self.priority_context = priority_context
        self.priority_context_enabled = priority_context_enabled
        self.implementation_requests = implementation_requests
        self.reviews = reviews
        self.product_currentness = product_currentness
        self.product_currentness_enabled = product_currentness_enabled
        self.activation_continuity = activation_continuity
        self.activation_technical = activation_technical
        self.activation_runtime = activation_runtime
        self.activation_proof = activation_proof

    async def activation_continuity_transition(
        self, operation_id: UUID, obligation_id: UUID, observed_revision: str,
        transition: Transition, evidence_refs: tuple[str, ...],
        blocker_ref: str | None, clearing_event_ref: str | None,
    ) -> ContinuityResult:
        """Derive actor and technical evidence from authenticated server state."""
        if self.activation_continuity is None:
            return ContinuityResult(status="DENIED", reason="feature_default_off")
        principal = await self.principal()
        if principal is None:
            return ContinuityResult(status="DENIED", reason="authenticated_principal_required")
        intent = TransitionIntent(
            operation_id=operation_id, obligation_id=obligation_id,
            observed_revision=observed_revision, transition=transition,
            evidence_refs=evidence_refs, blocker_ref=blocker_ref,
            clearing_event_ref=clearing_event_ref,
        )
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                if grant is None or "activation_continuity" not in grant.operations:
                    return ContinuityResult(status="DENIED", reason="work_not_granted")
                if self.activation_runtime is None:
                    return ContinuityResult(status="UNKNOWN", reason="runtime_binding_unavailable")
                runtime = await self.activation_runtime(principal, grant)
                if runtime is None:
                    return ContinuityResult(status="UNKNOWN", reason="runtime_binding_unavailable")
                technical = (
                    None
                    if self.activation_technical is None
                    else await self.activation_technical(principal, obligation_id)
                )
                proof = None
                if transition.startswith("ACCEPTANCE_") or transition in {
                    "ADOPTION_ADOPTED", "CLEAR_BLOCKER",
                }:
                    if self.activation_proof is None:
                        return ContinuityResult(status="UNKNOWN", reason="proof_unavailable")
                    proof = await self.activation_proof(principal, grant, intent)
                    if proof is None:
                        return ContinuityResult(status="UNKNOWN", reason="proof_unavailable")
                return await self.activation_continuity.transition(
                    principal, grant, runtime, intent, technical, proof
                )
        except (SQLAlchemyError, ValueError, KeyError, RuntimeError, TypeError):
            return ContinuityResult(status="UNKNOWN", reason="admission_unavailable")

    async def implementation_request(
        self, operation_id: UUID, package_work_id: UUID, observed_revision: str,
    ) -> ImplementationRequestResult:
        """Admit one exact reviewed package without exposing server-owned intent fields."""
        if self.implementation_requests is None:
            return ImplementationRequestResult(status="DENIED", reason="feature_default_off")
        principal = await self.principal()
        if principal is None:
            return ImplementationRequestResult(
                status="DENIED", reason="authenticated_principal_required"
            )
        return await self.implementation_requests.request(
            principal, operation_id, package_work_id, observed_revision
        )

    @staticmethod
    def denied(
        operation: str,
        reason: str = "authenticated_principal_required",
        next_action: str = "Use the authenticated connection and trusted work issuer.",
    ) -> GuardOutcome:
        return GuardOutcome(status="denied", operation=operation, reason=reason,
                            next_action=next_action)

    @classmethod
    def _reference_denied(cls, reason: str, next_action: str | None = None) -> GrantedWorkResult:
        return GrantedWorkResult(
            status="denied",
            guard=cls.denied(
                "work_resolve_reference", reason,
                next_action or "Use the authenticated connection and trusted work issuer.",
            ),
        )

    async def grant_get(self) -> GrantResult:
        principal = await self.principal()
        if principal is None:
            return GrantResult(status="denied", guard=self.denied("grant_get"))
        try:
            grant = await self.grants.current(principal.key)
            if not self.gateway.admitted(principal, grant):
                return GrantResult(status="denied", principal=principal,
                                   guard=self.denied("grant_get", "no_current_grant"))
            return GrantResult(status="ok", principal=principal, grant=grant)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return GrantResult(status="unknown", principal=principal)

    async def admission_get(self) -> GrantResult:
        """Read server-owned ordinary workspace admission without consulting work_grants."""
        principal = await self.principal()
        if principal is None:
            return GrantResult(status="denied", guard=self.denied("workspace_admission"))
        try:
            grant = await self.admission_grants.current(principal.key)
            if not self.gateway.admitted(principal, grant):
                return GrantResult(
                    status="denied", principal=principal,
                    guard=self.denied("workspace_admission", "no_current_grant"),
                )
            return GrantResult(status="ok", principal=principal, grant=grant)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return GrantResult(status="unknown", principal=principal)

    async def _read_authority(
        self,
        principal: PrincipalContext,
        grant: WorkGrant | None,
        *,
        operations: frozenset[str],
        work_id: UUID | None = None,
        explicit_target: bool = False,
        workspace_only: bool = False,
    ) -> tuple[LaunchAuthority | None, str | None]:
        """Return the sole admitted read authority and a closed denial reason."""
        if not self.gateway.admitted(principal, grant) or grant is None:
            return None, "no_current_grant"
        if not operations <= set(grant.operations):
            return None, "work_not_granted"
        if workspace_only and grant.scope != "workspace":
            return None, "work_not_granted"
        if work_id is None:
            return grant.authority, None
        if await self.state.get(work_id) is None:
            return None, "work_not_granted"
        if grant.scope == "workspace":
            if not explicit_target:
                return None, "explicit_work_id_required"
            return LaunchAuthority(active_work_id=work_id), None
        if grant.can_read(work_id, explicit_target=explicit_target):
            return grant.authority, None
        if await self.admission_grants.created_work_allowed(principal.key, work_id):
            return LaunchAuthority(active_work_id=work_id), None
        return None, "work_not_granted"

    async def search(self, request: WorkSearchRequest) -> WorkSearchResult:
        principal = await self.principal()
        if principal is None:
            return WorkSearchResult(status="denied")
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                authority, _ = await self._read_authority(
                    principal, grant, operations=frozenset({"work_search"}),
                    workspace_only=True,
                )
                if authority is None:
                    return WorkSearchResult(status="denied")
                if self.canonical_work_active:
                    if self.canonical_work is None:
                        return WorkSearchResult(status="unknown")
                    return await self.canonical_work.search(request)
                provider = self.providers.get("asana")
                if provider is None:
                    return WorkSearchResult(status="provider_error")
                return await WorkDiscovery(
                    "asana", cast(DiscoveryProvider, provider), self.state
                ).search(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkSearchResult(status="unknown")

    async def resolve_reference(
        self, request: WorkResolveReferenceRequest,
    ) -> GrantedWorkResult:
        try:
            parsed = parse_legacy_task_reference(request.reference)
        except ValueError:
            return GrantedWorkResult(status="unknown")
        principal = await self.principal()
        if principal is None:
            return self._reference_denied("authenticated_principal_required")
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                base_authority, reason = await self._read_authority(
                    principal, grant, operations=frozenset({"work_get"})
                )
                if base_authority is None or grant is None:
                    return self._reference_denied(reason or "no_current_grant")
                handle = await self.state.get_by_provider(
                    parsed.provider, parsed.provider_work_id
                )
                if grant.scope == "launch":
                    if handle is None:
                        return self._reference_denied(
                            "reference_not_granted",
                            "Use a reference admitted by the current work grant.",
                        )
                    authority, reason = await self._read_authority(
                        principal, grant, operations=frozenset({"work_get"}),
                        work_id=handle.id, explicit_target=True,
                    )
                    if authority is None:
                        return self._reference_denied(reason or "work_not_granted")
                elif handle is None and self.canonical_work_active:
                    return GrantedWorkResult(status="unknown")
                elif handle is None:
                    provider = self.providers.get(parsed.provider)
                    if provider is None:
                        return GrantedWorkResult(status="provider_error")
                    work = await provider.get(parsed.provider_work_id)
                    if work is None:
                        return GrantedWorkResult(status="unknown")
                    if not work.canonical:
                        return self._reference_denied(
                            "reference_not_admitted",
                            "Use a reference admitted by the authenticated workspace.",
                        )
                    handle = await self.state.bind(parsed.provider, parsed.provider_work_id)
                authority = LaunchAuthority(active_work_id=handle.id)
                if self.canonical_work_active:
                    if self.canonical_work is None:
                        return GrantedWorkResult(status="unknown")
                    result = await self.canonical_work.get(handle.id)
                else:
                    result = await Controller(authority, self.state, self.providers).get(
                        WorkGetRequest(api_version="1", work_id=handle.id)
                    )
                if result.status == "denied":
                    return self._reference_denied(
                        "reference_not_admitted",
                        "Use a reference admitted by the authenticated workspace.",
                    )
                return GrantedWorkResult(status=result.status, item=result.item)
        except ProviderError:
            return GrantedWorkResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return GrantedWorkResult(status="unknown")

    async def get(self, work_id: UUID | None = None) -> GrantedWorkResult:
        principal = await self.principal()
        if principal is None:
            return GrantedWorkResult(status="denied", guard=self.denied("work_get"))
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                if grant is None:
                    return GrantedWorkResult(
                        status="denied", guard=self.denied("work_get", "no_current_grant")
                    )
                explicit_target = work_id is not None
                target = work_id or grant.authority.active_work_id
                authority, reason = await self._read_authority(
                    principal, grant, operations=frozenset({"work_get"}),
                    work_id=target, explicit_target=explicit_target,
                )
                if authority is None:
                    return GrantedWorkResult(
                        status="denied",
                        guard=self.denied("work_get", reason or "work_not_granted"),
                    )
                if self.canonical_work_active:
                    if self.canonical_work is None:
                        return GrantedWorkResult(status="unknown")
                    result = await self.canonical_work.get(target)
                else:
                    result = await Controller(authority, self.state, self.providers).get(
                        WorkGetRequest(api_version="1", work_id=target)
                    )
                return GrantedWorkResult(status=result.status, item=result.item)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return GrantedWorkResult(status="unknown")

    async def outcome_state_update(
        self, *, operation_id: UUID, owner_work_id: UUID,
        expected_state_id: UUID | None, owner_observed_revision: str,
        items: tuple[OutcomeItem, ...],
    ) -> OutcomeWrite:
        """Record one explicit owner's AGENT snapshot after ordinary semantic admission."""
        principal = await self.principal()
        if principal is None:
            return OutcomeWrite("DENIED")
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                authority, _ = await self._read_authority(
                    principal, grant, operations=frozenset({"work_get"}),
                    work_id=owner_work_id, explicit_target=True,
                )
                if authority is None:
                    return OutcomeWrite("DENIED")
                if self.canonical_work_active:
                    if self.canonical_work is None:
                        return OutcomeWrite("UNKNOWN")
                    current = await self.canonical_work.get(owner_work_id)
                else:
                    current = await Controller(authority, self.state, self.providers).get(
                        WorkGetRequest(api_version="1", work_id=owner_work_id)
                    )
                if current.status != "ok" or current.item is None:
                    return OutcomeWrite("UNKNOWN")
                if current.item.revision != owner_observed_revision:
                    return OutcomeWrite("STALE")
                outcomes = getattr(self.state, "outcomes", None)
                if outcomes is None:
                    return OutcomeWrite("UNKNOWN")
                return await outcomes.record(
                    owner_work_id=owner_work_id, active_work_id=owner_work_id,
                    operation_id=operation_id, expected_state_id=expected_state_id,
                    owner_currentness_token=current.item.revision, items=items,
                )
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return OutcomeWrite("UNKNOWN")

    async def priority_claim_get(
        self, subject_kind: SubjectKind, subject_id: UUID,
    ) -> PriorityClaimReadResult:
        principal = await self.principal()
        if principal is None or self.priority_claims is None:
            return PriorityClaimReadResult(status="denied", reason="claim_surface_unavailable")
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                if subject_kind == "WORK":
                    authority, reason = await self._read_authority(
                        principal, grant, operations=frozenset({"work_get"}),
                        work_id=subject_id, explicit_target=True,
                    )
                    if authority is None:
                        return PriorityClaimReadResult(
                            status="denied", reason=reason or "work_not_granted",
                        )
                elif (grant is None or grant.principal != principal or not grant.current()
                      or grant.scope != "workspace" or "work_get" not in grant.operations):
                    return PriorityClaimReadResult(
                        status="denied", reason="project_read_not_granted",
                    )
                return await self.priority_claims.current(subject_kind, subject_id)
        except (SQLAlchemyError, ProviderError, TypeError, ValueError, KeyError):
            return PriorityClaimReadResult(status="unknown", reason="claim_state_unavailable")

    async def priority_claim_record(
        self, request: HumanPrioritySet | HumanPriorityClear,
    ) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return PriorityClaimService.human_guard(
                request, "priority_claim_record", "denied",
                "authenticated_principal_required",
            )
        if self.priority_claims is None:
            return PriorityClaimService.human_guard(
                request, "priority_claim_record", "denied", "claim_surface_unavailable"
            )
        if isinstance(request, HumanPriorityClear):
            return await self.priority_claims.human_clear(
                self.admission_grants, principal, request
            )
        return await self.priority_claims.human_set(
            self.admission_grants, principal, request
        )

    async def priority_context_get(
        self, work_ids: tuple[UUID, ...],
    ) -> PriorityContextResult:
        principal = await self.principal()
        if principal is None or self.priority_context is None:
            return PriorityContextResult(
                status="denied", scope_complete=False,
                reason="priority_context_unavailable",
            )
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                for work_id in set(work_ids):
                    authority, reason = await self._read_authority(
                        principal, grant, operations=frozenset({"work_get"}),
                        work_id=work_id, explicit_target=True,
                    )
                    if authority is None:
                        return PriorityContextResult(
                            status="denied", scope_complete=False,
                            reason=reason or "work_not_granted",
                        )
                return await self.priority_context.project(work_ids)
        except (SQLAlchemyError, ProviderError, TypeError, ValueError, KeyError):
            return PriorityContextResult(
                status="unknown", scope_complete=False,
                reason="priority_context_state_unavailable",
            )

    async def history(self, request: WorkHistoryRequest) -> WorkHistoryResult:
        principal = await self.principal()
        if principal is None:
            return WorkHistoryResult(status="denied")
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                authority, _ = await self._read_authority(
                    principal, grant, operations=frozenset({"work_get"}),
                    work_id=request.work_id, explicit_target=True,
                )
                if authority is None:
                    return WorkHistoryResult(status="denied")
                if self.canonical_work_active:
                    if self.canonical_events is None:
                        return WorkHistoryResult(status="unknown")
                    return await self.canonical_events.history(request)
                return await Controller(authority, self.state, self.providers).history(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkHistoryResult(status="unknown")

    async def event(self, request: WorkEventRequest) -> WorkEventResult:
        principal = await self.principal()
        if principal is None:
            return WorkEventResult(status="denied")
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                authority, _ = await self._read_authority(
                    principal, grant, operations=frozenset({"work_get"}),
                    work_id=request.work_id, explicit_target=True,
                )
                if authority is None:
                    return WorkEventResult(status="denied")
                if self.canonical_work_active:
                    if self.canonical_events is None:
                        return WorkEventResult(status="unknown")
                    return await self.canonical_events.event(request)
                return await Controller(authority, self.state, self.providers).event(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkEventResult(status="unknown")

    async def append(self, request: ProtectedAppend) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.gateway.guard(request, "denied", "authenticated_principal_required")
        if self.canonical_work_active:
            if self.canonical_append is None:
                return self.gateway.guard(
                    request, "unknown", "canonical_events_unavailable", possible_send=False,
                )
            return await self.canonical_append.append(principal, request)
        return await self.gateway.append(principal, request)

    async def create(self, request: ProtectedCreate) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.create_gateway.guard(request, "denied", "authenticated_principal_required")
        if self.canonical_work_active:
            if self.canonical_work is None:
                return self.create_gateway.guard(
                    request, "unknown", "canonical_work_unavailable", possible_send=False,
                )
            return await self.canonical_work.protected_create(
                self.admission_grants, principal, request
            )
        return await self.create_gateway.create(principal, request)

    async def task_control_get(self, work_id: UUID) -> TaskControlReadResult:
        if self.task_control is None:
            return TaskControlReadResult(
                status="unknown", currentness="UNKNOWN", reason="feature_default_off",
            )
        principal = await self.principal()
        if principal is None:
            return TaskControlReadResult(
                status="denied", currentness="UNKNOWN",
                reason="authenticated_principal_required",
            )
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                if (
                    grant is None or grant.principal != principal or not grant.current()
                    or "work_get" not in grant.operations
                    or not grant.can_read(work_id, explicit_target=True)
                ):
                    return TaskControlReadResult(
                        status="denied", currentness="UNKNOWN", reason="work_not_granted",
                    )
                return await self.task_control.read(work_id)
        except SQLAlchemyError:
            return TaskControlReadResult(
                status="unknown", currentness="UNKNOWN", reason="state_unavailable",
            )

    async def task_control_checkpoint(
        self, operation_id: UUID, work_id: UUID, observed_work_revision: str,
        expected_checkpoint_generation: int | None, capsule: DurableControlCapsule,
    ) -> TaskControlCheckpointResult:
        if self.task_control is None:
            return TaskControlCheckpointResult(
                status="unknown", currentness="UNKNOWN", reason="feature_default_off",
            )
        principal = await self.principal()
        if principal is None:
            return TaskControlCheckpointResult(
                status="denied", currentness="UNKNOWN",
                reason="authenticated_principal_required",
            )
        try:
            async with self.admission_grants.locked(principal.key) as grant:
                if grant is None or grant.principal != principal or not grant.current():
                    return TaskControlCheckpointResult(
                        status="denied", currentness="UNKNOWN", reason="no_current_grant",
                    )
                return await self.task_control.checkpoint(
                    principal, grant, operation_id, work_id, observed_work_revision,
                    expected_checkpoint_generation, capsule,
                )
        except SQLAlchemyError:
            return TaskControlCheckpointResult(
                status="unknown", currentness="UNKNOWN", reason="state_unavailable",
            )

    async def update(self, request: ProtectedUpdate) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.update_gateway.guard(request, "denied", "authenticated_principal_required")
        if self.canonical_work_active:
            if self.canonical_work is None:
                return self.update_gateway.guard(
                    request, "unknown", "canonical_work_unavailable", possible_send=False,
                )
            return await self.canonical_work.protected_update(
                self.admission_grants, principal, request
            )
        return await self.update_gateway.update(principal, request)

    async def relate(self, request: ProtectedRelation) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.relation_gateway.guard(
                request, "denied", "authenticated_principal_required"
            )
        if self.canonical_work_active:
            if self.canonical_work is None:
                return self.relation_gateway.guard(
                    request, "unknown", "canonical_work_unavailable", possible_send=False,
                )
            return await self.canonical_work.protected_relation(
                self.admission_grants, principal, request
            )
        return await self.relation_gateway.update(principal, request)

    @staticmethod
    def _result_guard(
        request: RequiredResultSaveRequest,
        status: Literal["denied", "stale", "unknown"],
        reason: str,
        operation_id: UUID | None = None,
        possible_send: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome(
            status=status, operation="required_result_save", work_id=request.work_id,
            operation_id=operation_id, reason=reason,
            effect="unknown" if possible_send else "not_sent",
            retry="reconcile" if possible_send else "refresh" if status == "stale" else "none",
            next_action=("Reconcile the recorded effect; do not send a new operation."
                         if possible_send else "Refresh work/grant or ask the trusted issuer."),
        )

    async def required_result_save(self, request: RequiredResultSaveRequest) -> GuardOutcome:
        """Promote one server-identified result into notes through the update effect journal."""
        principal = await self.principal()
        if principal is None:
            return self._result_guard(request, "denied", "authenticated_principal_required")
        if self.required_results is None:
            return self._result_guard(request, "denied", "required_result_persistence_unavailable")

        operation_id: UUID | None = None
        possible_send = False
        try:
            grant = await self.admission_grants.current(principal.key)
            if not self.gateway.admitted(principal, grant) or grant is None:
                return self._result_guard(request, "denied", "no_current_grant")
            if request.grant_version != grant.version:
                return self._result_guard(request, "stale", "grant_version_changed")
            if not grant.can_write(request.work_id) or "work_update" not in grant.operations:
                return self._result_guard(request, "denied", "operation_or_work_not_granted")
            handle = await self.state.get(request.work_id)
            if handle is None:
                return self._result_guard(request, "denied", "work_not_bound")

            currentness = hashlib.sha256(
                f"{principal.key}:{grant.id}:{grant.version}".encode()
            ).hexdigest()
            destination = (
                f"postgres:work:{request.work_id}:notes" if self.canonical_work_active
                else f"{handle.provider}:task:{handle.provider_work_id}:notes"
            )
            correlation = hashlib.sha256(request.text.encode()).hexdigest()
            operation_id = uuid5(
                REQUIRED_RESULT_NAMESPACE, f"{principal.key}:{request.work_id}:{destination}"
            )
            repository = self.required_results.repository
            obligation = await repository.get(operation_id)
            if obligation is None:
                try:
                    obligation = await repository.create(operation_id, request.work_id, currentness)
                except SQLAlchemyError:
                    obligation = await repository.get(operation_id)
                    if obligation is None:
                        raise
            if obligation.work_id_ref != request.work_id:
                return self._result_guard(
                    request, "denied", "lifecycle_obligation_work_conflict", operation_id
                )
            if obligation.state is ProfileState.PENDING_RESULT:
                identity = (None, None)
            else:
                identity = (obligation.destination_ref, obligation.result_correlation)
            if identity not in {(None, None), (destination, correlation)}:
                return self._result_guard(
                    request, "denied", "lifecycle_result_identity_conflict", operation_id
                )
            if obligation.currentness_token != currentness:
                async with self.admission_grants.locked(principal.key, request.work_id) as locked_grant:
                    if (locked_grant is None or not self.gateway.admitted(principal, locked_grant)
                            or locked_grant.id != grant.id
                            or locked_grant.version != grant.version
                            or not locked_grant.can_write(request.work_id)
                            or "work_update" not in locked_grant.operations):
                        return self._result_guard(
                            request, "stale", "lifecycle_currentness_changed", operation_id
                        )
                    effect = await self.admission_grants.exact(operation_id)
                    if effect is None:
                        obligation = await repository.adopt_currentness(obligation, currentness)
                    elif effect.principal_key != principal.key:
                        return self._result_guard(
                            request, "stale", "lifecycle_currentness_changed", operation_id
                        )
            if obligation.state is ProfileState.TERMINAL:
                return GuardOutcome(
                    status="ok", operation="required_result_save", work_id=request.work_id,
                    operation_id=operation_id, reason="required_result_already_persisted",
                    next_action="Use the durable Lifecycle terminal evidence.",
                )
            if (obligation.state is ProfileState.UNKNOWN
                    and obligation.unknown_reason != LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS.value):
                return self._result_guard(
                    request, "unknown", "lifecycle_currentness_unresolved", operation_id
                )

            effect = await self.admission_grants.exact(operation_id)
            if effect is None:
                if self.canonical_work_active:
                    if self.canonical_work is None:
                        return self._result_guard(
                            request, "unknown", "canonical_work_unavailable", operation_id
                        )
                    current_result = await self.canonical_work.get(request.work_id)
                    current_work = current_result.item
                    if current_work is None:
                        return self._result_guard(
                            request, "unknown", "source_read_unavailable", operation_id
                        )
                else:
                    provider = self.providers[handle.provider]
                    current_work = await provider.get(handle.provider_work_id)
                    if current_work is None or not current_work.canonical:
                        return self._result_guard(
                            request, "unknown", "source_read_unavailable", operation_id
                        )
                if current_work.completed:
                    return self._result_guard(request, "denied", "work_is_terminal", operation_id)
                if current_work.revision != request.observed_revision:
                    return self._result_guard(
                        request, "stale", "source_revision_changed", operation_id
                    )
                try:
                    notes = _required_result_notes(current_work.notes, request.text)
                except ValueError:
                    return GuardOutcome(
                        status="not_applied", operation="required_result_save",
                        work_id=request.work_id, operation_id=operation_id,
                        reason="required_result_exceeds_notes_capacity", effect="not_sent",
                        retry="none",
                        next_action="Shorten the result or current notes; nothing was written.",
                    )
                update_request = ProtectedUpdate(
                    api_version=request.api_version, operation_id=operation_id,
                    work_id=request.work_id, grant_version=request.grant_version,
                    observed_revision=request.observed_revision, patch=ScalarPatch(notes=notes),
                )
            else:
                raw_request_value = effect.intent.get("request")
                if not isinstance(raw_request_value, dict):
                    raise ValueError("durable required-result update request is invalid")
                raw_request = cast(dict[str, object], raw_request_value)
                raw_patch_value = raw_request.get("patch")
                if not isinstance(raw_patch_value, dict):
                    raise ValueError("durable required-result update patch is invalid")
                raw_patch = cast(dict[str, object], raw_patch_value)
                raw_request = raw_request | {
                    "patch": {key: value for key, value in raw_patch.items() if value is not None}
                }
                update_request = ProtectedUpdate.model_validate(raw_request)
                result_notes = update_request.patch.notes
                expected_suffix = f"{REQUIRED_RESULT_HEADING}\n\n{request.text}"
                if (
                    effect.principal_key != principal.key
                    or update_request.work_id != request.work_id
                    or update_request.patch.model_fields_set != {"notes"}
                    or result_notes is None
                    or not result_notes.endswith(expected_suffix)
                ):
                    return self._result_guard(
                        request, "denied", "lifecycle_effect_identity_conflict", operation_id
                    )

            # Do not bind one immutable result identity until its exact notes update
            # has passed every no-effect current-work and capacity check. A caller may
            # then correct a rejected result without inheriting an unusable obligation.
            if obligation.state is ProfileState.PENDING_RESULT:
                try:
                    obligation = await self.required_results.transition(
                        operation_id, currentness, LifecycleEvent.RESULT_READY,
                        destination_ref=destination, result_correlation=correlation,
                    )
                except ValueError:
                    obligation = await repository.get(operation_id)
                    if obligation is None:
                        raise
            if (obligation.destination_ref, obligation.result_correlation) != (destination, correlation):
                return self._result_guard(
                    request, "denied", "lifecycle_result_identity_conflict", operation_id
                )

            if self.canonical_work_active:
                assert self.canonical_work is not None
                outcome = await self.canonical_work.protected_update(
                    self.admission_grants, principal, update_request
                )
            else:
                outcome = await self.update_gateway.update(principal, update_request)
            possible_send = outcome.effect != "not_sent"
            if outcome.effect == "applied" and isinstance(outcome.receipt, UpdateReceipt):
                evidence = {
                    "destination_ref": destination, "result_correlation": correlation,
                    "operation_id": str(operation_id), "provider": outcome.receipt.provider,
                    "task_gid": outcome.receipt.task_gid,
                    "resulting_revision": outcome.receipt.resulting_revision,
                }
                try:
                    async with self.admission_grants.locked(principal.key, request.work_id) as current:
                        if (current is None or not self.gateway.admitted(principal, current)
                                or current.id != grant.id or current.version != grant.version
                                or not current.can_write(request.work_id)
                                or "work_update" not in current.operations):
                            return self._result_guard(
                                request, "stale", "lifecycle_currentness_changed_before_terminal",
                                operation_id, True,
                            )
                        await self.required_results.transition(
                            operation_id, obligation.currentness_token,
                            LifecycleEvent.PERSIST_READBACK_MATCHED,
                            evidence=evidence,
                        )
                except (SQLAlchemyError, ValueError):
                    confirmed = await repository.get(operation_id)
                    if confirmed is None or confirmed.state is not ProfileState.TERMINAL:
                        return self._result_guard(
                            request, "unknown", "lifecycle_terminal_commit_unconfirmed",
                            operation_id, True,
                        )
                return outcome.model_copy(update={"operation": "required_result_save"})
            if outcome.effect == "unknown" and obligation.state is ProfileState.PERSIST_REQUIRED:
                try:
                    await self.required_results.transition(
                        operation_id, obligation.currentness_token,
                        LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS,
                        evidence={"operation_id": str(operation_id), "reason": outcome.reason},
                    )
                except (SQLAlchemyError, ValueError):
                    pass
            return outcome.model_copy(update={"operation": "required_result_save"})
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return self._result_guard(
                request, "unknown", "lifecycle_or_effect_state_unavailable",
                operation_id, possible_send,
            )

    async def message_send(self, request: MessageSendRequest) -> MessageSubmitResult:
        principal = await self.principal()
        if principal is None:
            return MessageSubmitResult(status="denied", reason="actor_not_admitted")
        if self.messages is None:
            return MessageSubmitResult(status="recovery_required", reason="state_unavailable")
        return await send_message(
            self.state, self.grants, self.messages, principal, request
        )

    async def message_pending(
        self, work_id: UUID, request: MessagePendingRequest,
    ) -> MessagePendingResult:
        principal = await self.principal()
        if principal is None:
            return MessagePendingResult(status="denied", reason="actor_not_admitted")
        if self.messages is None:
            return MessagePendingResult(status="recovery_required", reason="state_unavailable")
        return await pending_messages(
            self.state, self.grants, self.messages, principal, work_id, request
        )
