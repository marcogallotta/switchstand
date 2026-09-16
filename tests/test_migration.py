import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from switchstand.provision import require_current_schema


def disposable_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL migration test")
    if make_url(url).database != "switchstand_test":
        pytest.fail("migration test requires the disposable switchstand_test database")
    return url


def test_stale_schema_check_does_not_upgrade(monkeypatch):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, message_projection, message_deliveries, "
            "messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    with pytest.raises(RuntimeError, match="shared CONTROL schema is stale"):
        require_current_schema()
    assert inspect(engine).get_table_names() == []


def test_empty_database_migrates_to_messages(monkeypatch):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, message_projection, message_deliveries, "
            "messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version", "work_handles", "work_grants", "effect_intents", "messages",
        "message_deliveries", "message_projection",
    }
    assert {column["name"] for column in inspect(engine).get_columns("work_handles")} == {"id", "provider", "provider_work_id"}


def test_message_downgrade_refuses_to_destroy_durable_truth(monkeypatch):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, message_projection, message_deliveries, "
            "messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO messages "
            "(sender_work_id, message_id, route_ref, kind, payload, digest) "
            "VALUES (:sender, :message, 'review', 'request', '{}'::jsonb, 'digest')"
        ), {"sender": str(uuid4()), "message": str(uuid4())})
    with pytest.raises(RuntimeError, match="preserve durable message truth"):
        command.downgrade(config, "0002_grants_and_effects")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM messages")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0003_messages_and_deliveries"
