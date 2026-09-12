import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def test_empty_database_migrates_to_grants_and_effects(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL migration test")
    if make_url(url).database != "switchstand_test":
        pytest.fail("migration test requires the disposable switchstand_test database")
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, effect_intents, work_grants, work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version", "work_handles", "work_grants", "effect_intents",
    }
    assert {column["name"] for column in inspect(engine).get_columns("work_handles")} == {"id", "provider", "provider_work_id"}
