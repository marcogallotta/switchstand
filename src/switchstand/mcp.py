import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, JsonValue
from sqlalchemy.ext.asyncio import create_async_engine

from .canonical_event_reads import CanonicalEventReader
from .canonical_relations import CanonicalRelationsRepository
from .canonical_work import CanonicalWorkRepository
from .canonical_work_runtime import CanonicalWorkRuntime
from .contracts import (
    AppendResult,
    ClosedModel,
    LaunchAuthority,
    Status,
    WorkAppendRequest,
    WorkEventRequest,
    WorkEventResult,
    WorkGetRequest,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkResult,
    WorkSearchItem,
)
from .core import Controller
from .grant_state import GrantState
from .grants import (
    GrantedWorkResult,
    GuardOutcome,
    PrincipalContext,
    ProtectedUpdate,
    ScalarPatch,
)
from .managed_identity import managed_principal
from .messages import (
    DispositionEvidence,
    MessageDispositionRequest,
    MessagePendingRequest,
    MessagePendingResult,
    MessageReceiveRequest,
    MessageSendRequest,
    MessageState,
    MessageSubmitResult,
    MessageTransitionResult,
    RuntimeCurrentness,
    current_message_grant_version,
    disposition_digest,
    pending_managed_messages,
    send_managed_result,
)
from .priority_claim_service import (
    PriorityClaimReadResult,
    PriorityClaimService,
    PriorityClaimWrite,
)
from .priority_claims import PriorityBand, PriorityClaimRepository, RelationKind
from .priority_context import PriorityContextProjection, PriorityContextResult
from .run import managed_runtime_currentness
from .state import PostgresState
from .task_runs import (
    AgentTaskRequest,
    AgentTaskResult,
    TaskRunRequestResult,
    TaskRunResultResult,
    TaskRunState,
    task_request_operation_id,
)
from .work_events import WorkEventRepository


class PublicWorkItem(WorkSearchItem):
    notes: str


class PublicReadGuard(ClosedModel):
    status: Literal["denied"] = "denied"
    operation: Literal["work_get"] = "work_get"
    reason: str
    next_action: str
    effect: Literal["not_sent"] = "not_sent"
    retry: Literal["none"] = "none"


class PublicWorkResult(ClosedModel):
    status: Status
    item: PublicWorkItem | None = None
    guard: PublicReadGuard | None = None


def project_work(result: WorkResult | GrantedWorkResult) -> PublicWorkResult:
    """Present already-authorized work; never retrieve, authorize, or invent identities."""
    public = PublicWorkResult(status=result.status)
    if isinstance(result, GrantedWorkResult) and result.guard is not None:
        public.guard = PublicReadGuard(reason=result.guard.reason, next_action=result.guard.next_action)
    if result.status not in {"ok", "stale"}:
        return public
    if result.item is not None:
        item = result.item
        public.item = PublicWorkItem(id=item.id, title=item.title, notes=item.notes,
                                     completed=item.completed, revision=item.revision,
                                     routing=item.routing, context=item.context,
                                     admitted_at=item.admitted_at)
    return public


class _CanonicalController(Controller):
    """Keep the managed MCP contract while projecting reads from canonical storage."""

    def __init__(
        self, authority: LaunchAuthority, state: PostgresState,
        work: CanonicalWorkRuntime, events: CanonicalEventReader,
    ):
        super().__init__(authority, state, {})
        self.work, self.events = work, events

    async def get(self, request: WorkGetRequest) -> WorkResult:
        if not self.authority.can_read(request.work_id):
            return WorkResult(status="denied")
        return await self.work.get(request.work_id)

    async def history(self, request: WorkHistoryRequest) -> WorkHistoryResult:
        if not self.authority.can_read(request.work_id):
            return WorkHistoryResult(status="denied")
        return await self.events.history(request)

    async def event(self, request: WorkEventRequest) -> WorkEventResult:
        if not self.authority.can_read(request.work_id):
            return WorkEventResult(status="denied")
        return await self.events.event(request)


