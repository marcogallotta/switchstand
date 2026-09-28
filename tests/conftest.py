import os

import pytest


@pytest.fixture
def database_prerequisite() -> None:
    if (
        os.getenv("SWITCHSTAND_REQUIRE_TEST_DATABASE") == "1"
        and not os.getenv("TEST_DATABASE_URL")
    ):
        pytest.fail("selected database test requires TEST_DATABASE_URL", pytrace=False)
