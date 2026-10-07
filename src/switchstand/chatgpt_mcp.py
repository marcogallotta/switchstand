from collections.abc import Callable
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import CallToolResult, ResourceLink, TextContent, ToolAnnotations
from pydantic import Field, JsonValue, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError

from . import activation_continuity as activation
from . import flow_report, repository_bundle, repository_candidate
from .activation_continuity import ContinuityResult, Transition, next_action
from .agent_mailboxes import AgentMailboxResult, AgentMailboxState
from .agent_messages import (
    AgentMessageContext,
    AgentMessagePendingResult,
    AgentMessageSubmitResult,
    AgentPendingMessage,
    AgentRegistrationResult,
    AgentTransferRequestResult,
    public_message,
)
from .chatgpt import ChatGPTService, RequiredResultSaveRequest
from .contracts import (
    ClosedModel,
    Status,
    WorkEventRequest,
    WorkEventResult,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkResolveReferenceRequest,
    WorkSearchRequest,
    WorkSearchResult,
)
from .failure_journal import FailureJournal
from .grants import (
    GrantedWorkResult,
    GuardOutcome,
    ProtectedAppend,
    ProtectedCreate,
    ProtectedRelation,
    ProtectedUpdate,
    RelationPatch,
    ScalarPatch,
)
from .implementation_requests import ImplementationRequestResult
from .mcp import PublicReadGuard, PublicWorkItem, closed_tool, project_work
from .messages import (
    DispositionEvidence,
    MessageDispositionRequest,
    MessagePendingRequest,
    MessageReceiveRequest,
    MessageRoute,
    MessageSendRequest,
    MessageSubmitRequest,
    MessageSubmitResult,
    MessageTransitionResult,
    RuntimeCurrentness,
    disposition_digest,
)
from .outcome_state import ActionSummary, OutcomeItem
from .priority_claim_service import (
    HumanPriorityClear,
    HumanPrioritySet,
    PriorityClaimReadResult,
)
from .priority_claims import PriorityBand, RelationKind, SubjectKind
from .priority_context import PriorityContextResult
from .product_currentness import ProductCurrentness
from .reviews import (
    ContextProvenance,
    ReviewFinding,
    ReviewKind,
    ReviewMode,
    ReviewRequest,
    ReviewResult,
    ReviewSubmit,
    ReviewVerdict,
)
from .task_control import DurableControlCapsule, TaskControlCheckpointResult, TaskControlReadResult

HistoryPurpose = Literal["investigation", "recovery", "legacy_reconciliation"]
AppendPurpose = Literal["provenance", "investigation", "legacy_reconciliation"]


class OrdinaryWorkResult(ClosedModel):
    """Provider-neutral ordinary work result without legacy related/grouped inference."""
    status: Status
    item: PublicWorkItem | None = None
    guard: PublicReadGuard | None = None


class ActionSummaryUnavailable(ClosedModel):
    state: Literal["UNAVAILABLE"] = "UNAVAILABLE"


class OpenFailureAction(ClosedModel):
    attempt_id: UUID
    clearing_action: str
    effect_state: str


class ActivationContinuityResult(ClosedModel):
    """Public state omits stored binding, actor/grant evidence, and technical basis."""
    status: Literal[
        "APPLIED", "REPLAYED", "CURRENT", "STALE", "CONFLICT", "DENIED", "MISSING", "UNKNOWN"
    ]
    observed_revision: str | None = None
    state: str | None = None
    acceptance: str | None = None
    adoption: str | None = None
    target_revision: str | None = None
    target_phase: str | None = None
    owner_work_id: UUID | None = None
    blocker_ref: str | None = None
    clearing_event_ref: str | None = None
    next_action: str | None = None
    terminal: bool | None = None
    reason: str | None = None


