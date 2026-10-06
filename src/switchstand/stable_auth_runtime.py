"""Default-off launch modes for the single-host stable-auth topology."""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn

from .chatgpt_edge import (
    GRACEFUL_SHUTDOWN_SECONDS,
    configured_resource_service,
    create_delegated_app,
    http_middleware,
)
from .stable_auth import (
    IntrospectionContract,
    IntrospectionTokenVerifier,
    StableAuthConfig,
    create_auth_service,
)
from .stable_auth_host import AUTH_PORT, EDGE_PORT, read_internal_secret


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"required split-auth configuration missing: {name}")
    return value


def _loopback_port(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


def _contract() -> IntrospectionContract:
    return IntrospectionContract.for_resource(
        _required("SWITCHSTAND_MCP_RESOURCE_URL"),
        _required("SWITCHSTAND_MCP_GITHUB_USER_ID"),
    )


def _secret() -> str:
    return read_internal_secret(Path(_required("SWITCHSTAND_INTERNAL_CREDENTIAL_FILE")))


@dataclass(frozen=True, slots=True)
class AuthRuntimeConfig:
    auth: StableAuthConfig
    bind_port: int

    @classmethod
    def from_environment(cls) -> AuthRuntimeConfig:
        return cls(
            StableAuthConfig(
                _required("SWITCHSTAND_MCP_GITHUB_CLIENT_ID"),
                _required("SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET"),
                _secret(),
                _contract(),
            ),
            _loopback_port("SWITCHSTAND_AUTH_BIND_PORT", AUTH_PORT),
        )


@dataclass(frozen=True, slots=True)
class EdgeRuntimeConfig:
    contract: IntrospectionContract
    internal_secret: str = field(repr=False)
    auth_url: str
    bind_port: int

    @classmethod
    def from_environment(cls) -> EdgeRuntimeConfig:
        forbidden = [
            name
            for name in (
                "SWITCHSTAND_MCP_GITHUB_CLIENT_ID",
                "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET",
                "FASTMCP_HOME",
            )
            if os.getenv(name)
        ]
        if forbidden:
            raise ValueError(
                "delegated edge must not receive stable-auth state: " + ", ".join(forbidden)
            )
        raw_url = _required("SWITCHSTAND_AUTH_INTERNAL_URL")
        parsed = urlparse(raw_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.path not in ("", "/")
            or parsed.params
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise ValueError("SWITCHSTAND_AUTH_INTERNAL_URL must be an exact loopback HTTP origin")
        return cls(
            _contract(),
            _secret(),
            raw_url.rstrip("/"),
            _loopback_port("SWITCHSTAND_MCP_BIND_PORT", EDGE_PORT),
        )


async def serve_auth() -> None:
    config = AuthRuntimeConfig.from_environment()
    app = create_auth_service(config.auth)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=config.bind_port,
            log_level="info",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    )
    await server.serve()


async def serve_edge() -> None:
    config = EdgeRuntimeConfig.from_environment()
    async with httpx.AsyncClient(base_url=config.auth_url, trust_env=False) as auth_client:
        verifier = IntrospectionTokenVerifier(
            auth_client,
            internal_secret=config.internal_secret,
            contract=config.contract,
        )
        async with configured_resource_service() as (service, runtime):
            app = create_delegated_app(service, verifier, certification_runtime=runtime)
            await app.state.fastmcp_server.run_http_async(
                host="127.0.0.1",
                port=config.bind_port,
                path="/mcp",
                json_response=True,
                stateless_http=False,
                show_banner=False,
                uvicorn_config={"timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS},
                middleware=http_middleware(),
            )


def auth_main() -> None:
    asyncio.run(serve_auth())


def edge_main() -> None:
    asyncio.run(serve_edge())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("auth", "edge"))
    mode = parser.parse_args().mode
    asyncio.run(serve_auth() if mode == "auth" else serve_edge())


if __name__ == "__main__":
    main()