def controller_from_env() -> _CanonicalController:
    references = tuple(UUID(value) for value in os.getenv("REFERENCE_WORK_IDS", "").split(",") if value)
    authority = LaunchAuthority(active_work_id=UUID(os.environ["ACTIVE_WORK_ID"]), reference_work_ids=references)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    repository = CanonicalWorkRepository(engine)
    return _CanonicalController(
        authority, PostgresState(engine),
        CanonicalWorkRuntime(repository, CanonicalRelationsRepository(engine)),
        CanonicalEventReader(repository, WorkEventRepository(engine)),
    )


def closed_tool(
    server: MCPServer, name: str, function: Callable[..., Any],
    annotations: ToolAnnotations | None = None,
) -> None:
    server.tool(name=name, annotations=annotations)(function)
    tool = server._tool_manager.get_tool(name)  # pyright: ignore[reportPrivateUsage]
    assert tool is not None
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)


def register_priority_tools(
    server: MCPServer,
    active_work_id: UUID,
    reference_work_ids: tuple[UUID, ...],
    *,
    priority_claims: PriorityClaimService | None,
    priority_context: PriorityContextProjection | None,
    grants: GrantState | None,
    principal: PrincipalContext | None,
) -> None:
    if priority_claims is not None and grants is not None and principal is not None:
        async def _priority_claim_get(
            api_version: Literal["1"],
        ) -> PriorityClaimReadResult:
            """Read current priority claims for the exact launch-bound work."""
            del api_version
            return await priority_claims.current("WORK", active_work_id)

        async def _priority_claim_record(
            api_version: Literal["1"], operation_id: UUID,
            observed_revision: Annotated[str, Field(min_length=1)],
            relation_kind: RelationKind,
            rationale: Annotated[str, Field(min_length=1, max_length=300)],
            relation_target_id: UUID | None = None,
            band: PriorityBand | None = None,
            supersedes_claim_id: UUID | None = None,
        ) -> GuardOutcome:
            """Record an AGENT recommendation for the exact launch-bound work."""
            grant = await grants.current(principal.key)
            request = PriorityClaimWrite(
                api_version=api_version,
                operation_id=operation_id,
                work_id=active_work_id,
                grant_version=1 if grant is None else grant.version,
                observed_revision=observed_revision,
                relation_kind=relation_kind,
                rationale=rationale,
                relation_target_id=relation_target_id,
                band=band,
                supersedes_claim_id=supersedes_claim_id,
            )
            return await priority_claims.record(grants, principal, request)

        closed_tool(server, "priority_claim_get", _priority_claim_get)
        closed_tool(server, "priority_claim_record", _priority_claim_record)
    if priority_context is not None:
        async def _priority_context_get(
            api_version: Literal["1"], include_references: bool = False,
        ) -> PriorityContextResult:
            """Project priority context for the launch-bound work and optional references."""
            del api_version
            work_ids = (
                (active_work_id, *reference_work_ids)
                if include_references else (active_work_id,)
            )
            return await priority_context.project(work_ids)

        closed_tool(server, "priority_context_get", _priority_context_get)


def _restrict_server_tools(server: MCPServer, allowed: frozenset[str]) -> MCPServer:
    """Keep one canonical managed implementation while exposing only launch-approved tools."""
    registered = set(server._tool_manager._tools)  # pyright: ignore[reportPrivateUsage]
    unknown = allowed - registered
    if unknown:
        raise ValueError(f"managed tool policy names unregistered tools: {sorted(unknown)}")
    server._tool_manager._tools = {  # pyright: ignore[reportPrivateUsage]
        name: tool for name, tool in server._tool_manager._tools.items()  # pyright: ignore[reportPrivateUsage]
        if name in allowed
    }
    return server


def _context_tools(priority_claims: bool) -> frozenset[str]:
    tools = {"work_get", "work_history"}
    if priority_claims:
        tools.update({"priority_claim_get", "priority_claim_record", "priority_context_get"})
    return frozenset(tools)


