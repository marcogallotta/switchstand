import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_empty_database_migrates_to_single_table():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required for the PostgreSQL migration test")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version, work_handles CASCADE"))
    command.upgrade(Config("alembic.ini"), "head")
    assert set(inspect(engine).get_table_names()) == {"alembic_version", "work_handles"}
