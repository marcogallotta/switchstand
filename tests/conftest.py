import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.getenv("SWITCHSTAND_REQUIRE_TEST_DATABASE") != "1" or os.getenv(
        "TEST_DATABASE_URL"
    ):
        return
    database_tests = {"test_migration.py", "test_state.py"}
    selected = [item for item in items if Path(str(item.path)).name in database_tests]
    if selected:
        raise pytest.UsageError("selected database tests require TEST_DATABASE_URL")
