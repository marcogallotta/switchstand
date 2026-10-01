import os

import pytest
from sqlalchemy.exc import ArgumentError

from switchstand.database import validate_test_database_url
from switchstand.work_index import activate, prepare_manifest


async def activate_stage1(engine, items):
    digest = await prepare_manifest(engine, items)
    return await activate(
        engine, items, expected_manifest_digest=digest
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
