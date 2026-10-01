import os
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import ArgumentError

from switchstand.core import Handle
from switchstand.database import validate_test_database_url
from switchstand.state import work_handles
from switchstand.work_index import activate, manifest_digest


async def activate_stage1(engine, items):
    async with engine.begin() as connection:
        for item in items:
            exists = await connection.scalar(select(work_handles.c.id).where(
                (work_handles.c.provider == "asana")
                & (work_handles.c.provider_work_id == item.provider_work_id)
            ))
            if exists is None:
                await connection.execute(insert(work_handles).values(
                    id=uuid5(NAMESPACE_URL, f"asana:{item.provider_work_id}"),
                    provider="asana", provider_work_id=item.provider_work_id,
                ))
        rows = (await connection.execute(select(
            work_handles.c.id, work_handles.c.provider_work_id,
        ).where(work_handles.c.provider == "asana"))).all()
    handles = {row[1]: Handle(row[0], "asana", row[1]) for row in rows}
    return await activate(
        engine, items, expected_manifest_digest=manifest_digest(items, handles)
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    url = os.getenv("TEST_DATABASE_URL")
    if url is None:
        return
    try:
        validate_test_database_url(url)
    except (ArgumentError, ValueError):
        pytest.exit(
            "TEST_DATABASE_URL must name an isolated PostgreSQL switchstand_test database "
            "without a dbname override",
            returncode=4,
        )


@pytest.fixture
def database_prerequisite() -> None:
    if (
        os.getenv("SWITCHSTAND_REQUIRE_TEST_DATABASE") == "1"
        and not os.getenv("TEST_DATABASE_URL")
    ):
        pytest.fail("selected database test requires TEST_DATABASE_URL", pytrace=False)
