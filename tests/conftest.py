import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import ArgumentError

from switchstand.canonical_relations import (
    project_memberships,
    projects,
    work_dependencies,
    work_parents,
)
from switchstand.canonical_work import canonical_metadata, canonical_work, legacy_work_aliases
from switchstand.database import validate_test_database_url
from switchstand.human_reviews import human_review_consequences
from switchstand.priority_claims import priority_claims
from switchstand.task_runs import task_run_requests
from switchstand.work_events import work_events

CANONICAL_TABLES = (
    task_run_requests,
    priority_claims,
    human_review_consequences,
    canonical_work, legacy_work_aliases, work_dependencies, work_parents,
    projects, project_memberships, work_events,
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
    url = os.getenv("TEST_DATABASE_URL")
    if os.getenv("SWITCHSTAND_REQUIRE_TEST_DATABASE") == "1" and not url:
        pytest.fail("selected database test requires TEST_DATABASE_URL", pytrace=False)
    if url:
        engine = create_engine(url)
        try:
            # Tests which rebuild an older schema with Alembic must not inherit
            # tables owned by the current head from a preceding metadata-based
            # fixture.  Keep the migration strict; isolate the disposable test
            # database at the fixture boundary instead.
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "DROP TABLE IF EXISTS task_run_results, task_run_executions, "
                    "task_run_requests, failure_resolutions, failure_records, "
                    "work_migration_receipts CASCADE"
                )
            canonical_metadata.drop_all(engine, tables=CANONICAL_TABLES, checkfirst=True)
        finally:
            engine.dispose()
