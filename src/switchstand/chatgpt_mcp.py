import asyncio
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server import MCPServer
from pydantic import Field, JsonValue

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
    GrantResult,
    GuardOutcome,
    PrincipalContext,
    ProtectedAppend,
    ProtectedCreate,
    ProtectedUpdate,
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
    MessageSendRequest,
    MessageSubmitResult,
    MessageTransitionResult,
    RuntimeCurrentness,
    disposition_digest,
    send_received_result,
)


def build_message_tools(
    service: ChatGPTService,
    audit: Callable[[str, str | None, str], None] | None = None,
) -> tuple[tuple[str, Callable[..., Any]], ...]:
    async def message_send(
        api_version: Literal["1"], work_id: UUID, grant_version: int,
        message_id: UUID, payload: JsonValue,
        route_ref: Annotated[str | None, Field(min_length=1)] = None,
        recipient_work_id: UUID | None = None,
        in_reply_to_delivery_id: UUID | None = None,
    ) -> MessageSubmitResult:
        """Durably send one request or exactly correlated result."""
        result = await service.message_send(MessageSendRequest(
            api_version=api_version, work_id=work_id, grant_version=grant_version,
            message_id=message_id, route_ref=route_ref, payload=payload,
            recipient_work_id=recipient_work_id,
            in_reply_to_delivery_id=in_reply_to_delivery_id,
        ))
        if audit is not None:
            audit("message_send", f"{work_id}:{message_id}", result.status)
        return result

    async def message_pending(
        api_version: Literal["1"], work_id: UUID, grant_version: int,
        cursor: UUID | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> MessagePendingResult:
        """Inspect durable pending deliveries for one explicitly admitted actor."""
        result = await service.message_pending(work_id, MessagePendingRequest(
            api_version=api_version, grant_version=grant_version, cursor=cursor, limit=limit,
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

    def audited(tool: str, target: str | None, status: str) -> None:
        if audit is not None:
            audit(tool, target, status)

    async def grant_get(api_version: Literal["1"]) -> GrantResult:
        """Read this authenticated caller's current grant; this never issues or changes a grant."""
        result = await service.grant_get()
        audited("grant_get", None, result.status)
        return result

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
        grant_version: int, observed_revision: str, text: str,
    ) -> GuardOutcome:
        """Append through the current grant. Reuse OperationId; UNKNOWN forbids new-ID retry."""
        result = await service.append(ProtectedAppend(
            api_version=api_version, operation_id=operation_id, work_id=work_id,
            grant_version=grant_version, observed_revision=observed_revision, text=text,
        ))
        audited("work_append", str(work_id), result.status)
        return result

    async def work_create(
        api_version: Literal["1"], operation_id: UUID, parent_work_id: UUID,
        grant_version: int, title: str, notes: str = "",
    ) -> GuardOutcome:
        """Create only through a test-qualified grant. Reuse OperationId to reconcile UNKNOWN."""
        result = await service.create(ProtectedCreate(
            api_version=api_version, operation_id=operation_id, parent_work_id=parent_work_id,
            grant_version=grant_version, title=title, notes=notes,
        ))
        audited("work_create", str(parent_work_id), result.status)
        return result

    async def work_update(
        api_version: Literal["1"], operation_id: UUID, work_id: UUID,
        grant_version: int, observed_revision: str, patch: ScalarPatch,
    ) -> GuardOutcome:
        """Set bounded scalar state. Reuse OperationId to reconcile UNKNOWN without resending."""
        result = await service.update(ProtectedUpdate(
            api_version=api_version, operation_id=operation_id, work_id=work_id,
            grant_version=grant_version, observed_revision=observed_revision, patch=patch,
        ))
        audited("work_update", str(work_id), result.status)
        return result

    async def required_result_save(
        api_version: Literal["1"], work_id: UUID, grant_version: int,
        observed_revision: str, text: Annotated[str, Field(min_length=1, max_length=8000)],
    ) -> GuardOutcome:
        """Save one required result; the server owns its stable operation identity."""
        result = await service.required_result_save(RequiredResultSaveRequest(
            api_version=api_version, work_id=work_id, grant_version=grant_version,
            observed_revision=observed_revision, text=text,
        ))
        audited("required_result_save", str(work_id), result.status)
        return result

    async def message_context(
        work_id: UUID, grant_version: int,
    ) -> tuple[
        PrincipalContext, WorkGrant, tuple[str, UUID, UUID, int], str
    ] | MessageTransitionResult:
        grant_result = await service.grant_get()
        if (
            grant_result.status != "ok"
            or grant_result.principal is None
            or grant_result.grant is None
        ):
            return MessageTransitionResult(status="denied", reason="no_current_grant")
        principal, grant = grant_result.principal, grant_result.grant
        if grant.version != grant_version:
            return MessageTransitionResult(status="stale", reason="grant_version_changed")
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

    async def message_receive(
        api_version: Literal["1"], work_id: UUID, grant_version: int, delivery_id: UUID,
    ) -> MessageTransitionResult:
        """Receive one exact delivery under this server-owned MCP session generation."""
        context = await message_context(work_id, grant_version)
        if isinstance(context, MessageTransitionResult):
            audited("message_receive", str(work_id), context.status)
            return context
        principal, _grant, namespace, session_id = context
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
                        grant_version=grant_version,
                    ),
                    work_id=work_id,
                )
                if result.status == "ok" and current is None:
                    current_generations[namespace] = session_id
        audited("message_receive", str(work_id), result.status)
        return result

    async def message_recover(
        api_version: Literal["1"], work_id: UUID, grant_version: int, delivery_id: UUID,
    ) -> MessageTransitionResult:
        """Explicitly transfer one received delivery to this replacement MCP session."""
        context = await message_context(work_id, grant_version)
        if isinstance(context, MessageTransitionResult):
            audited("message_recover", str(work_id), context.status)
            return context
        principal, _grant, namespace, session_id = context
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
                        grant_version=grant_version,
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
        api_version: Literal["1"], work_id: UUID, grant_version: int,
        in_reply_to_delivery_id: UUID, message_id: UUID, payload: JsonValue,
    ) -> MessageSubmitResult:
        """Send a result only from the current MCP session bound to the received delivery."""
        context = await message_context(work_id, grant_version)
        if isinstance(context, MessageTransitionResult):
            result = MessageSubmitResult(status="recovery_required", reason="state_unavailable")
            audited("message_result_send", str(work_id), result.status)
            return result
        principal, _grant, namespace, session_id = context
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
                    status="recovery_required", reason="state_unavailable"
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
                        grant_version=grant_version,
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
        api_version: Literal["1"], work_id: UUID, grant_version: int,
        delivery_id: UUID, result_message_id: UUID,
    ) -> MessageTransitionResult:
        """Disposition one received delivery only from its current MCP session."""
        context = await message_context(work_id, grant_version)
        if isinstance(context, MessageTransitionResult):
            audited("message_disposition", str(work_id), context.status)
            return context
        principal, _grant, namespace, session_id = context
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
                        grant_version=grant_version,
                        disposition_digest=disposition_digest(evidence),
                        evidence=evidence,
                    ),
                    work_id=work_id,
                )
        audited("message_disposition", str(work_id), result.status)
        return result

    return (
        ("grant_get", grant_get),
        ("work_get", work_get),
        ("work_search", work_search),
        ("work_resolve_reference", work_resolve_reference),
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
        ("required_result_save", required_result_save),
        *build_message_tools(service, audit),
        ("message_receive", message_receive),
        ("message_recover", message_recover),
        ("message_result_send", message_result_send),
        ("message_disposition", message_disposition),
    )


def build_chatgpt_server(service: ChatGPTService, server: MCPServer | None = None) -> MCPServer:
    """A host may supply an OAuth-configured server; default stdio is test-adapter only."""
    server = server or MCPServer("Switchstand ChatGPT")
    for name, function in build_ordinary_tools(service):
        closed_tool(server, name, function)
    return server
