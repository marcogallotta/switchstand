import argparse
import asyncio
import os

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from .contracts import LaunchVerification, PreparedLaunch, WorkFingerprint, WorkGetRequest
from .core import Controller, ProviderError, provision_launch
from .provider import AsanaProvider
from .state import PostgresState
from .task_ref import asana_task_id


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bind canonical Asana tasks to stable, opaque Switchstand WorkIds."
    )
    result.add_argument("--active", required=True, help="active Asana task URL or ID")
    result.add_argument(
        "--reference", action="append", default=[], help="read-only Asana task URL or ID"
    )
    result.add_argument("--json", action="store_true", dest="json_output", help=argparse.SUPPRESS)
    return result


async def run(active: str, references: tuple[str, ...]) -> PreparedLaunch:
    database_url = os.environ["DATABASE_URL"]
    token = os.environ["ASANA_TOKEN"]
    engine = create_async_engine(database_url)
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0",
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        state = PostgresState(engine)
        provider = AsanaProvider(client)
        authority = await provision_launch(
            state,
            "asana",
            provider,
            asana_task_id(active),
            tuple(asana_task_id(value) for value in references),
        )
        controller = Controller(authority, state, {"asana": provider})
        fingerprints: list[WorkFingerprint] = []
        for work_id in (authority.active_work_id, *authority.reference_work_ids):
            result = await controller.get(WorkGetRequest(api_version="1", work_id=work_id))
            if result.status != "ok" or result.item is None:
                raise ProviderError("provisioning readback failed")
            fingerprints.append(WorkFingerprint(
                id=work_id, title=result.item.title, revision=result.item.revision
            ))
        return PreparedLaunch(
            authority=authority,
            verification=LaunchVerification(
                active=fingerprints[0], references=tuple(fingerprints[1:])
            ),
        )
    finally:
        await client.aclose()
        await engine.dispose()


def upgrade_database() -> None:
    command.upgrade(Config("alembic.ini"), "head")


def main() -> None:
    arguments = parser().parse_args()
    try:
        upgrade_database()
        prepared = asyncio.run(run(arguments.active, tuple(arguments.reference)))
        if arguments.json_output:
            print(prepared.model_dump_json())
        else:
            print(f"ACTIVE_WORK_ID={prepared.authority.active_work_id}")
            print("REFERENCE_WORK_IDS=" + ",".join(
                map(str, prepared.authority.reference_work_ids)
            ))
    except (KeyError, ValueError, PermissionError, ProviderError) as error:
        parser().exit(1, f"provisioning failed: {error}\n")


if __name__ == "__main__":
    main()