class StatefulOrdinaryWorkResult(ClosedModel):
    """Exact ordinary work read enriched with owner-local actionable state."""
    action_summary: ActionSummary | ActionSummaryUnavailable | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    open_failures: tuple[OpenFailureAction, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    activation_obligations: tuple[ActivationContinuityResult, ...] | ActionSummaryUnavailable | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    item: PublicWorkItem | None = None
    status: Status
    guard: PublicReadGuard | None = None


class OrdinaryUpdateResult(GuardOutcome):
    action_summary: ActionSummary | ActionSummaryUnavailable | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    activation_obligations: tuple[ActivationContinuityResult, ...] | ActionSummaryUnavailable | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


class OutcomeStateUpdateResult(ClosedModel):
    status: Literal["APPLIED", "REPLAYED", "STALE", "CONFLICT", "DENIED", "UNKNOWN"]
    state_id: UUID | None = None


def project_activation_continuity(result: ContinuityResult) -> ActivationContinuityResult:
    value = result.obligation
    return ActivationContinuityResult(
        status=result.status,
        observed_revision=None if value is None else value.digest,
        state=None if value is None else value.state,
        acceptance=None if value is None else value.acceptance,
        adoption=None if value is None else value.adoption,
        target_revision=None if value is None else str(value.binding["target_revision"]),
        target_phase=None if value is None else str(value.binding["target_phase"]),
        owner_work_id=None if value is None else UUID(str(value.binding["return_owner_work_id"])),
        blocker_ref=None if value is None else value.blocker_ref,
        clearing_event_ref=None if value is None else value.clearing_event_ref,
        next_action=None if value is None else next_action(value),
        terminal=None if value is None else value.state in {"DELIVERED", "DEFERRED", "RETIRED"},
        reason=result.reason,
    )


class OrdinaryRelationPatch(ClosedModel):
    """Provider-neutral relation shape for the ordinary surface."""
    kind: Literal["parent", "dependency", "placement"]
    action: Literal["set", "clear", "add", "remove"]
    target_work_id: UUID | None = None
    project_id: UUID | None = None

    @model_validator(mode="after")
    def valid_relation(self) -> Self:
        if self.kind == "parent":
            if self.action not in {"set", "clear"}:
                raise ValueError("parent relation requires set or clear")
            if (self.action == "set") != (self.target_work_id is not None):
                raise ValueError("parent target does not match action")
            if self.project_id is not None:
                raise ValueError("parent relation forbids unrelated fields")
        elif self.kind == "dependency" and (
            self.action not in {"add", "remove"} or self.target_work_id is None
        ):
            raise ValueError("dependency relation requires target and add/remove")
        elif self.kind == "dependency" and self.project_id is not None:
            raise ValueError("dependency relation forbids unrelated fields")
        elif self.kind == "placement" and (
            self.action not in {"add", "remove"}
            or self.project_id is None
            or self.target_work_id is not None
        ):
            raise ValueError("placement requires project and add/remove")
        return self

    def internal(self) -> RelationPatch:
        return RelationPatch(
            kind=self.kind, action=self.action, target_work_id=self.target_work_id,
            project_id=self.project_id,
        )


ORDINARY_GENUINE_READ_TOOLS = frozenset({
    "repository_bundle_get",
    "repository_candidate_qualification_get",
    "work_get",
    "work_search",
    "work_resolve_reference",
    "work_history",
    "work_event",
    "agent_message_pending",
    "capability_preflight_get",
    "task_control_get",
})

ORDINARY_EFFECT_TOOLS = frozenset({
    "work_append",
    "work_create",
    "work_update",
    "work_relate",
    "required_result_save",
    "agent_register",
    "agent_takeover",
    "agent_transfer_request",
    "agent_message_send",
    "agent_message_receive",
    "agent_message_recover",
    "agent_message_result_send",
    "agent_message_disposition",
    "task_control_checkpoint",
})

ORDINARY_NON_IDEMPOTENT_TOOLS: frozenset[str] = frozenset()
OUTCOME_STATE_TOOLS = frozenset({"outcome_state_update"})
PRIORITY_CLAIM_TOOLS = frozenset({"priority_claim_get", "priority_claim_record"})
PRIORITY_CONTEXT_TOOLS = frozenset({"priority_context_get"})
IMPLEMENTATION_REQUEST_TOOLS = frozenset({"implementation_request"})
PRODUCT_CURRENTNESS_TOOLS = frozenset({"product_currentness_get"})
ACTIVATION_CONTINUITY_TOOLS = frozenset({"activation_obligation_transition"})
REVIEW_TOOLS = frozenset({"review_request", "review_get", "review_recover", "review_submit",
                          "review_bundle_get"})
OBSERVABILITY_TOOLS = frozenset({"observability_get"})


def ordinary_tool_annotations(name: str) -> ToolAnnotations:
    """Emit private-host approval metadata; reject unreviewed surface growth."""
    if name not in (
        ORDINARY_GENUINE_READ_TOOLS | ORDINARY_EFFECT_TOOLS
        | OUTCOME_STATE_TOOLS | PRIORITY_CLAIM_TOOLS | PRIORITY_CONTEXT_TOOLS
        | IMPLEMENTATION_REQUEST_TOOLS
        | PRODUCT_CURRENTNESS_TOOLS
        | ACTIVATION_CONTINUITY_TOOLS
        | REVIEW_TOOLS
        | OBSERVABILITY_TOOLS
    ):
        raise ValueError(f"ordinary tool lacks annotations: {name}")
    return ToolAnnotations(
        # ChatGPT prompts for ordinary effects even when this private app allows all tools.
        # Server-side admission and validation remain authoritative for every operation.
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=name not in ORDINARY_NON_IDEMPOTENT_TOOLS,
        open_world_hint=False,
    )


def project_ordinary_work(result: GrantedWorkResult) -> OrdinaryWorkResult:
    projected = project_work(result)
    return OrdinaryWorkResult(
        status=projected.status, item=projected.item, guard=projected.guard,
    )


def build_ordinary_tools(
    service: ChatGPTService,
    audit: Callable[[str, str | None, str], None] | None = None,
    agent_identity: Callable[[], str] | None = None,
    correlate_work: Callable[[UUID | None], None] | None = None,
    review_bundle_enabled: bool = False,
) -> tuple[tuple[str, Callable[..., Any]], ...]:
    """Build the canonical ordinary tool callables shared by all transports."""

    chat_identity = agent_identity or (lambda: (_ for _ in ()).throw(
        RuntimeError("ChatGPT runtime identity unavailable")
    ))
    message_engine = None if service.messages is None else getattr(service.messages, "engine", None)
    mailboxes = None if message_engine is None else AgentMailboxState(message_engine)

    def audited(tool: str, target: str | None, status: str) -> None:
        if audit is not None:
            audit(tool, target, status)

    def correlate(work_id: UUID | None) -> None:
        if correlate_work is not None:
            correlate_work(work_id)

    async def current_grant_version() -> tuple[int | None, str]:
        result = await service.admission_get()
        if result.status == "unknown":
            return None, "unknown"
        if result.status != "ok" or result.grant is None:
            return None, "denied"
        return result.grant.version, "ok"

    def admission_unknown(
        operation: str, work_id: UUID | None = None, operation_id: UUID | None = None,
    ) -> GuardOutcome:
        return GuardOutcome(
            status="unknown", operation=operation, work_id=work_id, operation_id=operation_id,
            reason="admission_state_unavailable", effect="not_sent", retry="none",
            next_action="Retry after admission state is readable; no provider effect was sent.",
        )

    async def repository_bundle_get(
        api_version: Literal["1"],
        required_sha: Annotated[str | None, Field(pattern=r"^[0-9a-f]{40}$")] = None,
    ) -> CallToolResult:
        """Use this when substantive Switchstand repository content is needed and no verified local checkout is available.

        Returns the verified current public repository bundle as an MCP resource link. Do not use
        when a verified local checkout is already available. On ``refresh_pending``, retry boundedly
        rather than reconstructing the repository through repeated remote file/tree reads. Pass
        ``required_sha`` only as the exact local checkout target; it never selects another bundle.
        """
        del api_version
        result = await repository_bundle.resolve_repository_bundle(required_sha)
        structured = {
            "status": result.status,
            "repository": result.repository,
            "snapshot_digest": result.snapshot_digest,
            "bundle_sha256": result.bundle_sha256,
            "bundle_url": result.bundle_url,
            "ref_count": len(result.refs),
            "required_sha": result.required_sha,
            "required_sha_is_head": result.required_sha_is_head,
            "reason": result.reason,
        }
        if result.status != "current" or result.bundle_url is None:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=f"Repository bundle {result.status}: {result.reason or 'unavailable'}.",
                )],
                structured_content=structured,
            )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        "Current Switchstand repository bundle. Verify the published SHA-256 "
                        "before materialization; required_sha is only the exact local checkout target."
                    ),
                ),
                ResourceLink(
                    type="resource_link",
                    name=repository_bundle.BUNDLE_NAME,
                    uri=result.bundle_url,
                    description="Verified current Switchstand Git repository bundle",
                    mime_type="application/octet-stream",
                ),
            ],
            structured_content=structured,
        )

    async def repository_candidate_qualification_get(
        api_version: Literal["1"],
        pull_request: Annotated[int, Field(ge=1)],
        include_failure_detail: bool = False,
    ) -> repository_candidate.RepositoryCandidateQualification:
        """Qualify the exact current GitHub PR candidate against code-owned CI gates."""
        del api_version
        result = await repository_candidate.qualify_repository_candidate(
            pull_request, include_failure_detail,
        )
        audited("repository_candidate_qualification_get", str(pull_request), result.status)
        return result

    async def observability_get(work_id: UUID) -> dict[str, object]:
        """Read current exact-WorkId flow evidence and authenticated review pickup state."""
        correlate(work_id)
        if service.reviews is None:
            raise RuntimeError("canonical review occurrence state is unavailable")
        result = await flow_report.report(
            service.reviews.occurrences.engine, work_id, service.reviews.occurrences,
        )
        audited("observability_get", str(work_id), "ok")
        return result

    async def work_get(
        api_version: Literal["1"], work_id: UUID | None = None,
    ) -> OrdinaryWorkResult:
        """Read canonical current state, including current notes, routing, and placement context."""
        correlate(work_id)
        result = await service.get(work_id)
        audited("work_get", None if work_id is None else str(work_id), result.status)
        return project_ordinary_work(result)

    async def action_summary(
        owner_work_id: UUID, currentness_token: str,
    ) -> ActionSummary | ActionSummaryUnavailable | None:
        outcomes = getattr(service.state, "outcomes", None)
        if outcomes is None:
            return ActionSummaryUnavailable()
        try:
            summary = await outcomes.summary(owner_work_id, currentness_token)
        except (SQLAlchemyError, KeyError, TypeError, ValueError, ValidationError):
            return ActionSummaryUnavailable()
        return ActionSummaryUnavailable() if summary == "UNKNOWN" else summary

    async def activation_projection(
        owner_work_id: UUID,
    ) -> tuple[ActivationContinuityResult, ...] | ActionSummaryUnavailable | None:
        if service.activation_continuity is None:
            return None
        values = await service.activation_continuity.for_owner(owner_work_id)
        if values == "UNKNOWN":
            return ActionSummaryUnavailable()
        return tuple(project_activation_continuity(
            ContinuityResult(status="CURRENT", obligation=value)
        ) for value in values)

    async def enriched_work_get(
        api_version: Literal["1"], work_id: UUID | None = None,
    ) -> StatefulOrdinaryWorkResult:
        """Read exact work with its owner-local action summary when available."""
        result = await work_get(api_version, work_id)
        summary = None
        failures = None
        activation = None
        if result.status == "ok" and result.item is not None:
            owner_work_id = result.item.id
            summary = await action_summary(owner_work_id, result.item.revision)
            activation = await activation_projection(owner_work_id)
            engine = getattr(service.state, "engine", None)
            if engine is not None:
                try:
                    opened = await FailureJournal(engine).open(owner=str(owner_work_id))
                    if opened != "UNKNOWN":
                        failures = tuple(OpenFailureAction(
                            attempt_id=item.attempt_id,
                            clearing_action=item.clearing_action,
                            effect_state=item.effect_state.value,
                        ) for item in opened)
                except (SQLAlchemyError, KeyError, TypeError, ValueError):
                    failures = None
        return StatefulOrdinaryWorkResult(
            action_summary=summary, activation_obligations=activation,
            open_failures=failures, item=result.item,
            status=result.status, guard=result.guard,
        )

    async def work_search(
        api_version: Literal["1"], text: str | None = None,
        completed: bool | None = None, lifecycle_state: str | None = None,
        owner_key: str | None = None, priority: str | None = None,
        work_type: str | None = None, canonical_root: str | None = None,
        cursor: str | None = None, limit: int = 50,
    ) -> WorkSearchResult:
        """Search admitted workspace work and return only stable provider-neutral WorkIds."""
        result = await service.search(WorkSearchRequest(
            api_version=api_version, text=text, completed=completed,
            lifecycle_state=lifecycle_state, owner_key=owner_key, priority=priority,
            work_type=work_type, canonical_root=canonical_root,
            cursor=cursor, limit=limit,
        ))
        audited("work_search", None, result.status)
        return result

    async def work_resolve_reference(
        api_version: Literal["1"],
        reference: Annotated[str, Field(min_length=1, max_length=2048)],
    ) -> OrdinaryWorkResult:
        """Resolve one exact legacy task reference to current provider-neutral work."""
        result = await service.resolve_reference(WorkResolveReferenceRequest(
            api_version=api_version, reference=reference,
        ))
        audited("work_resolve_reference", None, result.status)
        return project_ordinary_work(result)

    async def work_history(
        api_version: Literal["1"], work_id: UUID, observed_revision: str,
        purpose: HistoryPurpose,
        cursor: str | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> WorkHistoryResult:
        """Exceptional investigation/recovery/legacy history; never normal grounding or polling."""
        correlate(work_id)
        result = await service.history(
            WorkHistoryRequest(api_version=api_version, work_id=work_id,
                               observed_revision=observed_revision, cursor=cursor, limit=limit))
        audited("work_history", f"{work_id}:purpose={purpose}", result.status)
        return result

    async def work_event(
        api_version: Literal["1"], event_id: UUID, observed_revision: str,
        work_id: UUID, purpose: HistoryPurpose,
    ) -> WorkEventResult:
        """Exceptional investigation/recovery/legacy event read; never normal grounding or polling."""
        correlate(work_id)
        result = await service.event(
            WorkEventRequest(api_version=api_version, work_id=work_id,
                             event_id=event_id, observed_revision=observed_revision))
        audited("work_event", f"{work_id}:purpose={purpose}", result.status)
        return result

    async def work_append(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        observed_revision: str, text: str, purpose: AppendPurpose,
    ) -> GuardOutcome:
        """Append exceptional provenance/history only; never current state, results, or messaging."""
        correlate(work_id)
        grant_version, admission = await current_grant_version()
        if admission == "unknown":
            return admission_unknown("work_append", work_id, operation_id)
        if grant_version is None:
            return service.denied("work_append", "no_current_grant")
        result = await service.append(ProtectedAppend(
            api_version=api_version, operation_id=operation_id, work_id=work_id,
            grant_version=grant_version, observed_revision=observed_revision, text=text,
        ))
        audited("work_append", f"{work_id}:purpose={purpose}", result.status)
        return result

    async def work_create(
        api_version: Literal["1"], operation_id: UUID,
        title: str, parent_work_id: UUID | None = None, project_id: UUID | None = None,
        notes: str = "", priority: str = "UNSET",
        work_type: str = "UNKNOWN", lifecycle_state: Literal[
            "CURRENT", "WAITING", "DEFERRED", "TERMINAL", "UNKNOWN"
        ] = "UNKNOWN", canonical_root: str | None = None, owner_key: str = "UNKNOWN",
        wait_kind: str = "UNKNOWN", unblock_condition: str = "UNKNOWN",
        next_due: str = "UNKNOWN", next_action_class: str = "UNKNOWN",
        next_action_ref: str = "UNKNOWN",
    ) -> GuardOutcome:
        """Create work under one admitted provider-neutral parent or project."""
        correlate(parent_work_id)
        grant_version, admission = await current_grant_version()
        if admission == "unknown":
            return admission_unknown("work_create", parent_work_id or project_id, operation_id)
        if grant_version is None:
            return service.denied("work_create", "no_current_grant")
        if service.canonical_work_active:
            evidence = (
                parent_work_id is not None
                and work_type == "Evidence"
                and owner_key == "NONE"
            )
            if not evidence:
                if owner_key != "SELF":
                    return service.denied(
                        "work_create", "canonical_owner_must_be_self",
                        "Use SELF for substantive work; ownership never grants create authority.",
                    )
                context = await agent_context()
                if isinstance(context, tuple):
                    status, reason = context
                    if status == "recovery_required":
                        return GuardOutcome(
                            status="unknown", operation="work_create",
                            operation_id=operation_id,
                            reason=reason, effect="not_sent", retry="none",
                            next_action="Restore the registered runtime identity, then retry.",
                        )
                    return GuardOutcome(
                        status="denied", operation="work_create",
                        operation_id=operation_id,
                        reason=reason, effect="not_sent", retry="none",
                        next_action=unregistered_agent_next_action,
                    )
                owner_key = f"agent:{context.mailbox.name_key}"
        result = await service.create(ProtectedCreate(
            api_version=api_version, operation_id=operation_id, parent_work_id=parent_work_id,
            project_id=project_id,
            grant_version=grant_version, title=title, notes=notes,
            priority=priority, work_type=work_type, lifecycle_state=lifecycle_state,
            canonical_root=canonical_root, owner_key=owner_key, wait_kind=wait_kind,
            unblock_condition=unblock_condition, next_due=next_due,
            next_action_class=next_action_class, next_action_ref=next_action_ref,
        ))
        audited("work_create", str(parent_work_id or project_id), result.status)
        return result

    async def task_control_get(
        api_version: Literal["1"], work_id: UUID,
    ) -> TaskControlReadResult:
        """Read the latest durable control capsule and server-computed currentness."""
        del api_version
        correlate(work_id)
        result = await service.task_control_get(work_id)
        audited("task_control_get", str(work_id), result.status)
        return result

    async def task_control_checkpoint(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        observed_work_revision: str,
        expected_checkpoint_generation: Annotated[int | None, Field(ge=1)],
        capsule: DurableControlCapsule,
    ) -> TaskControlCheckpointResult:
        """CAS-replace one owner checkpoint; effect labels never grant authority."""
        del api_version
        correlate(work_id)
        result = await service.task_control_checkpoint(
            operation_id, work_id, observed_work_revision,
            expected_checkpoint_generation, capsule,
        )
        audited("task_control_checkpoint", str(work_id), result.status)
        return result

    async def work_update(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        observed_revision: str, patch: ScalarPatch,
    ) -> GuardOutcome:
        """Write canonical current state; use notes for intent, progress, findings, verdicts, and results."""
        correlate(work_id)
        grant_version, admission = await current_grant_version()
        if admission == "unknown":
            return admission_unknown("work_update", work_id, operation_id)
        if grant_version is None:
            return service.denied("work_update", "no_current_grant")
        result = await service.update(ProtectedUpdate(
            api_version=api_version, operation_id=operation_id, work_id=work_id,
            grant_version=grant_version, observed_revision=observed_revision, patch=patch,
        ))
        audited("work_update", str(work_id), result.status)
        return result

    async def enriched_work_update(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        observed_revision: str, patch: ScalarPatch,
    ) -> OrdinaryUpdateResult:
        """Write canonical state and enrich known outcomes from one fresh exact read."""
        result = await work_update(
            api_version, operation_id, work_id, observed_revision, patch,
        )
        summary = None
        activation = None
        if result.status != "unknown" and result.effect != "unknown":
            current = await service.get(work_id)
            if current.status == "ok" and current.item is not None:
                summary = await action_summary(work_id, current.item.revision)
                activation = await activation_projection(work_id)
        return OrdinaryUpdateResult(
            **{name: getattr(result, name) for name in GuardOutcome.model_fields},
            action_summary=summary, activation_obligations=activation,
        )

    async def outcome_state_update(
        api_version: Literal["1"], operation_id: UUID, owner_work_id: UUID,
        expected_state_id: UUID | None, owner_observed_revision: Annotated[
            str, Field(min_length=1)
        ], items: tuple[OutcomeItem, ...],
    ) -> OutcomeStateUpdateResult:
        """Record AGENT outcome state for one explicit admitted owner at its exact revision."""
        correlate(owner_work_id)
        del api_version
        result = await service.outcome_state_update(
            operation_id=operation_id, owner_work_id=owner_work_id,
            expected_state_id=expected_state_id,
            owner_observed_revision=owner_observed_revision, items=items,
        )
        audited("outcome_state_update", str(owner_work_id), result.status)
        return OutcomeStateUpdateResult(status=result.status, state_id=result.state_id)

    async def priority_claim_get(
        api_version: Literal["1"], subject_kind: SubjectKind, subject_id: UUID,
    ) -> PriorityClaimReadResult:
        """Read current priority claims for one exact WorkId or project."""
        del api_version
        correlate(subject_id if subject_kind == "WORK" else None)
        return await service.priority_claim_get(subject_kind, subject_id)

    async def priority_claim_record(
        api_version: Literal["1"], operation_id: UUID,
        action: Literal["SET", "CLEAR"], subject_kind: SubjectKind, subject_id: UUID,
        observed_revision: Annotated[str | None, Field(min_length=1)] = None,
        relation_kind: RelationKind | None = None,
        rationale: Annotated[str | None, Field(min_length=1, max_length=300)] = None,
        relation_target_id: UUID | None = None, band: PriorityBand | None = None,
        supersedes_claim_id: UUID | None = None,
        claim_id: UUID | None = None,
    ) -> GuardOutcome:
        """SET or CLEAR explicit HUMAN priority from current Marco direction."""
        correlate(subject_id if subject_kind == "WORK" else None)
        if action == "CLEAR":
            if claim_id is None or any(value is not None for value in (
                relation_kind, rationale, relation_target_id, band, supersedes_claim_id,
            )):
                raise ValueError("CLEAR requires only the exact claim_id and subject fence")
            request = HumanPriorityClear(
                api_version=api_version, operation_id=operation_id,
                subject_kind=subject_kind, subject_id=subject_id,
                claim_id=claim_id, observed_revision=observed_revision,
            )
        else:
            if claim_id is not None or relation_kind is None or rationale is None:
                raise ValueError("SET requires relation_kind and rationale, not claim_id")
            request = HumanPrioritySet(
                api_version=api_version, operation_id=operation_id,
                subject_kind=subject_kind, subject_id=subject_id,
                observed_revision=observed_revision, relation_kind=relation_kind,
                rationale=rationale, relation_target_id=relation_target_id,
                band=band, supersedes_claim_id=supersedes_claim_id,
            )
        return await service.priority_claim_record(request)

    async def priority_context_get(
        api_version: Literal["1"],
        work_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=50)],
    ) -> PriorityContextResult:
        """Project context for explicit WorkIds without ranking or inheritance."""
        del api_version
        return await service.priority_context_get(work_ids)

    async def implementation_request(
        api_version: Literal["1"], operation_id: UUID,
        package_work_id: UUID, observed_revision: Annotated[str, Field(min_length=1)],
    ) -> ImplementationRequestResult:
        """Create or replay one authorized implementation request for an exact package."""
        del api_version
        correlate(package_work_id)
        result = await service.implementation_request(
            operation_id, package_work_id, observed_revision
        )
        audited("implementation_request", str(package_work_id), result.status)
        return result

    async def review_request(
        api_version: Literal["1"], subject_work_id: UUID,
        observed_revision: Annotated[str, Field(min_length=1)], review_kind: ReviewKind,
        candidate_ref: Annotated[str | None, Field(min_length=1, max_length=500)] = None,
        mode: ReviewMode = "FULL", prior_review_id: UUID | None = None,
        finding_ids: tuple[str, ...] = (),
    ) -> ReviewResult:
        """Request one review as this registered caller at an exact subject revision."""
        del api_version
        correlate(subject_work_id)
        context = await agent_context()
        if isinstance(context, tuple):
            result = ReviewResult(
                status="UNKNOWN" if context[0] == "recovery_required" else "DENIED",
                reason="state_unavailable" if context[0] == "recovery_required"
                else "requester_not_current",
            )
        else:
            assert service.reviews is not None
            result = await service.reviews.request(ReviewRequest(
                subject_work_id=subject_work_id, observed_revision=observed_revision,
                review_kind=review_kind, candidate_ref=candidate_ref, mode=mode,
                prior_review_id=prior_review_id, finding_ids=finding_ids,
            ), context.mailbox)
        audited("review_request", str(subject_work_id), result.status)
        return result

    async def review_bundle_get(api_version: Literal["1"], review_id: UUID) -> CallToolResult:
        def unavailable(status: str, reason: str | None = None, **detail: object) -> CallToolResult:
            return CallToolResult(content=[TextContent(type="text", text=status)],
                                  structured_content={"status": status, "reason": reason} | detail)
        def qualification_status(value: repository_candidate.RepositoryCandidateQualification) -> str:
            identity = (value.base_sha, value.head_sha, value.composition_sha)
            if None in identity or len(value.composition_parents) != 2:
                return "UNKNOWN"
            expected = {
                ("Exact-head Quality", "exact_head", value.head_sha),
                ("PR composition Quality", "composition", value.composition_sha),
            }
            observed = {(gate.name, gate.subject_kind, gate.subject_sha) for gate in value.gates}
            if (len(value.gates) != 2 or observed != expected
                    or any(gate.detail_reason is not None for gate in value.gates)):
                return "UNKNOWN"
            stale = value.reason in {"composition_mismatch", "candidate_changed"} or any(
                gate.reason in {"wrong-head", "wrong-base", "wrong-composition", "conflicting",
                                "stale"} for gate in value.gates)
            if value.status == "NOT_READY" and stale:
                return "STALE"
            failed = {"action_required", "failure", "neutral", "stale", "startup_failure",
                      "timed_out"}
            known = {("queued", None, "queued"), ("in_progress", None, "running"),
                     ("completed", "success", None), ("completed", "cancelled", "cancelled"),
                     ("completed", "skipped", "skipped"), ("missing", None, "missing")}
            if any(
                (gate.state, gate.conclusion, gate.reason) not in known
                and not (gate.state == "completed" and gate.reason == "failed"
                         and gate.conclusion in failed) for gate in value.gates
            ):
                return "UNKNOWN"
            gate_ready = all(gate.reason is None for gate in value.gates)
            coherent = (value.status, value.reason, gate_ready) in {
                ("READY", None, True), ("NOT_READY", "gates_not_ready", False),
            }
            return "READY" if coherent else "UNKNOWN"
        context = await agent_context()
        if isinstance(context, tuple):
            status = "UNKNOWN" if context[0] == "recovery_required" else "DENIED"
            return unavailable(status)
        assert service.reviews is not None
        access = await service.reviews.bundle_access(review_id, context.mailbox)
        if access.status != "AUTHORIZED" or access.basis is None:
            return unavailable(access.status, access.reason)
        prefix, candidate = "github:marcogallotta/switchstand:pr/", access.basis.candidate_ref or ""
        number = candidate.removeprefix(prefix)
        if not candidate.startswith(prefix) or not number.isdigit() or int(number) < 1:
            return unavailable("UNKNOWN", "candidate_identity")
        qualification = await repository_candidate.qualify_repository_candidate(int(number))
        status, reason = qualification_status(qualification), qualification.reason
        if status != "READY" or qualification.head_sha is None:
            return unavailable(status, reason, qualification=qualification.model_dump())
        bundle = await repository_bundle.resolve_repository_bundle(qualification.head_sha)
        if bundle.status != "current" or bundle.bundle_url is None:
            return unavailable("UNKNOWN", bundle.reason, bundle=bundle.model_dump())
        refreshed = await repository_candidate.qualify_repository_candidate(int(number))
        identity = {
            "pull_request", "base_sha", "head_sha", "composition_sha", "composition_parents",
        }
        if refreshed.model_dump(include=identity) != qualification.model_dump(include=identity):
            return unavailable("STALE", "candidate_changed")
        if (status := qualification_status(refreshed)) != "READY":
            return unavailable(status, refreshed.reason, qualification=refreshed.model_dump())
        final = await service.reviews.bundle_access(review_id, context.mailbox, access.basis)
        if final.status != "READY":
            return unavailable(final.status, final.reason)
        structured = {
            "status": final.status, "review_id": str(review_id),
            "delivery_id": str(final.delivery_id),
            "subject_work_id": str(access.basis.subject_work_id),
            "subject_revision": access.basis.subject_revision, "subject_title": final.subject_title,
            "material_claim_digest": final.material_claim_digest,
            "candidate": refreshed.model_dump(), "bundle": bundle.model_dump(),
            "exclusions": ["prior_verdicts", "author_narrative", "effect_authority"],
        }
        audited("review_bundle_get", str(review_id), final.status)
        return CallToolResult(
            content=[ResourceLink(
                type="resource_link", name=repository_bundle.BUNDLE_NAME,
                uri=bundle.bundle_url, mime_type="application/octet-stream",
            )],
            structured_content=structured,
        )
    async def review_submit(
        api_version: Literal["1"], review_id: UUID, verdict: ReviewVerdict,
        context_provenance: ContextProvenance,
        findings: tuple[ReviewFinding, ...] = (), evidence_refs: tuple[str, ...] = (),
    ) -> ReviewResult:
        """Submit one verdict as the current registered reviewer mailbox."""
        del api_version
        correlate(None)
        context = await agent_context()
        if isinstance(context, tuple):
            result = ReviewResult(
                status="UNKNOWN" if context[0] == "recovery_required" else "DENIED",
                reason="state_unavailable" if context[0] == "recovery_required"
                else "reviewer_binding_changed",
            )
        else:
            assert service.reviews is not None
            result = await service.reviews.submit(ReviewSubmit(
                review_id=review_id,
                verdict=verdict,
                findings=findings,
                evidence_refs=evidence_refs,
                context_provenance=context_provenance,
            ), context.mailbox)
        audited("review_submit", str(review_id), result.status)
        return result

    async def review_get(api_version: Literal["1"], review_id: UUID) -> ReviewResult:
        """Poll one exact review as its current registered requester."""
        del api_version
        correlate(review_id)
        context = await agent_context()
        if isinstance(context, tuple):
            result = ReviewResult(
                status="UNKNOWN" if context[0] == "recovery_required" else "DENIED",
                review_id=review_id,
                reason="state_unavailable" if context[0] == "recovery_required"
                else "caller_not_owner",
            )
        else:
            assert service.reviews is not None
            result = await service.reviews.get(review_id, context.mailbox)
        audited("review_get", str(review_id), result.status)
        return result

    async def review_recover(api_version: Literal["1"], review_id: UUID) -> ReviewResult:
        """Recover one received review after same-principal mailbox takeover."""
        del api_version
        correlate(review_id)
        context = await agent_context()
        if isinstance(context, tuple):
            result = ReviewResult(
                status="UNKNOWN" if context[0] == "recovery_required" else "DENIED",
                review_id=review_id,
                reason="state_unavailable" if context[0] == "recovery_required"
                else "recovery_not_allowed",
            )
        else:
            assert service.reviews is not None
            result = await service.reviews.recover(review_id, context.mailbox)
        audited("review_recover", str(review_id), result.status)
        return result

    async def product_currentness_get(api_version: Literal["1"]) -> ProductCurrentness:
        """Reconcile Stateful technical currentness from server-owned live evidence."""
        del api_version
        assert service.product_currentness is not None
        principal = await service.principal()
        if principal is None:
            raise PermissionError("authenticated principal is unavailable")
        return await service.product_currentness(principal)

    async def capability_preflight_get(api_version: Literal["1"]) -> activation.CapabilityPreflight:
        del api_version
        return activation.CapabilityPreflight(
            surface="ORDINARY_WORKSPACE", status="MISSING_CAPABILITY",
            reasons=("WORK_BOUND_ACTOR_REQUIRED",),
        )

    async def activation_obligation_transition(
        api_version: Literal["1"], operation_id: UUID, obligation_id: UUID,
        observed_revision: Annotated[str, Field(min_length=1, max_length=64)],
        transition: Transition,
        evidence_refs: Annotated[tuple[str, ...], Field(max_length=16)] = (),
        blocker_ref: Annotated[str | None, Field(min_length=1, max_length=1000)] = None,
        clearing_event_ref: Annotated[str | None, Field(min_length=1, max_length=1000)] = None,
    ) -> ActivationContinuityResult:
        """Advance one installed contract; actor and technical truth are server-owned."""
        del api_version
        result = await service.activation_continuity_transition(
            operation_id, obligation_id, observed_revision, transition,
            evidence_refs, blocker_ref, clearing_event_ref,
        )
        audited("activation_obligation_transition", str(obligation_id), result.status)
        return project_activation_continuity(result)

    async def work_relate(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        observed_revision: str, patch: OrdinaryRelationPatch,
    ) -> GuardOutcome:
        """Apply one bounded relation mutation through current authenticated admission."""
        correlate(work_id)
        grant_version, admission = await current_grant_version()
        if admission == "unknown":
            return admission_unknown("work_relate", work_id, operation_id)
        if grant_version is None:
            return service.denied("work_relate", "no_current_grant")
        result = await service.relate(ProtectedRelation(
            api_version=api_version, operation_id=operation_id, work_id=work_id,
            grant_version=grant_version, observed_revision=observed_revision,
            patch=patch.internal(),
        ))
        audited("work_relate", str(work_id), result.status)
        return result

    async def required_result_save(
        api_version: Literal["1"], work_id: UUID,
        observed_revision: str, text: Annotated[str, Field(min_length=1, max_length=8000)],
    ) -> GuardOutcome:
        """Promote one required result into canonical current notes with authoritative readback."""
        correlate(work_id)
        grant_version, admission = await current_grant_version()
        if admission == "unknown":
            return admission_unknown("required_result_save", work_id)
        if grant_version is None:
            return service.denied("required_result_save", "no_current_grant")
        result = await service.required_result_save(RequiredResultSaveRequest(
            api_version=api_version, work_id=work_id, grant_version=grant_version,
            observed_revision=observed_revision, text=text,
        ))
        audited("required_result_save", str(work_id), result.status)
        return result

    async def agent_context() -> AgentMessageContext | tuple[
        Literal["denied", "recovery_required"],
        Literal[
            "state_unavailable", "no_current_grant", "agent_not_registered",
            "runtime_identity_unavailable",
        ],
    ]:
        try:
            principal = await service.principal()
        except (KeyError, RuntimeError, ValueError):
            return "recovery_required", "state_unavailable"
        if principal is None:
            return "denied", "no_current_grant"
        if mailboxes is None:
            return "recovery_required", "state_unavailable"
        try:
            chat_session = chat_identity()
        except (KeyError, RuntimeError, TypeError, ValueError):
            return "recovery_required", "runtime_identity_unavailable"
        if not chat_session:
            return "recovery_required", "runtime_identity_unavailable"
        binding = await mailboxes.for_actor(principal.key, chat_session)
        if binding.status == "recovery_required":
            return "recovery_required", "state_unavailable"
        if binding.status != "ok" or binding.mailbox is None:
            return "denied", "agent_not_registered"
        return AgentMessageContext(principal, binding.mailbox)

    unregistered_agent_next_action = (
        "Call agent_register for this exact session using its intended durable agent name, "
        "then retry. Do not infer /root from an internal role label. Only after Marco "
        "explicitly authorizes taking over an existing /root, use agent_takeover for the "
        "same authenticated principal; cross-principal replacement requires "
        "agent_transfer_request and host approval."
    )

    def agent_transition_failure(
        failure: tuple[
            Literal["denied", "recovery_required"],
            Literal[
                "state_unavailable", "no_current_grant", "agent_not_registered",
                "runtime_identity_unavailable",
            ],
        ],
    ) -> MessageTransitionResult:
        if failure[1] == "agent_not_registered":
            return MessageTransitionResult(
                status=failure[0], reason="receiving_binding_changed"
            )
        return MessageTransitionResult(status=failure[0], reason=failure[1])

    def agent_runtime(mailbox_generation: int) -> RuntimeCurrentness:
        durable_generation = str(mailbox_generation)
        return RuntimeCurrentness(
            generation=durable_generation, current_generation=durable_generation,
        )

    async def agent_submit_view(
        result: MessageSubmitResult, *, request: bool = False,
    ) -> AgentMessageSubmitResult:
        if result.status != "ok" or result.message is None:
            return AgentMessageSubmitResult(status=result.status, reason=result.reason)
        assert mailboxes is not None
        view = await public_message(mailboxes, result.message)
        if view is None:
            return AgentMessageSubmitResult(
                status="recovery_required", reason="state_unavailable"
            )
        next_action = None
        if request and view.state in ("AVAILABLE", "RECEIVED"):
            next_action = (
                "SENT is not a reply or completion. If this request needs a response, "
                "keep its watch active while this chat can run: read pending results, "
                "use a bounded wait, and reread. An empty read does not end the watch. "
                "Do not substitute an hourly Scheduled watch. If the chat stops, "
                "preserve the exact request for re-entry."
            )
        elif request and view.state == "DISPOSITIONED":
            next_action = (
                "The recipient completed this request. Check and act on its pending result; "
                "if already handled, continue your assigned work. Do not wait for the "
                "original delivery."
            )
        return AgentMessageSubmitResult(
            status="ok", message=view, next_action=next_action,
        )

    async def agent_register(
        api_version: Literal["1"],
        name: Annotated[str, Field(min_length=1, max_length=80)],
    ) -> AgentRegistrationResult:
        """Register an immutable name; /root requires authorized takeover or transfer."""
        del api_version
        try:
            principal = await service.principal()
        except (KeyError, RuntimeError, ValueError):
            result = AgentRegistrationResult(
                status="recovery_required", reason="state_unavailable"
            )
        else:
            if principal is None:
                result = AgentRegistrationResult(
                    status="denied", reason="no_current_grant"
                )
            elif mailboxes is None:
                result = AgentRegistrationResult(
                    status="recovery_required", reason="state_unavailable"
                )
            else:
                try:
                    chat_session = chat_identity()
                except (KeyError, RuntimeError, TypeError, ValueError):
                    chat_session = ""
                if not chat_session:
                    result = AgentRegistrationResult(
                        status="recovery_required", reason="runtime_identity_unavailable"
                    )
                    audited("agent_register", name, result.status)
                    return result
                stored = await mailboxes.register_agent(name, principal.key, chat_session)
                result = (
                    AgentRegistrationResult(status="ok", name=stored.mailbox.name)
                    if stored.status == "ok" and stored.mailbox is not None
                    else AgentRegistrationResult(status=stored.status, reason=stored.reason)
                )
        audited("agent_register", name, result.status)
        return result

    async def agent_takeover(
        api_version: Literal["1"],
        name: Annotated[str, Field(min_length=1, max_length=80)],
    ) -> AgentRegistrationResult:
        """Rebind a dead agent after Marco declares it dead; if unclear, ask Marco first."""
        del api_version
        try:
            principal = await service.principal()
            chat_session = chat_identity()
        except (KeyError, RuntimeError, TypeError, ValueError):
            principal, chat_session = None, ""
        if principal is None:
            result = AgentRegistrationResult(status="denied", reason="no_current_grant")
        elif not chat_session:
            result = AgentRegistrationResult(
                status="recovery_required", reason="runtime_identity_unavailable"
            )
        elif mailboxes is None:
            result = AgentRegistrationResult(status="recovery_required", reason="state_unavailable")
        else:
            stored = await mailboxes.takeover(name, principal.key, chat_session)
            if stored.status == "recovery_required" and stored.reason == "state_unavailable":
                # Same-principal takeover is replay-safe for this exact replacement session.
                # Retry only the ambiguous state result, with the already-captured identity
                # and unchanged arguments, then require an authoritative binding readback.
                stored = await mailboxes.takeover(name, principal.key, chat_session)
                if stored.status == "ok" and stored.mailbox is not None:
                    observed = await mailboxes.for_actor(principal.key, chat_session)
                    if observed.status != "ok" or observed.mailbox != stored.mailbox:
                        stored = AgentMailboxResult(
                            status="recovery_required", reason="state_unavailable"
                        )
            result = (
                AgentRegistrationResult(status="ok", name=stored.mailbox.name)
                if stored.status == "ok" and stored.mailbox is not None
                else AgentRegistrationResult(status=stored.status, reason=stored.reason)
            )
        audited("agent_takeover", name, result.status)
        return result

    async def agent_transfer_request(
        api_version: Literal["1"],
        name: Annotated[str, Field(min_length=1, max_length=80)],
    ) -> AgentTransferRequestResult:
        """Request host approval to take over a name owned by another authenticated identity."""
        del api_version
        try:
            principal = await service.principal()
            chat_session = chat_identity()
        except (KeyError, RuntimeError, TypeError, ValueError):
            principal, chat_session = None, ""
        if principal is None:
            result = AgentTransferRequestResult(status="denied", reason="no_current_grant")
        elif not chat_session:
            result = AgentTransferRequestResult(
                status="recovery_required", reason="runtime_identity_unavailable"
            )
        elif mailboxes is None:
            result = AgentTransferRequestResult(
                status="recovery_required", reason="state_unavailable"
            )
        else:
            stored = await mailboxes.request_transfer(name, principal.key, chat_session)
            if stored.status in ("pending", "approved"):
                assert stored.request_id is not None and stored.name is not None
                result = AgentTransferRequestResult(
                    status="ok", request_id=stored.request_id, name=stored.name
                )
            else:
                result = AgentTransferRequestResult(status=stored.status, reason=stored.reason)
        audited("agent_transfer_request", name, result.status)
        return result

    async def agent_message_send(
        api_version: Literal["1"], recipient_name: str,
        message_id: UUID, payload: JsonValue,
    ) -> AgentMessageSubmitResult:
        """Send one durable request to a registered immutable agent name."""
        context = await agent_context()
        if isinstance(context, tuple):
            return AgentMessageSubmitResult(
                status=context[0], reason=context[1],
                next_action=(
                    unregistered_agent_next_action
                    if context[1] == "agent_not_registered" else None
                ),
            )
        sender = context.mailbox
        assert mailboxes is not None and service.messages is not None
        recipient = await mailboxes.by_name(recipient_name)
        if recipient.status == "recovery_required":
            return AgentMessageSubmitResult(
                status="recovery_required", reason="state_unavailable"
            )
        if recipient.status != "ok" or recipient.mailbox is None:
            return AgentMessageSubmitResult(
                status="denied", reason="recipient_not_registered"
            )
        replay = await service.messages.committed_public_replay(
            sender.endpoint_id,
            MessageSendRequest(
                api_version=api_version, work_id=sender.endpoint_id,
                grant_version=sender.generation, message_id=message_id,
                route_ref=f"agent.{recipient.mailbox.name_key}", payload=payload,
                recipient_work_id=recipient.mailbox.endpoint_id,
            ),
            agent_binding=sender,
        )
        if replay is not None:
            return await agent_submit_view(replay, request=True)
        route = MessageRoute(
            recipient_work_id=recipient.mailbox.endpoint_id,
            recipient_grant_version=recipient.mailbox.generation,
        )
        submitted = MessageSubmitRequest(
            api_version=api_version,
            message_id=message_id,
            grant_version=sender.generation,
            route_ref=f"agent.{recipient.mailbox.name_key}",
            kind="request",
            payload=payload,
        )
        result = await service.messages.submit_admitted(
            sender.endpoint_id, route, submitted, agent_binding=sender,
        )
        return await agent_submit_view(result, request=True)

    async def agent_message_pending(
        api_version: Literal["1"], cursor: UUID | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> AgentMessagePendingResult:
        """List this registered agent's durable pending deliveries without WorkId addressing."""
        context = await agent_context()
        if isinstance(context, tuple):
            return AgentMessagePendingResult(
                status=context[0], reason=context[1],
                next_action=(
                    unregistered_agent_next_action
                    if context[1] == "agent_not_registered" else None
                ),
            )
        mailbox = context.mailbox
        assert mailboxes is not None and service.messages is not None
        result = await service.messages.pending_admitted(
            mailbox.endpoint_id,
            MessagePendingRequest(
                api_version=api_version,
                grant_version=mailbox.generation,
                cursor=cursor,
                limit=limit,
            ),
            agent_binding=mailbox,
        )
        if result.status != "ok":
            return AgentMessagePendingResult(status=result.status, reason=result.reason)
        views: list[AgentPendingMessage] = []
        for item in result.messages:
            view = await public_message(mailboxes, item)
            if view is None:
                return AgentMessagePendingResult(
                    status="recovery_required", reason="state_unavailable"
                )
            views.append(view)
        return AgentMessagePendingResult(
            status="ok", messages=tuple(views),
            next_cursor=result.next_cursor, has_more=result.has_more,
        )

    async def agent_message_receive(
        api_version: Literal["1"], delivery_id: UUID,
    ) -> MessageTransitionResult:
        """Receive one delivery for this registered name under current MCP-session fencing."""
        context = await agent_context()
        if isinstance(context, tuple):
            return agent_transition_failure(context)
        mailbox = context.mailbox
        runtime = agent_runtime(mailbox.generation)
        assert service.messages is not None
        return await service.messages.receive_admitted(
            mailbox.endpoint_id, mailbox.generation, runtime,
            MessageReceiveRequest(
                api_version=api_version, delivery_id=delivery_id,
                grant_version=mailbox.generation,
            ),
            agent_binding=mailbox,
        )

    async def agent_message_recover(
        api_version: Literal["1"], delivery_id: UUID,
    ) -> MessageTransitionResult:
        """Explicitly recover a received delivery after an authorized binding/session replacement."""
        context = await agent_context()
        if isinstance(context, tuple):
            return agent_transition_failure(context)
        mailbox = context.mailbox
        runtime = agent_runtime(mailbox.generation)
        assert service.messages is not None
        return await service.messages.recover_admitted(
            mailbox.endpoint_id, mailbox.generation, runtime,
            MessageReceiveRequest(
                api_version=api_version, delivery_id=delivery_id,
                grant_version=mailbox.generation,
            ),
            agent_binding=mailbox,
        )

    async def agent_message_result_send(
        api_version: Literal["1"], delivery_id: UUID,
        message_id: UUID, payload: JsonValue,
    ) -> AgentMessageSubmitResult:
        """Reply to one received delivery as this registered agent name."""
        context = await agent_context()
        if isinstance(context, tuple):
            return AgentMessageSubmitResult(status=context[0], reason=context[1])
        mailbox = context.mailbox
        runtime = agent_runtime(mailbox.generation)
        assert mailboxes is not None and service.messages is not None
        replay = await service.messages.committed_public_replay(
            mailbox.endpoint_id,
            MessageSendRequest(
                api_version=api_version, work_id=mailbox.endpoint_id,
                grant_version=mailbox.generation, message_id=message_id,
                payload=payload, in_reply_to_delivery_id=delivery_id,
            ),
            agent_binding=mailbox,
        )
        if replay is not None:
            return await agent_submit_view(replay)
        reply = await service.messages.reply_context(delivery_id)
        if reply is None:
            return AgentMessageSubmitResult(status="conflict", reason="reply_delivery_not_found")
        recipient = await mailboxes.by_endpoint_id(reply.recipient_work_id)
        if recipient.status != "ok" or recipient.mailbox is None:
            return AgentMessageSubmitResult(status="recovery_required", reason="state_unavailable")
        route = MessageRoute(
            recipient_work_id=recipient.mailbox.endpoint_id,
            recipient_grant_version=recipient.mailbox.generation,
        )
        submitted = MessageSubmitRequest(
            api_version=api_version, message_id=message_id,
            grant_version=mailbox.generation, route_ref=reply.route_ref,
            kind="result", payload=payload, in_reply_to_delivery_id=delivery_id,
        )
        result = await service.messages.submit_received_result(
            mailbox.endpoint_id, mailbox.generation, runtime.generation, route, submitted,
            agent_binding=mailbox,
        )
        return await agent_submit_view(result)

    async def agent_message_disposition(
        api_version: Literal["1"], delivery_id: UUID, result_message_id: UUID,
    ) -> MessageTransitionResult:
        """Disposition one received delivery using its exact reply as evidence."""
        context = await agent_context()
        if isinstance(context, tuple):
            return agent_transition_failure(context)
        mailbox = context.mailbox
        runtime = agent_runtime(mailbox.generation)
        assert service.messages is not None
        evidence = DispositionEvidence(kind="result", result_message_id=result_message_id)
        return await service.messages.disposition_admitted(
            mailbox.endpoint_id, mailbox.generation, runtime,
            MessageDispositionRequest(
                api_version=api_version, delivery_id=delivery_id,
                grant_version=mailbox.generation,
                disposition_digest=disposition_digest(evidence), evidence=evidence,
            ),
            agent_binding=mailbox,
        )

    # FastMCP derives argument-schema titles from the callable name. Codex rejects a
    # tool whose registered name and generated argument title disagree.
    enriched_work_get.__name__ = "work_get"
    enriched_work_update.__name__ = "work_update"
    _ = activation_obligation_transition  # Intentionally unavailable on this surface.

    return (
        ("repository_bundle_get", repository_bundle_get),
        ("repository_candidate_qualification_get", repository_candidate_qualification_get),
        *(((("observability_get", observability_get),)) if service.reviews is not None else ()),
        ("work_get", enriched_work_get if (
            service.outcome_state_enabled or service.activation_continuity is not None
        ) else work_get),
        ("work_search", work_search),
        ("work_resolve_reference", work_resolve_reference),
        ("work_history", work_history),
        ("work_event", work_event),
        ("work_append", work_append),
        ("work_create", work_create),
        ("task_control_get", task_control_get),
        ("task_control_checkpoint", task_control_checkpoint),
        ("work_update", enriched_work_update if (
            service.outcome_state_enabled or service.activation_continuity is not None
        ) else work_update),
        *((("outcome_state_update", outcome_state_update),)
          if service.outcome_state_enabled else ()),
        *((
            ("priority_claim_get", priority_claim_get),
            ("priority_claim_record", priority_claim_record),
        ) if service.priority_claims_enabled and service.priority_claims is not None else ()),
        *((("priority_context_get", priority_context_get),)
          if service.priority_context_enabled and service.priority_context is not None else ()),
        *((("implementation_request", implementation_request),)
          if service.implementation_requests is not None else ()),
        *((
            ("review_request", review_request), ("review_get", review_get),
            ("review_recover", review_recover), ("review_submit", review_submit),
        ) if service.reviews is not None else ()),
        *((("review_bundle_get", review_bundle_get),)
          if service.reviews is not None and review_bundle_enabled else ()),
        *((("product_currentness_get", product_currentness_get),)
          if service.product_currentness_enabled and service.product_currentness is not None else ()),
        ("capability_preflight_get", capability_preflight_get),
        ("work_relate", work_relate),
        ("required_result_save", required_result_save),
        ("agent_register", agent_register),
        ("agent_takeover", agent_takeover),
        ("agent_transfer_request", agent_transfer_request),
        ("agent_message_send", agent_message_send),
        ("agent_message_pending", agent_message_pending),
        ("agent_message_receive", agent_message_receive),
        ("agent_message_recover", agent_message_recover),
        ("agent_message_result_send", agent_message_result_send),
        ("agent_message_disposition", agent_message_disposition),
    )


def build_chatgpt_server(service: ChatGPTService, server: MCPServer | None = None) -> MCPServer:
    """A host may supply an OAuth-configured server; default stdio is test-adapter only."""
    server = server or MCPServer("Switchstand ChatGPT")
    for name, function in build_ordinary_tools(service):
        closed_tool(server, name, function, ordinary_tool_annotations(name))
    return server
