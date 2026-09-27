"""Neutral database-boundary validation shared by test-only tools."""

from sqlalchemy.engine import make_url


def validate_test_database_url(url: str) -> str:
    parsed = make_url(url)
    if (parsed.drivername != "postgresql+psycopg"
            or parsed.database != "switchstand_test" or "dbname" in parsed.query):
        raise ValueError("database URL must name switchstand_test without a dbname override")
    return url
