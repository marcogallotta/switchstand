import os
import subprocess
import sys


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
