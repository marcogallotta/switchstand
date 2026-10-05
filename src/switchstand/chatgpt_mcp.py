from collections.abc import Callable
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import CallToolResult, ResourceLink, TextContent, ToolAnnotations
from pydantic import Field, JsonValue, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError

from . import repository_bundle, repository_candidate
from .agent_mailboxes import AgentMailboxState
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


class StatefulOrdinaryWorkResult(ClosedModel):
    """Exact ordinary work read enriched with owner-local actionable state."""
    action_summary: ActionSummary | ActionSummaryUnavailable | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    open_failures: tuple[OpenFailureAction, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    item: PublicWorkItem | None = None
    status: Status
    guard: PublicReadGuard | None = None


class OrdinaryUpdateResult(GuardOutcome):
    action_summary: ActionSummary | ActionSummaryUnavailable | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


class OutcomeStateUpdateResult(ClosedModel):
    status: Literal["APPLIED", "REPLAYED", "STALE", "CONFLICT", "DENIED", "UNKNOWN"]
    state_id: UUID | None = None


class OrdinaryRelationPatch(ClosedModel):
    """Provider-neutral relation shape for the ordinary surface."""
    kind: Literal["parent", "dependency"]
    action: Literal["set", "clear", "add", "remove"]
    target_work_id: UUID | None = None

    @model_validator(mode="after")
    def valid_relation(self) -> Self:
        if self.kind == "parent":
            if self.action not in {"set", "clear"}:
                raise ValueError("parent relation requires set or clear")
            if (self.action == "set") != (self.target_work_id is not None):
                raise ValueError("parent target does not match action")
        elif self.kind == "dependency" and (
            self.action not in {"add", "remove"} or self.target_work_id is None
        ):
            raise ValueError("dependency relation requires target and add/remove")
        return self

    def internal(self) -> RelationPatch:
        return RelationPatch(
            kind=self.kind, action=self.action, target_work_id=self.target_work_id,
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
})

ORDINARY_NON_IDEMPOTENT_TOOLS: frozenset[str] = frozenset()
OUTCOME_STATE_TOOLS = frozenset({"outcome_state_update"})


def ordinary_tool_annotations(name: str) -> ToolAnnotations:
    """Emit private-host approval metadata; reject unreviewed surface growth."""
    if name not in ORDINARY_GENUINE_READ_TOOLS | ORDINARY_EFFECT_TOOLS | OUTCOME_STATE_TOOLS:
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
        """Return the verified current public repository bundle as an MCP resource link."""
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

    async def enriched_work_get(
        api_version: Literal["1"], work_id: UUID | None = None,
    ) -> StatefulOrdinaryWorkResult:
        """Read exact work with its owner-local action summary when available."""
        result = await work_get(api_version, work_id)
        summary = None
        failures = None
        if result.status == "ok" and result.item is not None:
            owner_work_id = result.item.id
            summary = await action_summary(owner_work_id, result.item.revision)
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
            action_summary=summary, open_failures=failures, item=result.item,
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
        if result.status != "unknown" and result.effect != "unknown":
            current = await service.get(work_id)
            if current.status == "ok" and current.item is not None:
                summary = await action_summary(work_id, current.item.revision)
        return OrdinaryUpdateResult(
            **{name: getattr(result, name) for name in GuardOutcome.model_fields},
            action_summary=summary,
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
        """Register this authenticated actor's immutable visible agent name."""
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
            return AgentMessageSubmitResult(status=context[0], reason=context[1])
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
            return AgentMessagePendingResult(status=context[0], reason=context[1])
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

    return (
        ("repository_bundle_get", repository_bundle_get),
        ("repository_candidate_qualification_get", repository_candidate_qualification_get),
        ("work_get", enriched_work_get if service.outcome_state_enabled else work_get),
        ("work_search", work_search),
        ("work_resolve_reference", work_resolve_reference),
        ("work_history", work_history),
        ("work_event", work_event),
        ("work_append", work_append),
        ("work_create", work_create),
        ("work_update", enriched_work_update if service.outcome_state_enabled else work_update),
        *((("outcome_state_update", outcome_state_update),)
          if service.outcome_state_enabled else ()),
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
