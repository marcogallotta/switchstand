import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from switchstand.provision import require_current_schema

TABLES = (
    "lifecycle_obligations",
    "message_projection",
    "message_deliveries",
    "messages",
    "effect_intents",
    "work_grants",
    "work_handles",
    "alembic_version",
)


def disposable_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL migration test")
    if make_url(url).database != "switchstand_test":
        pytest.fail("migration test requires the disposable switchstand_test database")
    return url


def reset_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {', '.join(TABLES)} CASCADE"))


def config_for(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_stale_schema_check_does_not_upgrade(monkeypatch):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    reset_schema(engine)
    with pytest.raises(RuntimeError, match="shared CONTROL schema is stale"):
        require_current_schema()
    assert inspect(engine).get_table_names() == []


def test_empty_database_migrates_to_lifecycle_head(monkeypatch):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    reset_schema(engine)
    command.upgrade(config_for(url), "head")
    assert set(TABLES) == set(inspect(engine).get_table_names())


def test_lifecycle_downgrade_refuses_to_discard_obligation():
    url = disposable_url()
    engine = create_engine(url)
    reset_schema(engine)
    config = config_for(url)
    command.upgrade(config, "head")
    work_id, obligation_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO work_handles (id, provider, provider_work_id) "
                "VALUES (:id, 'migration-test', :provider_id)"
            ),
            {"id": work_id, "provider_id": str(work_id)},
        )
        connection.execute(
            text(
                "INSERT INTO lifecycle_obligations "
                "(obligation_id, profile_type, profile_version, work_id_ref, "
                "currentness_token, state, row_version) VALUES "
                "(:id, 'REQUIRED_RESULT_PERSISTENCE', 1, :work_id, "
                "'revision:1', 'PENDING_RESULT', 1)"
            ),
            {"id": obligation_id, "work_id": work_id},
        )

    with pytest.raises(RuntimeError, match="preserve durable lifecycle obligation truth"):
        command.downgrade(config, "0003_messages_and_deliveries")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM lifecycle_obligations")) == 1


def test_empty_lifecycle_downgrade_and_reupgrade_recovers_schema():
    url = disposable_url()
    engine = create_engine(url)
    reset_schema(engine)
    config = config_for(url)
    command.upgrade(config, "head")
    command.downgrade(config, "0003_messages_and_deliveries")
    assert "lifecycle_obligations" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    assert "lifecycle_obligations" in inspect(engine).get_table_names()