def build_context_server(
    service: object,
    active_work_id: UUID,
    reference_work_ids: tuple[UUID, ...] = (),
    *,
    priority_claims: PriorityClaimService | None = None,
    priority_context: PriorityContextProjection | None = None,
    grants: GrantState | None = None,
    principal: PrincipalContext | None = None,
) -> MCPServer:
    """Compatibility wrapper over the canonical managed server; no parallel tool inventory."""
    server = build_server(
        service, active_work_id, reference_work_ids,
        priority_claims=priority_claims, priority_context=priority_context,
        grants=grants, principal=principal,
    )
    return _restrict_server_tools(server, _context_tools(priority_claims is not None))


def build_server(
    service: object, active_work_id: UUID, reference_work_ids: tuple[UUID, ...] = (), *,
    messages: MessageState | None = None, grants: GrantState | None = None,
    principal: PrincipalContext | None = None,
    currentness: Callable[[], RuntimeCurrentness | None] | None = None,
    updates: Callable[[PrincipalContext, ProtectedUpdate], Awaitable[GuardOutcome]] | None = None,
    task_runs: TaskRunState | None = None,
    priority_claims: PriorityClaimService | None = None,
    priority_context: PriorityContextProjection | None = None,
) -> MCPServer:
    server = MCPServer("Switchstand")

    async def _work_get(
        api_version: Literal["1"], work_id: UUID | None = None,
    ) -> PublicWorkResult:
        request = WorkGetRequest(api_version=api_version, work_id=work_id or active_work_id)
        return project_work(await service.get(request))  # type: ignore[attr-defined]

    references = ", ".join(map(str, reference_work_ids)) or "none"
    _work_get.__doc__ = (
        "Read launch-bound work. Omit work_id for the active assignment. "
        f"Bounded read-only reference WorkIds: {references}."
    )

    async def _work_history(
        api_version: Literal["1"], observed_revision: str, work_id: UUID | None = None,
        cursor: str | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> WorkHistoryResult:
        """Read bounded history; on stale, repeat work_get and restart pagination."""
        return await service.history(  # type: ignore[attr-defined]
            WorkHistoryRequest(api_version=api_version, work_id=work_id or active_work_id,
                               observed_revision=observed_revision, cursor=cursor, limit=limit))

    async def _work_event(
        api_version: Literal["1"], event_id: UUID, observed_revision: str,
        work_id: UUID | None = None,
    ) -> WorkEventResult:
        """Reread one opaque event at the observed work revision."""
        return await service.event(  # type: ignore[attr-defined]
            WorkEventRequest(api_version=api_version, work_id=work_id or active_work_id,
                             event_id=event_id, observed_revision=observed_revision))

    async def _work_append(api_version: Literal["1"], work_id: UUID, text: str) -> AppendResult:
        """Append one history entry to the active work item and return exact Asana effect identity."""
        return await service.append(WorkAppendRequest(api_version=api_version, work_id=work_id, text=text))  # type: ignore[attr-defined]

    closed_tool(server, "work_get", _work_get)
    closed_tool(server, "work_history", _work_history)
    closed_tool(server, "work_event", _work_event)
    closed_tool(server, "work_append", _work_append)
    if updates is not None and grants is not None and principal is not None:
        async def _work_update(
            api_version: Literal["1"], operation_id: UUID,
            observed_revision: Annotated[str, Field(min_length=1)], patch: ScalarPatch,
        ) -> GuardOutcome:
            grant = await grants.current(principal.key)
            request = ProtectedUpdate(
                api_version=api_version, operation_id=operation_id, work_id=active_work_id,
                grant_version=1 if grant is None else grant.version,
                observed_revision=observed_revision, patch=patch,
            )
            return await updates(principal, request)

        closed_tool(server, "work_update", _work_update)
    register_priority_tools(
        server, active_work_id, reference_work_ids,
        priority_claims=priority_claims, priority_context=priority_context,
        grants=grants, principal=principal,
    )
    if messages is not None and grants is not None and principal is not None and currentness is not None:
        def runtime() -> RuntimeCurrentness:
            return currentness() or RuntimeCurrentness(
                generation="unavailable", current_generation=None
            )

        async def _message_pending(
            api_version: Literal["1"],
            cursor: UUID | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 50,
        ) -> MessagePendingResult:
            return await pending_managed_messages(
                cast(Controller, service).state,
                grants,
                messages,
                principal,
                active_work_id,
                MessagePendingRequest(
                    api_version=api_version, grant_version=1,
                    cursor=cursor, limit=limit,
                ),
                runtime(),
            )

        async def transition(
            operation: Literal["receive", "recover"], api_version: Literal["1"],
            delivery_id: UUID,
        ) -> MessageTransitionResult:
            version = await current_message_grant_version(grants, principal)
            if version is None:
                return MessageTransitionResult(
                    status="recovery_required", reason="state_unavailable"
                )
            request = MessageReceiveRequest(
                api_version=api_version, delivery_id=delivery_id,
                grant_version=version,
            )
            return await getattr(messages, operation)(principal, runtime(), request)

        async def _message_receive(
            api_version: Literal["1"], delivery_id: UUID,
        ) -> MessageTransitionResult:
            return await transition("receive", api_version, delivery_id)

        async def _message_recover(
            api_version: Literal["1"], delivery_id: UUID,
        ) -> MessageTransitionResult:
            return await transition("recover", api_version, delivery_id)

        async def _message_result_send(
            api_version: Literal["1"], in_reply_to_delivery_id: UUID,
            message_id: UUID, payload: JsonValue,
        ) -> MessageSubmitResult:
            return await send_managed_result(
                cast(Controller, service).state,
                grants,
                messages,
                principal,
                MessageSendRequest(
                    api_version=api_version,
                    work_id=active_work_id,
                    grant_version=1,
                    message_id=message_id,
                    payload=payload,
                    in_reply_to_delivery_id=in_reply_to_delivery_id,
                ),
                runtime(),
            )

        async def _message_disposition(
            api_version: Literal["1"], delivery_id: UUID,
            result_message_id: UUID,
        ) -> MessageTransitionResult:
            evidence = DispositionEvidence(kind="result", result_message_id=result_message_id)
            version = await current_message_grant_version(grants, principal)
            if version is None:
                return MessageTransitionResult(
                    status="recovery_required", reason="state_unavailable"
                )
            return await messages.disposition(principal, runtime(), MessageDispositionRequest(
                api_version=api_version, delivery_id=delivery_id,
                grant_version=version,
                disposition_digest=disposition_digest(evidence),
                evidence=evidence,
            ))

        closed_tool(server, "message_pending", _message_pending)
        closed_tool(server, "message_receive", _message_receive)
        closed_tool(server, "message_recover", _message_recover)
        closed_tool(server, "message_result_send", _message_result_send)
        closed_tool(server, "message_disposition", _message_disposition)
    if task_runs is not None and grants is not None and principal is not None and currentness is not None:
        async def _agent_task_request(
            api_version: Literal["1"], execution_work_id: UUID,
            observed_revision: Annotated[str, Field(min_length=1, max_length=512)],
            task_kind: Literal["INVESTIGATION", "VALIDATION"],
            objective: Annotated[str, Field(min_length=1, max_length=8000)],
            result_contract: dict[str, JsonValue],
            continuation: Literal["START", "CONTINUE", "TAKEOVER"] = "START",
            candidate_ref: Annotated[str | None, Field(min_length=1, max_length=1024)] = None,
        ) -> TaskRunRequestResult:
            """Persist one launch-bound investigation or validation request; never launch it."""
            async with grants.locked(principal.key) as grant:
                if grant is None or not grant.current():
                    return TaskRunRequestResult(status="denied", reason="no_current_grant")
                if (
                    principal != managed_principal(active_work_id)
                    or grant.principal != principal
                    or grant.scope != "launch"
                    or grant.authority.active_work_id != active_work_id
                    or not grant.can_write(active_work_id)
                    or "agent_task" not in grant.operations
                    or execution_work_id != active_work_id
                ):
                    return TaskRunRequestResult(
                        status="denied", reason="operation_not_granted"
                    )
                runtime = currentness()
                if runtime is None or runtime.current_generation is None:
                    return TaskRunRequestResult(
                        status="unknown", reason="runtime_currentness_unavailable"
                    )
                if runtime.generation != runtime.current_generation:
                    return TaskRunRequestResult(
                        status="stale", reason="requester_run_superseded"
                    )
                request = AgentTaskRequest(
                    api_version=api_version,
                    execution_work_id=execution_work_id,
                    observed_revision=observed_revision,
                    task_kind=task_kind,
                    objective=objective,
                    result_contract=result_contract,
                    continuation=continuation,
                    candidate_ref=candidate_ref,
                )
                return await task_runs.request(
                    active_work_id,
                    task_request_operation_id(active_work_id, request),
                    request,
                )

        async def _agent_task_result(
            api_version: Literal["1"], request_id: UUID, result_id: UUID,
            outcome: Annotated[str, Field(min_length=1, max_length=128)],
            summary: Annotated[str, Field(min_length=1, max_length=8000)],
            evidence_refs: Annotated[tuple[str, ...], Field(max_length=64)] = (),
        ) -> TaskRunResultResult:
            """Persist evidence for an exact pre-bound managed task execution."""
            async with grants.locked(principal.key) as grant:
                if grant is None or not grant.current():
                    return TaskRunResultResult(status="denied", reason="no_current_grant")
                if (
                    principal != managed_principal(active_work_id)
                    or grant.principal != principal
                    or grant.scope != "launch"
                    or grant.authority.active_work_id != active_work_id
                    or not grant.can_write(active_work_id)
                    or "agent_task" not in grant.operations
                ):
                    return TaskRunResultResult(
                        status="denied", reason="operation_not_granted"
                    )
                runtime = currentness()
                if runtime is None:
                    return TaskRunResultResult(
                        status="unknown", reason="runtime_currentness_unavailable"
                    )
                return await task_runs.submit_result(
                    request_id,
                    result_id,
                    runtime,
                    AgentTaskResult(
                        api_version=api_version,
                        outcome=outcome,
                        summary=summary,
                        evidence_refs=evidence_refs,
                    ),
                )

        closed_tool(server, "agent_task_request", _agent_task_request)
        closed_tool(server, "agent_task_result", _agent_task_result)
    return server


def server_from_env() -> MCPServer:
    if os.getenv("SWITCHSTAND_MANAGED") != "1":
        return MCPServer("Switchstand (unbound)")
    service = controller_from_env()
    engine = cast(PostgresState, service.state).engine
    grants = GrantState(engine)
    messages = MessageState(engine, grants)
    work = service.work
    task_runs = TaskRunState(engine, work.works)
    active = service.authority.active_work_id
    priority_claims = None
    priority_context = None
    if os.getenv("SWITCHSTAND_PRIORITY_CLAIMS") == "1":
        priority_claims = PriorityClaimService(
            PriorityClaimRepository(engine), work.works,
        )
        priority_context = PriorityContextProjection(
            works=work.works,
            relations=work.relations,
            claims=priority_claims,
        )

    def currentness() -> RuntimeCurrentness | None:
        try:
            return managed_runtime_currentness(
                os.environ["SWITCHSTAND_RUN_ID"], active,
                Path(os.environ["SWITCHSTAND_WORKTREE"]), os.environ["SWITCHSTAND_BRANCH"],
                Path(os.environ["SWITCHSTAND_GIT_DIR"]),
                Path(os.getenv("SWITCHSTAND_PROC_ROOT", "/proc")),
            )
        except KeyError:
            return None
    server = build_server(
        service, active, service.authority.reference_work_ids,
        messages=messages, grants=grants, principal=managed_principal(active),
        currentness=currentness,
        updates=lambda principal, request: work.protected_update(grants, principal, request),
        task_runs=task_runs,
        priority_claims=priority_claims,
        priority_context=priority_context,
    )
    if os.getenv("SWITCHSTAND_MANAGED_PROFILE") == "context":
        return _restrict_server_tools(server, _context_tools(priority_claims is not None))
    return server


def protect_provider_logs() -> None:
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False


def main() -> None:
    protect_provider_logs()
    if os.getenv("SWITCHSTAND_MANAGED_PROFILE") == "context":
        from .provision import require_current_schema
        require_current_schema()
    server_from_env().run()


if __name__ == "__main__":
    main()
