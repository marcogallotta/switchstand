import asyncio
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server import MCPServer
from pydantic import Field, JsonValue

from .agent_mailboxes import AgentMailboxState
from .agent_messages import (
    AgentMessageContext,
    AgentMessagePendingResult,
    AgentMessageSubmitResult,
    AgentPendingMessage,
    AgentRegistrationResult,
    public_message,
)
from .chatgpt import ChatGPTService, RequiredResultSaveRequest
from .contracts import (
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTaskRequest,
    SourceTaskResult,
    WorkAttachmentsRequest,
    WorkAttachmentsResult,
    WorkEventRequest,
    WorkEventResult,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkResolveReferenceRequest,
    WorkSearchRequest,
    WorkSearchResult,
    WorkStructureRequest,
    WorkStructureResult,
)
from .grants import (
    GuardOutcome,
    PrincipalContext,
    ProtectedAppend,
    ProtectedCreate,
    ProtectedRelation,
    ProtectedUpdate,
    RelationPatch,
    ScalarPatch,
    WorkGrant,
)
from .mcp import PublicWorkResult, closed_tool, project_work
from .messages import (
    DispositionEvidence,
    MessageDispositionRequest,
    MessagePendingRequest,
    MessagePendingResult,
    MessageReceiveRequest,
    MessageRoute,
    MessageSendRequest,
    MessageSubmitRequest,
    MessageSubmitResult,
    MessageTransitionResult,
    RuntimeCurrentness,
    disposition_digest,
    send_received_result,
)
from .resolver import ResolverResult, resolve_alias


