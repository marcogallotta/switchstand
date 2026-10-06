"""Default-off authenticated ChatGPT MCP HTTP edge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.tools import Tool
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import AnyHttpUrl
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .activation_continuity import ActivationContract
from .activation_continuity_store import ActivationContinuity
from .activation_contract_loader import load_activation_contracts_from_environment
from .agent_mailboxes import AgentMailboxState
from .canonical_event_reads import CanonicalEventReader
from .canonical_relations import CanonicalRelationsRepository
from .canonical_work import CanonicalWorkRepository
from .canonical_work_runtime import CanonicalWorkRuntime
from .chatgpt import ChatGPTService
from .chatgpt_mcp import build_ordinary_tools, ordinary_tool_annotations
from .grant_state import GrantState
from .grants import PrincipalContext
from .implementation_requests import ImplementationRequestState
from .lifecycle import LifecycleRepository, RequiredResultPersistence
from .messages import MessageState
from .oauth_continuity import (
    FASTMCP_ACCESS_TOKEN_LIFETIME_SECONDS,
    SwitchstandGitHubProvider,
)
from .observability import CallTimingMiddleware, annotate_target, register_sqlalchemy_timing
from .principal import RequestPrincipal
from .priority_claim_service import PriorityClaimService
from .priority_claims import PriorityClaimRepository
from .priority_context import PriorityContextProjection
from .product_currentness import (
    STATEFUL_PRODUCT_WORK_ID,
    ProductCurrentness,
    evaluate_stateful_currentness,
)
from .product_currentness_stateful import LiveStatefulEvidenceReader, StatefulServerSnapshot
from .reviews import ReviewOccurrenceState, ReviewPolicy
from .stable_auth import (
    REQUIRED_SCOPE,
    IntrospectionTokenVerifier,
    delegated_auth,
    normalize_resource_url,
)
from .state import PostgresState
from .work_events import WorkEventRepository

LOG = logging.getLogger(__name__)
CERTIFICATION_RUNTIME_PATH = "/.well-known/switchstand-certification-runtime"
GRACEFUL_SHUTDOWN_SECONDS = 30
STATEFUL_MIGRATION_REVISION = "0023_activation_continuity"
_https_resource_url = normalize_resource_url


@dataclass(frozen=True, slots=True)
class _ProductCurrentnessConfig:
    runtime_sha: str
    selected_runtime_sha: str
    run_id: str
    expected_tools_schema_sha256: str
    qualification_receipt: Path | None = None
    qualification_key: Path | None = None

    @classmethod
    def from_environment(cls) -> _ProductCurrentnessConfig | None:
        enabled = os.getenv("SWITCHSTAND_PRODUCT_CURRENTNESS", "").strip()
        if enabled not in {"", "0", "1"}:
            raise ValueError("SWITCHSTAND_PRODUCT_CURRENTNESS must be 0 or 1")
        if enabled != "1":
            return None
        names = {
            "runtime_sha": "SWITCHSTAND_PRODUCT_CURRENTNESS_RUNTIME_SHA",
            "selected_runtime_sha": "SWITCHSTAND_PRODUCT_CURRENTNESS_SELECTED_RUNTIME_SHA",
            "run_id": "SWITCHSTAND_PRODUCT_CURRENTNESS_RUN_ID",
            "expected_tools_schema_sha256": (
                "SWITCHSTAND_PRODUCT_CURRENTNESS_EXPECTED_TOOLS_SCHEMA_SHA256"
            ),
        }
        values = {field: os.getenv(variable, "").strip() for field, variable in names.items()}
        missing = [names[field] for field, value in values.items() if not value]
        if missing:
            raise ValueError(
                "required product-currentness configuration missing: " + ", ".join(missing)
            )
        if not re.fullmatch(r"[0-9a-f]{40}", values["runtime_sha"]):
            raise ValueError("product-currentness runtime SHA must be a lowercase Git SHA")
        if not re.fullmatch(r"[0-9a-f]{40}", values["selected_runtime_sha"]):
            raise ValueError("product-currentness selected runtime SHA must be a lowercase Git SHA")
        if not re.fullmatch(r"[0-9a-f]{64}", values["expected_tools_schema_sha256"]):
            raise ValueError("product-currentness expected schema must be a SHA-256 digest")
        receipt = os.getenv("SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_RECEIPT", "").strip()
        key = os.getenv("SWITCHSTAND_PRODUCT_CURRENTNESS_QUALIFICATION_KEY", "").strip()
        if bool(receipt) != bool(key):
            raise ValueError(
                "product-currentness qualification receipt and key must be configured together"
            )
        receipt_path = Path(receipt) if receipt else None
        key_path = Path(key) if key else None
        if (
            receipt_path is not None
            and key_path is not None
            and (not receipt_path.is_absolute() or not key_path.is_absolute())
        ):
            raise ValueError(
                "product-currentness qualification receipt and key paths must be absolute"
            )
        return cls(
            **values,
            qualification_receipt=receipt_path,
            qualification_key=key_path,
        )


def _tools_snapshot(tools: Sequence[Tool]) -> tuple[tuple[str, ...], str]:
    payload = [
        tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in tools
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return tuple(tool.name for tool in tools), hashlib.sha256(canonical).hexdigest()


class _CompleteMCPStream:
    """Finish a normally-returning MCP GET stream at the ASGI boundary.

    sse-starlette's signal watcher cancels its body writer before the final
    chunk. Keep this integration shim until that dependency completes the
    response itself; exceptions still propagate without being disguised.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET" or scope["path"] != "/mcp":
            await self.app(scope, receive, send)
            return

        response_started = False
        response_complete = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started, response_complete
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            await send(message)

        await self.app(scope, receive, tracked_send)
        if response_started and not response_complete:
            await send({"type": "http.response.body", "body": b"", "more_body": False})


