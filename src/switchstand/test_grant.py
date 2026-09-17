"""Manage one disposable ChatGPT grant on the test database."""

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from .core import provision_launch
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant
from .provider import AsanaProvider
from .state import PostgresState
from .task_ref import asana_task_id

MAX_TTL_SECONDS = 3600
Operation = Literal["work_get", "work_append", "work_create"]


def test_database_url(environment: Mapping[str, str] = os.environ) -> str:
    url = environment.get("TEST_DATABASE_URL", "")
    if not url:
        raise ValueError("TEST_DATABASE_URL must name switchstand_test")
    parsed = make_url(url)
    if (parsed.drivername != "postgresql+psycopg"
            or parsed.database != "switchstand_test" or "dbname" in parsed.query):
        raise ValueError("TEST_DATABASE_URL must name switchstand_test without a dbname override")
    return url


def redacted(principal: PrincipalContext, grant: WorkGrant | None) -> dict[str, object]:
    result: dict[str, object] = {
        "principal": {"identity": "[redacted]", "assurance": principal.assurance},
        "grant": None,
    }
    if grant:
        result["grant"] = {
            "id": str(grant.id), "version": grant.version, "state": grant.state,
            "current": grant.current(), "expires_at": grant.expires_at.isoformat(),
            "active_work_id": str(grant.authority.active_work_id),
            "qualification": "[redacted]",
        }
    return result


async def execute(arguments: argparse.Namespace) -> dict[str, object]:
    engine = create_async_engine(test_database_url())
    principal = PrincipalContext(
        issuer=arguments.issuer, subject=arguments.subject,
        client_id=arguments.client_id, assurance=getattr(arguments, "assurance", "test"),
    )
    grants = GrantState(engine)
    try:
        current = await grants.current(principal.key)
        if arguments.command == "inspect":
            return redacted(principal, current)
        if current is None:
            if arguments.command == "revoke":
                raise ValueError("no grant to revoke")
            actual_version = 0
        else:
            actual_version = current.version
        if actual_version != arguments.expected_version:
            raise ValueError("stale grant issuance")
        if arguments.command == "revoke":
            assert current is not None
            replacement = current.model_copy(update={
                "id": uuid4(), "version": actual_version + 1, "state": "revoked",
            })
        else:
            if not 1 <= arguments.ttl_seconds <= MAX_TTL_SECONDS:
                raise ValueError(f"TTL must be between 1 and {MAX_TTL_SECONDS} seconds")
            if not arguments.qualification.startswith("test:"):
                raise ValueError("qualification must start with test:")
            async with httpx.AsyncClient(
                base_url="https://app.asana.com/api/1.0", trust_env=False,
                headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
            ) as client:
                authority = await provision_launch(
                    PostgresState(engine), "asana",
                    AsanaProvider(client, arguments.test_project, test_only=True), arguments.task, (),
                )
            operations: set[Operation] = {"work_get", "work_create"}
            append_qualification = None
            if principal.assurance == "test":
                operations.add("work_append")
                append_qualification = arguments.qualification
            replacement = WorkGrant(
                id=uuid4(), version=actual_version + 1, principal=principal,
                authority=authority, operations=frozenset(operations),
                issuer="switchstand-test-grant",
                provenance=f"explicit disposable task {arguments.task}",
                expires_at=datetime.now(UTC) + timedelta(seconds=arguments.ttl_seconds),
                append_qualification=append_qualification,
                create_qualification=arguments.qualification,
            )
        await grants.issue(replacement, None if actual_version == 0 else actual_version)
        return redacted(principal, replacement)
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for field in ("issuer", "subject", "client-id"):
        result.add_argument(f"--{field}", required=True)
    result.add_argument("--assurance", choices=("test", "authenticated"), default="test")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    setting = commands.add_parser("set")
    setting.add_argument("--task", required=True, type=asana_task_id)
    setting.add_argument("--test-project", required=True, type=asana_task_id)
    setting.add_argument("--expected-version", required=True, type=int)
    setting.add_argument("--ttl-seconds", required=True, type=int)
    setting.add_argument("--qualification", required=True)
    revoking = commands.add_parser("revoke")
    revoking.add_argument("--expected-version", required=True, type=int)
    return result


def main() -> None:
    cli = parser()
    try:
        print(json.dumps(asyncio.run(execute(cli.parse_args())), sort_keys=True))
    except (KeyError, ValueError, PermissionError) as error:
        cli.exit(1, f"test grant command failed: {error}\n")


if __name__ == "__main__":
    main()
