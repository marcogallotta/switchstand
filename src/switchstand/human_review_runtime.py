"""Explicit loopback runtime for the default-off Human Review shell."""

from __future__ import annotations

import asyncio
import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine

from .human_review_shell import create_persistent_human_review_shell


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required Human Review configuration missing: {name}")
    return value


@dataclass(frozen=True, slots=True)
class HumanReviewRuntimeConfig:
    database_url: str = field(repr=False)
    expected_origin: str
    username: str
    password_hash: str = field(repr=False)
    bind_host: str
    bind_port: int

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> HumanReviewRuntimeConfig:
        values = os.environ if environment is None else environment
        host = _required(values, "SWITCHSTAND_HUMAN_REVIEW_BIND_HOST")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ValueError("SWITCHSTAND_HUMAN_REVIEW_BIND_HOST must be a loopback IP") from error
        if not address.is_loopback:
            raise ValueError("SWITCHSTAND_HUMAN_REVIEW_BIND_HOST must be a loopback IP")
        raw_port = _required(values, "SWITCHSTAND_HUMAN_REVIEW_BIND_PORT")
        try:
            port = int(raw_port)
        except ValueError as error:
            raise ValueError("SWITCHSTAND_HUMAN_REVIEW_BIND_PORT must be an integer") from error
        if not 1 <= port <= 65535:
            raise ValueError("SWITCHSTAND_HUMAN_REVIEW_BIND_PORT must be between 1 and 65535")
        return cls(
            database_url=_required(values, "DATABASE_URL"),
            expected_origin=_required(values, "SWITCHSTAND_HUMAN_REVIEW_ORIGIN"),
            username=_required(values, "SWITCHSTAND_HUMAN_REVIEW_USERNAME"),
            password_hash=_required(values, "SWITCHSTAND_HUMAN_REVIEW_PASSWORD_HASH"),
            bind_host=host,
            bind_port=port,
        )


async def serve(config: HumanReviewRuntimeConfig | None = None) -> None:
    runtime = config or HumanReviewRuntimeConfig.from_environment()
    engine = create_async_engine(runtime.database_url)
    try:
        app = create_persistent_human_review_shell(
            engine,
            expected_origin=runtime.expected_origin,
            username=runtime.username,
            password_hash=runtime.password_hash,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host=runtime.bind_host, port=runtime.bind_port, log_level="info")
        )
        await server.serve()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
