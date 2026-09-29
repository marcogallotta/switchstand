import os
import subprocess
import sys

import pytest


def test_focused_database_selection_fails_closed_without_url() -> None:
    environment = os.environ.copy()
    environment.pop("TEST_DATABASE_URL", None)
    environment["SWITCHSTAND_REQUIRE_TEST_DATABASE"] = "1"
    command = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "tests/test_workspace_admission.py", "-k", "stable_without_work_grant",
    ]
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1 and "1 error" in result.stdout
    assert "selected database test requires TEST_DATABASE_URL" in result.stdout


@pytest.mark.parametrize("url", [
    "",
    "postgresql+psycopg://fixture:must-not-appear@127.0.0.1/production",
])
def test_supplied_invalid_database_fails_before_collection(url: str) -> None:
    environment = os.environ.copy()
    secret = "must-not-appear"
    environment["TEST_DATABASE_URL"] = url
    command = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "tests/test_principal.py",
    ]

    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 4
    assert "must name an isolated PostgreSQL switchstand_test database" in output
    assert secret not in output
    assert "collected" not in output
