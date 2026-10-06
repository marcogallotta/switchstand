import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError


def _disposable_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL migration test")
    if make_url(url).database != "switchstand_test":
        pytest.fail("migration test requires the disposable switchstand_test database")
    return url


def _empty_database(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, activation_obligation_revisions, "
                "human_review_consequences, "
                "task_run_results, task_run_executions, task_run_requests, "
                "failure_resolutions, failure_records, work_migration_receipts, "
                "outcome_state_revisions, human_trajectory_revisions, "
                "agent_mailbox_transfer_requests, agent_mailboxes, work_event_handles, "
                "lifecycle_obligations, message_projection, message_deliveries, messages, "
                "effect_intents, work_grants, work_events, project_memberships, projects, "
                "work_parents, work_dependencies, legacy_work_aliases, canonical_work, "
                "work_handles CASCADE"
            )
        )


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def _insert_package(engine: Engine):
    package_work_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO canonical_work "
                "(work_id, title, normalized_title, completed, notes, row_version) "
                "VALUES (:work_id, 'Package', 'package', false, '', 1)"
            ),
            {"work_id": package_work_id},
        )
    return package_work_id


def test_human_review_migration_enforces_shape_and_preserves_decisions(
    database_prerequisite: None,
):
    url = _disposable_url()
    engine = create_engine(url)
    _empty_database(engine)
    config = _config(url)
    command.upgrade(config, "0020_task_run_results")
    package_work_id = _insert_package(engine)
    command.upgrade(config, "0021_human_reviews")

    database = inspect(engine)
    assert {column["name"] for column in database.get_columns("human_review_consequences")} == {
        "consequence_id",
        "package_work_id",
        "package_revision",
        "consequence_digest",
        "consequence",
        "decision",
        "state",
        "created_at",
        "decided_at",
    }
    assert {
        constraint["name"]
        for constraint in database.get_check_constraints("human_review_consequences")
    } >= {
        "ck_human_review_package_revision",
        "ck_human_review_digest",
        "ck_human_review_decision",
        "ck_human_review_state",
        "ck_human_review_terminal_shape",
    }
    assert {
        (index["name"], tuple(index["column_names"]))
        for index in database.get_indexes("human_review_consequences")
    } >= {
        ("ix_human_review_consequences_package_work_id", ("package_work_id",)),
    }

    values = {
        "consequence_id": uuid4(),
        "package_work_id": package_work_id,
        "package_revision": "pg_1",
        "consequence_digest": "a" * 64,
        "consequence": "{}",
    }
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO human_review_consequences "
                "(consequence_id, package_work_id, package_revision, consequence_digest, "
                "consequence, decision, state, decided_at) VALUES "
                "(:consequence_id, :package_work_id, :package_revision, "
                ":consequence_digest, CAST(:consequence AS jsonb), 'HOLD', "
                "'READY_FOR_IMPLEMENTATION', now())"
            ),
            values,
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO human_review_consequences "
                "(consequence_id, package_work_id, package_revision, consequence_digest, "
                "consequence, state) VALUES "
                "(:consequence_id, :package_work_id, :package_revision, "
                ":consequence_digest, CAST(:consequence AS jsonb), 'PENDING')"
            ),
            values,
        )

    with pytest.raises(RuntimeError, match="preserve durable Human Review decisions"):
        command.downgrade(config, "0020_task_run_results")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0021_human_reviews"
        )
        assert connection.scalar(text("SELECT count(*) FROM human_review_consequences")) == 1
    engine.dispose()


def test_empty_human_review_downgrade_and_reupgrade_recovers_schema(
    database_prerequisite: None,
):
    url = _disposable_url()
    engine = create_engine(url)
    _empty_database(engine)
    config = _config(url)
    command.upgrade(config, "0021_human_reviews")
    command.downgrade(config, "0020_task_run_results")
    assert "human_review_consequences" not in inspect(engine).get_table_names()
    command.upgrade(config, "0021_human_reviews")
    assert "human_review_consequences" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0021_human_reviews"
        )
    engine.dispose()