def http_middleware() -> list[Middleware]:
    return [Middleware(_CompleteMCPStream)]


def _loopback_host(raw_value: str) -> str:
    value = raw_value.strip()
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("SWITCHSTAND_MCP_BIND_HOST must remain loopback-only")
    return value


def _bind_port(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("SWITCHSTAND_MCP_BIND_PORT must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError("SWITCHSTAND_MCP_BIND_PORT must be between 1 and 65535")
    return value


@dataclass(frozen=True, slots=True)
class MCPAuthConfig:
    github_client_id: str
    github_client_secret: str
    github_user_id: str
    resource_url: str
    bind_host: str = "127.0.0.1"
    bind_port: int = 8790

    @property
    def base_url(self) -> str:
        return str(AnyHttpUrl(self.resource_url.removesuffix("/mcp").rstrip("/")))

    @property
    def issuer_url(self) -> str:
        return self.base_url

    @classmethod
    def from_environment(cls) -> MCPAuthConfig:
        values = {
            "github_client_id": os.getenv("SWITCHSTAND_MCP_GITHUB_CLIENT_ID", "").strip(),
            "github_client_secret": os.getenv("SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET", "").strip(),
            "github_user_id": os.getenv("SWITCHSTAND_MCP_GITHUB_USER_ID", "").strip(),
            "resource_url": os.getenv("SWITCHSTAND_MCP_RESOURCE_URL", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"required MCP OAuth configuration missing: {', '.join(missing)}")
        if not values["github_user_id"].isdigit():
            raise ValueError("SWITCHSTAND_MCP_GITHUB_USER_ID must be a numeric GitHub user ID")
        return cls(
            **(values | {"resource_url": normalize_resource_url(values["resource_url"])}),
            bind_host=_loopback_host(os.getenv("SWITCHSTAND_MCP_BIND_HOST", "127.0.0.1")),
            bind_port=_bind_port(os.getenv("SWITCHSTAND_MCP_BIND_PORT", "8790")),
        )


def _audit(tool: str, target: str | None, status: str) -> None:
    token = get_access_token()
    LOG.info(
        "chatgpt_mcp tool=%s issuer=%s subject=%s client_id=%s target=%s status=%s",
        tool,
        None if token is None else (token.claims or {}).get("iss"),
        None if token is None else token.subject,
        None if token is None else token.client_id,
        target,
        status,
    )


def _timing_identity() -> dict[str, str | None]:
    token = get_access_token()
    return {
        "issuer": None if token is None else (token.claims or {}).get("iss"),
        "subject": None if token is None else token.subject,
        "client_id": None if token is None else token.client_id,
    }


def runtime_identity_from_meta(meta: dict[str, Any]) -> str:
    """Read one supported host identity without conflating host namespaces."""
    present = [(key, meta[key]) for key in ("openai/session", "threadId") if key in meta]
    if len(present) != 1 or not isinstance(present[0][1], str) or not present[0][1]:
        return ""
    key, value = present[0]
    return value if key == "openai/session" else f"codex:{value}"


def _runtime_identity() -> str:
    """Read the stable host identity from the current MCP call metadata."""
    request_context = get_context().request_context
    if request_context is None:
        return ""
    meta = request_context.meta
    if meta is None:
        return ""
    return runtime_identity_from_meta(meta)


def create_app(
    service: ChatGPTService, config: MCPAuthConfig, *, client_storage: Any | None = None,
    certification_runtime: tuple[str, str] | None = None,
):
    """Build the current combined OAuth/resource application."""
    auth_options: dict[str, Any] = {}
    if client_storage is not None:
        auth_options["client_storage"] = client_storage
    auth = SwitchstandGitHubProvider(
        client_id=config.github_client_id,
        client_secret=config.github_client_secret,
        allowed_user_id=config.github_user_id,
        base_url=config.base_url,
        issuer_url=config.issuer_url,
        required_scopes=[REQUIRED_SCOPE],
        require_authorization_consent=True,
        fastmcp_access_token_expiry_seconds=FASTMCP_ACCESS_TOKEN_LIFETIME_SECONDS,
        **auth_options,
    )
    return _create_resource_app(
        service,
        issuer_url=config.issuer_url,
        resource_url=config.resource_url,
        auth=auth,
        certification_runtime=certification_runtime,
    )


def create_delegated_app(
    service: ChatGPTService,
    verifier: IntrospectionTokenVerifier,
    *,
    certification_runtime: tuple[str, str] | None = None,
):
    """Build an inert resource edge with no GitHub or OAuth signing state."""
    return _create_resource_app(
        service,
        issuer_url=verifier.contract.issuer_url,
        resource_url=verifier.contract.resource_url,
        auth=delegated_auth(verifier),
        certification_runtime=certification_runtime,
    )


def _create_resource_app(
    service: ChatGPTService,
    *,
    issuer_url: str,
    resource_url: str,
    auth: Any,
    certification_runtime: tuple[str, str] | None,
):
    service = ChatGPTService(
        RequestPrincipal(issuer_url, resource_url, REQUIRED_SCOPE),
        service.state,
        service.grants,
        service.providers,
        service.messages,
        service.required_results,
        ordinary_workspace_admission=True,
        canonical_work=service.canonical_work,
        canonical_events=service.canonical_events,
        canonical_work_active=service.canonical_work_active,
        outcome_state_enabled=service.outcome_state_enabled,
        priority_claims=service.priority_claims,
        priority_claims_enabled=service.priority_claims_enabled,
        priority_context=service.priority_context,
        priority_context_enabled=service.priority_context_enabled,
        implementation_requests=service.implementation_requests,
        reviews=service.reviews,
        product_currentness=service.product_currentness,
        product_currentness_enabled=service.product_currentness_enabled,
        activation_continuity=service.activation_continuity,
        activation_technical=service.activation_technical,
        activation_runtime=service.activation_runtime,
        activation_proof=service.activation_proof,
    )
    currentness_config = _ProductCurrentnessConfig.from_environment()
    if currentness_config is not None:
        currentness_state = service.state
        if not isinstance(currentness_state, PostgresState):
            raise ValueError("product currentness requires the PostgreSQL resource edge")

        async def product_currentness(principal: PrincipalContext) -> ProductCurrentness:
            async def read_principal() -> PrincipalContext:
                return principal

            async def read_snapshot() -> StatefulServerSnapshot:
                names, schema_digest = _tools_snapshot(
                    await server.list_tools(run_middleware=False)
                )
                return StatefulServerSnapshot(
                    runtime_sha=currentness_config.runtime_sha,
                    selected_runtime_sha=currentness_config.selected_runtime_sha,
                    run_id=currentness_config.run_id,
                    principal_key=principal.key,
                    outcome_actions_enabled=service.outcome_state_enabled,
                    tool_names=names,
                    tools_schema_sha256=schema_digest,
                )

            reader = LiveStatefulEvidenceReader(
                currentness_state.engine,
                read_principal,
                read_snapshot,
                expected_migration_revision=STATEFUL_MIGRATION_REVISION,
                expected_tools_schema_sha256=currentness_config.expected_tools_schema_sha256,
                qualification_receipt=currentness_config.qualification_receipt,
                qualification_key=currentness_config.qualification_key,
            )
            return await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)

        service.product_currentness = product_currentness
        service.product_currentness_enabled = True
    server = FastMCP("Switchstand ChatGPT", version="1", auth=auth)
    server.add_middleware(CallTimingMiddleware(_timing_identity))
    for name, tool in build_ordinary_tools(
        service, _audit,
        agent_identity=_runtime_identity,
        correlate_work=annotate_target,
    ):
        server.tool(tool, annotations=ordinary_tool_annotations(name))
    app = server.http_app(
        path="/mcp", json_response=True, stateless_http=False,
        middleware=http_middleware(),
    )
    if certification_runtime is not None:
        runtime_sha, run_id = certification_runtime

        async def certification_readback(_request: Request) -> JSONResponse:
            return JSONResponse({"runtime_sha": runtime_sha, "run_id": run_id})

        app.add_route(CERTIFICATION_RUNTIME_PATH, certification_readback, methods=["GET"])
    return app


@asynccontextmanager
async def resource_service(
    activation_contracts: Mapping[UUID, ActivationContract] | None = None,
) -> AsyncGenerator[tuple[ChatGPTService, tuple[str, str] | None]]:
    """Own the resource edge's PostgreSQL dependencies for either launch mode."""
    engine = create_async_engine(os.environ["DATABASE_URL"])
    register_sqlalchemy_timing(engine)
    try:
        async def unresolved_principal():
            return None

        marker = os.getenv("SWITCHSTAND_CERTIFICATION_FIXTURE_MARKER", "").strip()
        grants = GrantState(engine)
        canonical_repository = CanonicalWorkRepository(engine)
        canonical_relations = CanonicalRelationsRepository(engine)
        canonical_work = CanonicalWorkRuntime(
            canonical_repository, canonical_relations
        )
        canonical_events = CanonicalEventReader(
            canonical_repository, WorkEventRepository(engine)
        )
        messages = MessageState(engine, grants)
        priority_claims = None
        priority_context = None
        priority_claims_enabled = os.getenv("SWITCHSTAND_PRIORITY_CLAIMS") == "1"
        if priority_claims_enabled:
            priority_claims = PriorityClaimService(
                PriorityClaimRepository(engine), canonical_repository
            )
            priority_context = PriorityContextProjection(
                works=canonical_repository,
                relations=canonical_relations,
                claims=priority_claims,
            )
        implementation_requests = None
        if os.getenv("SWITCHSTAND_IMPLEMENTATION_REQUESTS") == "1":
            reviews = ReviewOccurrenceState(
                canonical_repository, AgentMailboxState(engine),
                ReviewPolicy(version="review-policy-v1", reviewer_by_kind={}),
            )
            implementation_requests = ImplementationRequestState(
                engine, canonical_repository, reviews
            )
        service = ChatGPTService(unresolved_principal, PostgresState(engine), grants, {},
            messages, RequiredResultPersistence(LifecycleRepository(engine)),
            canonical_work=canonical_work, canonical_events=canonical_events,
            canonical_work_active=True,
            outcome_state_enabled=os.getenv("SWITCHSTAND_OUTCOME_STATE_ACTIONS") == "1",
            priority_claims=priority_claims,
            priority_claims_enabled=priority_claims_enabled,
            priority_context=priority_context,
            priority_context_enabled=priority_claims_enabled,
            implementation_requests=implementation_requests,
            activation_continuity=(
                None
                if activation_contracts is None
                else ActivationContinuity(engine, activation_contracts)
            ))
        runtime = None
        if marker:
            runtime = (
                os.environ["SWITCHSTAND_CERTIFICATION_RUNTIME_SHA"],
                os.environ["SWITCHSTAND_CERTIFICATION_RUN_ID"],
            )
        yield service, runtime
    finally:
        await engine.dispose()


@asynccontextmanager
async def configured_resource_service(
) -> AsyncGenerator[tuple[ChatGPTService, tuple[str, str] | None]]:
    """Own resource dependencies using the explicit default-off host configuration."""

    async with resource_service(load_activation_contracts_from_environment()) as owned:
        yield owned


async def serve() -> None:
    config = MCPAuthConfig.from_environment()
    async with configured_resource_service() as (service, runtime):
        app = create_app(service, config, certification_runtime=runtime)
        await app.state.fastmcp_server.run_http_async(
            host=config.bind_host, port=config.bind_port, path="/mcp",
            json_response=True, stateless_http=False, show_banner=False,
            uvicorn_config={"timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS},
            middleware=http_middleware(),
        )


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