def build_message_tools(
    service: ChatGPTService,
    audit: Callable[[str, str | None, str], None] | None = None,
) -> tuple[tuple[str, Callable[..., Any]], ...]:
    async def message_send(
        api_version: Literal["1"], work_id: UUID,
        message_id: UUID, payload: JsonValue,
        route_ref: Annotated[str | None, Field(min_length=1)] = None,
        recipient_work_id: UUID | None = None,
        in_reply_to_delivery_id: UUID | None = None,
    ) -> MessageSubmitResult:
        """Durably send one request or exactly correlated result."""
        grant = await service.grant_get()
        if grant.status == "unknown":
            return MessageSubmitResult(status="recovery_required", reason="state_unavailable")
        if grant.status != "ok" or grant.grant is None:
            return MessageSubmitResult(status="denied", reason="no_current_grant")
        result = await service.message_send(MessageSendRequest(
            api_version=api_version, work_id=work_id, grant_version=grant.grant.version,
            message_id=message_id, route_ref=route_ref, payload=payload,
            recipient_work_id=recipient_work_id,
            in_reply_to_delivery_id=in_reply_to_delivery_id,
        ))
        if audit is not None:
            audit("message_send", f"{work_id}:{message_id}", result.status)
        return result

    async def message_pending(
        api_version: Literal["1"], work_id: UUID,
        cursor: UUID | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> MessagePendingResult:
        """Inspect durable pending deliveries under current authenticated admission."""
        grant = await service.grant_get()
        if grant.status == "unknown":
            return MessagePendingResult(status="recovery_required", reason="state_unavailable")
        if grant.status != "ok" or grant.grant is None:
            return MessagePendingResult(status="denied", reason="no_current_grant")
        result = await service.message_pending(work_id, MessagePendingRequest(
            api_version=api_version, grant_version=grant.grant.version, cursor=cursor, limit=limit,
        ))
        if audit is not None:
            audit("message_pending", str(work_id), result.status)
        return result

    return (("message_send", message_send), ("message_pending", message_pending))


def build_ordinary_tools(
    service: ChatGPTService,
    audit: Callable[[str, str | None, str], None] | None = None,
    session_generation: Callable[[], str] | None = None,
) -> tuple[tuple[str, Callable[..., Any]], ...]:
    """Build the canonical ordinary tool callables shared by all transports."""

    generation = session_generation or (lambda: (_ for _ in ()).throw(
        RuntimeError("MCP session generation unavailable")
    ))
    current_generations: dict[tuple[str, UUID, UUID, int], str] = {}
    retired_generations: dict[tuple[str, UUID, UUID, int], set[str]] = {}
    currentness_locks: dict[tuple[str, UUID, UUID, int], asyncio.Lock] = {}
    message_engine = None if service.messages is None else getattr(service.messages, "engine", None)
    mailboxes = None if message_engine is None else AgentMailboxState(message_engine)

    def audited(tool: str, target: str | None, status: str) -> None:
        if audit is not None:
            audit(tool, target, status)

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

    async def work_get(
        api_version: Literal["1"], work_id: UUID | None = None, include_related: bool = False,
    ) -> PublicWorkResult:
        """Read granted work; include_related adds bounded direct-child evidence or UH_OH."""
        result = await service.get(work_id, include_related=include_related)
        audited("work_get", None if work_id is None else str(work_id), result.status)
        return project_work(result, include_related)

    async def work_search(
        api_version: Literal["1"], text: str | None = None,
        completed: bool | None = None, cursor: str | None = None, limit: int = 50,
    ) -> WorkSearchResult:
        """Search admitted workspace work and return only stable provider-neutral WorkIds."""
        result = await service.search(WorkSearchRequest(
            api_version=api_version, text=text, completed=completed,
            cursor=cursor, limit=limit,
        ))
        audited("work_search", None, result.status)
        return result

    async def work_resolve_reference(
        api_version: Literal["1"],
        reference: Annotated[str, Field(min_length=1, max_length=2048)],
    ) -> PublicWorkResult:
        """Resolve one exact legacy task reference to current provider-neutral work."""
        result = await service.resolve_reference(WorkResolveReferenceRequest(
            api_version=api_version, reference=reference,
        ))
        audited("work_resolve_reference", None, result.status)
        return project_work(result, False)

    async def work_structure(
        api_version: Literal["1"], work_id: UUID,
        observed_revision: Annotated[str, Field(min_length=1)],
    ) -> WorkStructureResult:
        """Read the immediate parent and complete direct children of bound workspace work."""
        result = await service.structure(WorkStructureRequest(
            api_version=api_version, work_id=work_id, observed_revision=observed_revision,
        ))
        audited("work_structure", str(work_id), result.status)
        return result

    async def work_history(
        api_version: Literal["1"], work_id: UUID, observed_revision: str,
        cursor: str | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> WorkHistoryResult:
        """Read bounded history; on stale, repeat work_get and restart pagination."""
        result = await service.history(
            WorkHistoryRequest(api_version=api_version, work_id=work_id,
                               observed_revision=observed_revision, cursor=cursor, limit=limit))
        audited("work_history", str(work_id), result.status)
        return result

    async def work_attachments(
        api_version: Literal["1"], work_id: UUID,
        observed_revision: Annotated[str, Field(min_length=1)],
        cursor: Annotated[str | None, Field(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
    ) -> WorkAttachmentsResult:
        """List attachment names only; on stale, repeat work_get and restart pagination."""
        result = await service.attachments(WorkAttachmentsRequest(
            api_version=api_version, work_id=work_id, observed_revision=observed_revision,
            cursor=cursor, limit=limit,
        ))
        audited("work_attachments", str(work_id), result.status)
        return result

    async def work_event(
        api_version: Literal["1"], event_id: UUID, observed_revision: str,
        work_id: UUID,
    ) -> WorkEventResult:
        """Reread one opaque event at the observed work revision."""
        result = await service.event(
            WorkEventRequest(api_version=api_version, work_id=work_id,
                             event_id=event_id, observed_revision=observed_revision))
        audited("work_event", str(work_id), result.status)
        return result

    async def source_task(api_version: Literal["1"], task_gid: str) -> SourceTaskResult:
        """Read current notes/state of one exact canonical source task; reading grants no work."""
        result = await service.source_task(
            SourceTaskRequest(api_version=api_version, task_gid=task_gid)
        )
        audited("source_task", task_gid, result.status)
        return result

    async def work_resolve_alias(
        api_version: Literal["1"],
        alias: Annotated[str, Field(min_length=1, max_length=80)],
    ) -> ResolverResult:
        """Resolve one normalized alias through the temporary canonical registry index."""
        async def read(task_gid: str) -> SourceTaskResult:
            return await service.source_task(SourceTaskRequest(api_version=api_version, task_gid=task_gid))

        result = await resolve_alias(alias, read)
        audited("work_resolve_alias", result.alias, result.status)
        return result

    async def source_stories(
        api_version: Literal["1"], task_gid: str, observed_revision: str,
        offset: str | None = None, limit: int = 50,
    ) -> SourceStoriesResult:
        """Read a bounded current history page; stale/error is not an empty inbox."""
        result = await service.source_stories(SourceStoriesRequest(
            api_version=api_version, task_gid=task_gid, observed_revision=observed_revision,
            offset=offset, limit=limit,
        ))
        audited("source_stories", task_gid, result.status)
        return result

    async def source_story(
        api_version: Literal["1"], task_gid: str, story_gid: str, observed_revision: str,
    ) -> SourceStoryResult:
        """Reread an exact material story and its target/currentness before relying on it."""
        result = await service.source_story(SourceStoryRequest(
            api_version=api_version, task_gid=task_gid, story_gid=story_gid,
            observed_revision=observed_revision,
        ))
        audited("source_story", task_gid, result.status)
        return result

    async def work_append(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        observed_revision: str, text: str,
    ) -> GuardOutcome:
        """Append through current authenticated workspace admission; never blind-retry UNKNOWN."""
        grant_version, admission = await current_grant_version()
        if admission == "unknown":
            return admission_unknown("work_append", work_id, operation_id)
        if grant_version is None:
            return service.denied("work_append", "no_current_grant")
        result = await service.append(ProtectedAppend(
            api_version=api_version, operation_id=operation_id, work_id=work_id,
            grant_version=grant_version, observed_revision=observed_revision, text=text,
        ))
        audited("work_append", str(work_id), result.status)
        return result

    async def work_create(
        api_version: Literal["1"], operation_id: UUID,
        title: str, notes: str = "", parent_work_id: UUID | None = None,
        project_gid: Annotated[str | None, Field(pattern=r"^[0-9]+$")] = None,
    ) -> GuardOutcome:
        """Create parented or admitted-project work through current authenticated admission."""
        grant_version, admission = await current_grant_version()
        if admission == "unknown":
            return admission_unknown("work_create", parent_work_id, operation_id)
        if grant_version is None:
            return service.denied("work_create", "no_current_grant")
        result = await service.create(ProtectedCreate(
            api_version=api_version, operation_id=operation_id, parent_work_id=parent_work_id,
            project_gid=project_gid, grant_version=grant_version, title=title, notes=notes,
        ))
        audited("work_create", str(parent_work_id or project_gid), result.status)
        return result

    async def work_update(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        observed_revision: str, patch: ScalarPatch,
    ) -> GuardOutcome:
        """Set bounded scalar state using current admission; UNKNOWN is never resent blindly."""
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

    async def work_relate(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        observed_revision: str, patch: RelationPatch,
    ) -> GuardOutcome:
        """Apply one bounded relation mutation through current authenticated admission."""
        grant_version, admission = await current_grant_version()
        if admission == "unknown":
            return admission_unknown("work_relate", work_id, operation_id)
        if grant_version is None:
            return service.denied("work_relate", "no_current_grant")
        result = await service.relate(ProtectedRelation(
            api_version=api_version, operation_id=operation_id, work_id=work_id,
            grant_version=grant_version, observed_revision=observed_revision, patch=patch,
        ))
        audited("work_relate", str(work_id), result.status)
        return result

    async def required_result_save(
        api_version: Literal["1"], work_id: UUID,
        observed_revision: str, text: Annotated[str, Field(min_length=1, max_length=8000)],
    ) -> GuardOutcome:
        """Save one required result; admission and stable operation identity are server-owned."""
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

    async def message_context(
        work_id: UUID,
    ) -> tuple[
        PrincipalContext, WorkGrant, tuple[str, UUID, UUID, int], str
    ] | MessageTransitionResult:
        grant_result = await service.grant_get()
        if grant_result.status == "unknown":
            return MessageTransitionResult(status="recovery_required", reason="state_unavailable")
        if (
            grant_result.status != "ok"
            or grant_result.principal is None
            or grant_result.grant is None
        ):
            return MessageTransitionResult(status="denied", reason="no_current_grant")
        principal, grant = grant_result.principal, grant_result.grant
        if "message" not in grant.operations:
            return MessageTransitionResult(status="denied", reason="no_current_grant")
        if grant.scope == "launch" and grant.authority.active_work_id != work_id:
            return MessageTransitionResult(status="denied", reason="no_current_grant")
        try:
            if await service.state.get(work_id) is None:
                return MessageTransitionResult(status="denied", reason="delivery_not_for_current_work")
            session_id = generation()
        except (KeyError, RuntimeError, ValueError):
            return MessageTransitionResult(
                status="recovery_required", reason="runtime_currentness_unavailable"
            )
        if not session_id:
            return MessageTransitionResult(
                status="recovery_required", reason="runtime_currentness_unavailable"
            )
        namespace = (principal.key, work_id, grant.id, grant.version)
        return principal, grant, namespace, session_id

    def stale_runtime() -> MessageTransitionResult:
        return MessageTransitionResult(status="stale", reason="runtime_generation_changed")

    def submit_preflight(result: MessageTransitionResult) -> MessageSubmitResult:
        """Preserve closed admission/currentness semantics on result-send preflight."""
        if result.reason == "no_current_grant":
            return MessageSubmitResult(status=result.status, reason="no_current_grant")
        if result.reason == "delivery_not_for_current_work":
            return MessageSubmitResult(
                status=result.status, reason="delivery_not_for_current_work"
            )
        if result.reason == "runtime_currentness_unavailable":
            return MessageSubmitResult(
                status=result.status, reason="runtime_currentness_unavailable"
            )
        if result.reason == "runtime_generation_changed":
            return MessageSubmitResult(
                status=result.status, reason="runtime_generation_changed"
            )
        return MessageSubmitResult(status="recovery_required", reason="state_unavailable")

    async def message_receive(
        api_version: Literal["1"], work_id: UUID, delivery_id: UUID,
    ) -> MessageTransitionResult:
        """Receive one exact delivery under this server-owned MCP session generation."""
        context = await message_context(work_id)
        if isinstance(context, MessageTransitionResult):
            audited("message_receive", str(work_id), context.status)
            return context
        principal, grant, namespace, session_id = context
        if service.messages is None:
            result = MessageTransitionResult(
                status="recovery_required", reason="state_unavailable"
            )
            audited("message_receive", str(work_id), result.status)
            return result
        lock = currentness_locks.setdefault(namespace, asyncio.Lock())
        async with lock:
            retired = retired_generations.setdefault(namespace, set())
            current = current_generations.get(namespace)
            if session_id in retired or (current is not None and current != session_id):
                result = stale_runtime()
            else:
                result = await service.messages.receive(
                    principal,
                    RuntimeCurrentness(
                        generation=session_id,
                        current_generation=session_id,
                    ),
                    MessageReceiveRequest(
                        api_version=api_version,
                        delivery_id=delivery_id,
                        grant_version=grant.version,
                    ),
                    work_id=work_id,
                )
                if result.status == "ok" and current is None:
                    current_generations[namespace] = session_id
        audited("message_receive", str(work_id), result.status)
        return result

    async def message_recover(
        api_version: Literal["1"], work_id: UUID, delivery_id: UUID,
    ) -> MessageTransitionResult:
        """Explicitly transfer one received delivery to this replacement MCP session."""
        context = await message_context(work_id)
        if isinstance(context, MessageTransitionResult):
            audited("message_recover", str(work_id), context.status)
            return context
        principal, grant, namespace, session_id = context
        if service.messages is None:
            result = MessageTransitionResult(
                status="recovery_required", reason="state_unavailable"
            )
            audited("message_recover", str(work_id), result.status)
            return result
        lock = currentness_locks.setdefault(namespace, asyncio.Lock())
        async with lock:
            retired = retired_generations.setdefault(namespace, set())
            previous = current_generations.get(namespace)
            if session_id in retired:
                result = stale_runtime()
            else:
                result = await service.messages.recover(
                    principal,
                    RuntimeCurrentness(
                        generation=session_id,
                        current_generation=session_id,
                    ),
                    MessageReceiveRequest(
                        api_version=api_version,
                        delivery_id=delivery_id,
                        grant_version=grant.version,
                    ),
                    work_id=work_id,
                )
                if result.status == "ok":
                    if previous is not None and previous != session_id:
                        retired.add(previous)
                    current_generations[namespace] = session_id
        audited("message_recover", str(work_id), result.status)
        return result

    async def message_result_send(
        api_version: Literal["1"], work_id: UUID,
        in_reply_to_delivery_id: UUID, message_id: UUID, payload: JsonValue,
    ) -> MessageSubmitResult:
        """Send a result only from the current MCP session bound to the received delivery."""
        context = await message_context(work_id)
        if isinstance(context, MessageTransitionResult):
            result = submit_preflight(context)
            audited("message_result_send", str(work_id), result.status)
            return result
        principal, grant, namespace, session_id = context
        if service.messages is None:
            result = MessageSubmitResult(status="recovery_required", reason="state_unavailable")
            audited("message_result_send", str(work_id), result.status)
            return result
        lock = currentness_locks.setdefault(namespace, asyncio.Lock())
        async with lock:
            retired = retired_generations.setdefault(namespace, set())
            current = current_generations.get(namespace)
            if session_id in retired or current != session_id:
                result = MessageSubmitResult(
                    status="stale", reason="runtime_generation_changed"
                )
            else:
                result = await send_received_result(
                    service.state,
                    service.grants,
                    service.messages,
                    principal,
                    MessageSendRequest(
                        api_version=api_version,
                        work_id=work_id,
                        grant_version=grant.version,
                        message_id=message_id,
                        payload=payload,
                        in_reply_to_delivery_id=in_reply_to_delivery_id,
                    ),
                    RuntimeCurrentness(
                        generation=session_id,
                        current_generation=session_id,
                    ),
                )
        audited("message_result_send", str(work_id), result.status)
        return result

    async def message_disposition(
        api_version: Literal["1"], work_id: UUID,
        delivery_id: UUID, result_message_id: UUID,
    ) -> MessageTransitionResult:
        """Disposition one received delivery only from its current MCP session."""
        context = await message_context(work_id)
        if isinstance(context, MessageTransitionResult):
            audited("message_disposition", str(work_id), context.status)
            return context
        principal, grant, namespace, session_id = context
        if service.messages is None:
            result = MessageTransitionResult(
                status="recovery_required", reason="state_unavailable"
            )
            audited("message_disposition", str(work_id), result.status)
            return result
        lock = currentness_locks.setdefault(namespace, asyncio.Lock())
        async with lock:
            retired = retired_generations.setdefault(namespace, set())
            current = current_generations.get(namespace)
            if session_id in retired or current != session_id:
                result = stale_runtime()
            else:
                evidence = DispositionEvidence(
                    kind="result", result_message_id=result_message_id
                )
                result = await service.messages.disposition(
                    principal,
                    RuntimeCurrentness(
                        generation=session_id,
                        current_generation=session_id,
                    ),
                    MessageDispositionRequest(
                        api_version=api_version,
                        delivery_id=delivery_id,
                        grant_version=grant.version,
                        disposition_digest=disposition_digest(evidence),
                        evidence=evidence,
                    ),
                    work_id=work_id,
                )
                if result.status == "ok" and result.state == "DISPOSITIONED":
                    has_received = await service.messages.has_received(work_id, grant.version)
                    if has_received is False:
                        current_generations.pop(namespace, None)
        audited("message_disposition", str(work_id), result.status)
        return result

    agent_current_generations: dict[tuple[UUID, int], str] = {}
    agent_retired_generations: dict[tuple[UUID, int], set[str]] = {}
    agent_currentness_locks: dict[tuple[UUID, int], asyncio.Lock] = {}

    async def agent_context() -> AgentMessageContext | tuple[
        Literal["denied", "recovery_required"],
        Literal["state_unavailable", "no_current_grant", "agent_not_registered"],
    ]:
        try:
            principal = await service.principal()
        except (KeyError, RuntimeError, ValueError):
            return "recovery_required", "state_unavailable"
        if principal is None:
            return "denied", "no_current_grant"
        if mailboxes is None:
            return "recovery_required", "state_unavailable"
        binding = await mailboxes.for_principal(principal.key)
        if binding.status == "recovery_required":
            return "recovery_required", "state_unavailable"
        if binding.status != "ok" or binding.mailbox is None:
            return "denied", "agent_not_registered"
        return AgentMessageContext(principal, binding.mailbox)

    def agent_transition_failure(
        failure: tuple[
            Literal["denied", "recovery_required"],
            Literal["state_unavailable", "no_current_grant", "agent_not_registered"],
        ],
    ) -> MessageTransitionResult:
        if failure[1] == "agent_not_registered":
            return MessageTransitionResult(
                status=failure[0], reason="receiving_binding_changed"
            )
        return MessageTransitionResult(status=failure[0], reason=failure[1])

    def agent_runtime() -> RuntimeCurrentness | MessageTransitionResult:
        try:
            session_id = generation()
        except (KeyError, RuntimeError, ValueError):
            return MessageTransitionResult(
                status="recovery_required", reason="runtime_currentness_unavailable"
            )
        if not session_id:
            return MessageTransitionResult(
                status="recovery_required", reason="runtime_currentness_unavailable"
            )
        return RuntimeCurrentness(
            generation=session_id, current_generation=session_id,
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
                stored = await mailboxes.register_agent(name, principal.key)
                result = (
                    AgentRegistrationResult(status="ok", name=stored.mailbox.name)
                    if stored.status == "ok" and stored.mailbox is not None
                    else AgentRegistrationResult(status=stored.status, reason=stored.reason)
                )
        audited("agent_register", name, result.status)
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
        route = MessageRoute(
            recipient_work_id=recipient.mailbox.work_id,
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
            sender.work_id, route, submitted,
        )
        if result.status != "ok" or result.message is None:
            return AgentMessageSubmitResult(status=result.status, reason=result.reason)
        view = await public_message(mailboxes, result.message)
        if view is None:
            return AgentMessageSubmitResult(
                status="recovery_required", reason="state_unavailable"
            )
        return AgentMessageSubmitResult(status="ok", message=view)

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
            mailbox.work_id,
            MessagePendingRequest(
                api_version=api_version,
                grant_version=mailbox.generation,
                cursor=cursor,
                limit=limit,
            ),
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
        runtime = agent_runtime()
        if isinstance(runtime, MessageTransitionResult):
            return runtime
        mailbox = context.mailbox
        assert service.messages is not None
        key = (mailbox.work_id, mailbox.generation)
        lock = agent_currentness_locks.setdefault(key, asyncio.Lock())
        async with lock:
            retired = agent_retired_generations.setdefault(key, set())
            current = agent_current_generations.get(key)
            if (
                runtime.generation in retired
                or current is not None and current != runtime.generation
            ):
                return stale_runtime()
            result = await service.messages.receive_admitted(
                mailbox.work_id, mailbox.generation, runtime,
                MessageReceiveRequest(
                    api_version=api_version,
                    delivery_id=delivery_id,
                    grant_version=mailbox.generation,
                ),
            )
            if result.status == "ok" and current is None:
                agent_current_generations[key] = runtime.generation
            return result

    async def agent_message_recover(
        api_version: Literal["1"], delivery_id: UUID,
    ) -> MessageTransitionResult:
        """Explicitly recover a received delivery after an authorized binding/session replacement."""
        context = await agent_context()
        if isinstance(context, tuple):
            return agent_transition_failure(context)
        runtime = agent_runtime()
        if isinstance(runtime, MessageTransitionResult):
            return runtime
        mailbox = context.mailbox
        assert service.messages is not None
        key = (mailbox.work_id, mailbox.generation)
        lock = agent_currentness_locks.setdefault(key, asyncio.Lock())
        async with lock:
            retired = agent_retired_generations.setdefault(key, set())
            previous = agent_current_generations.get(key)
            if runtime.generation in retired:
                return stale_runtime()
            result = await service.messages.recover_admitted(
                mailbox.work_id, mailbox.generation, runtime,
                MessageReceiveRequest(
                    api_version=api_version,
                    delivery_id=delivery_id,
                    grant_version=mailbox.generation,
                ),
            )
            if result.status == "ok":
                if previous is not None and previous != runtime.generation:
                    retired.add(previous)
                agent_current_generations[key] = runtime.generation
            return result

    async def agent_message_result_send(
        api_version: Literal["1"], delivery_id: UUID,
        message_id: UUID, payload: JsonValue,
    ) -> AgentMessageSubmitResult:
        """Reply to one received delivery as this registered agent name."""
        context = await agent_context()
        if isinstance(context, tuple):
            return AgentMessageSubmitResult(status=context[0], reason=context[1])
        runtime = agent_runtime()
        if isinstance(runtime, MessageTransitionResult):
            return AgentMessageSubmitResult(status=runtime.status, reason=runtime.reason)
        mailbox = context.mailbox
        assert mailboxes is not None and service.messages is not None
        key = (mailbox.work_id, mailbox.generation)
        lock = agent_currentness_locks.setdefault(key, asyncio.Lock())
        async with lock:
            retired = agent_retired_generations.setdefault(key, set())
            current = agent_current_generations.get(key)
            if runtime.generation in retired or current != runtime.generation:
                return AgentMessageSubmitResult(
                    status="stale", reason="runtime_generation_changed"
                )
            reply = await service.messages.reply_context(delivery_id)
            if reply is None:
                return AgentMessageSubmitResult(
                    status="conflict", reason="reply_delivery_not_found"
                )
            recipient = await mailboxes.by_work_id(reply.recipient_work_id)
            if recipient.status != "ok" or recipient.mailbox is None:
                return AgentMessageSubmitResult(
                    status="recovery_required", reason="state_unavailable"
                )
            route = MessageRoute(
                recipient_work_id=recipient.mailbox.work_id,
                recipient_grant_version=recipient.mailbox.generation,
            )
            submitted = MessageSubmitRequest(
                api_version=api_version,
                message_id=message_id,
                grant_version=mailbox.generation,
                route_ref=reply.route_ref,
                kind="result",
                payload=payload,
                in_reply_to_delivery_id=delivery_id,
            )
            result = await service.messages.submit_received_result(
                mailbox.work_id, mailbox.generation, runtime.generation,
                route, submitted,
            )
        if result.status != "ok" or result.message is None:
            return AgentMessageSubmitResult(status=result.status, reason=result.reason)
        view = await public_message(mailboxes, result.message)
        if view is None:
            return AgentMessageSubmitResult(
                status="recovery_required", reason="state_unavailable"
            )
        return AgentMessageSubmitResult(status="ok", message=view)

    async def agent_message_disposition(
        api_version: Literal["1"], delivery_id: UUID, result_message_id: UUID,
    ) -> MessageTransitionResult:
        """Disposition one received delivery using its exact reply as evidence."""
        context = await agent_context()
        if isinstance(context, tuple):
            return agent_transition_failure(context)
        runtime = agent_runtime()
        if isinstance(runtime, MessageTransitionResult):
            return runtime
        mailbox = context.mailbox
        assert service.messages is not None
        key = (mailbox.work_id, mailbox.generation)
        lock = agent_currentness_locks.setdefault(key, asyncio.Lock())
        async with lock:
            retired = agent_retired_generations.setdefault(key, set())
            current = agent_current_generations.get(key)
            if runtime.generation in retired or current != runtime.generation:
                return stale_runtime()
            evidence = DispositionEvidence(
                kind="result", result_message_id=result_message_id,
            )
            result = await service.messages.disposition_admitted(
                mailbox.work_id, mailbox.generation, runtime,
                MessageDispositionRequest(
                    api_version=api_version,
                    delivery_id=delivery_id,
                    grant_version=mailbox.generation,
                    disposition_digest=disposition_digest(evidence),
                    evidence=evidence,
                ),
            )
            if result.status == "ok" and result.state == "DISPOSITIONED":
                has_received = await service.messages.has_received(
                    mailbox.work_id, mailbox.generation
                )
                if has_received is False:
                    agent_current_generations.pop(key, None)
            return result

    return (
        ("work_get", work_get),
        ("work_search", work_search),
        ("work_resolve_reference", work_resolve_reference),
        ("work_resolve_alias", work_resolve_alias),
        ("work_structure", work_structure),
        ("work_history", work_history),
        ("work_attachments", work_attachments),
        ("work_event", work_event),
        ("source_task", source_task),
        ("source_stories", source_stories),
        ("source_story", source_story),
        ("work_append", work_append),
        ("work_create", work_create),
        ("work_update", work_update),
        ("work_relate", work_relate),
        ("required_result_save", required_result_save),
        *build_message_tools(service, audit),
        ("message_receive", message_receive),
        ("message_recover", message_recover),
        ("message_result_send", message_result_send),
        ("message_disposition", message_disposition),
        ("agent_register", agent_register),
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
        closed_tool(server, name, function)
    return server
