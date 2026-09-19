"""Causal isolation and process cleanup checks, with no shared database dependency."""

import os
import signal
import sys
import time

import pytest
from disposable_postgres import clean_environment, exited, owned_process, private_directory


def test_private_directory_refuses_foreign_shape_without_changing_it(tmp_path):
    target = tmp_path / "public"
    target.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="owner-only"):
        private_directory(target)
    assert target.stat().st_mode & 0o777 == 0o755
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="owner-only"):
        private_directory(link)
    assert link.is_symlink()


def test_environment_excludes_provider_and_database_inputs(monkeypatch):
    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "PGPASSWORD", "PGSERVICE",
                 "ASANA_TOKEN", "SWITCHSTAND_TEST_PROJECT_GID", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "production-must-not-be-used")
    assert "production-must-not-be-used" not in clean_environment().values()


def test_failure_kills_owned_term_resistant_child_and_preserves_foreign_process(tmp_path):
    env = clean_environment()
    with owned_process([sys.executable, "-c", "import time; time.sleep(60)"], env,
                       tmp_path / "foreign.log") as foreign:
        ready = tmp_path / "ready"
        command = [sys.executable, "-c",
                   ("import signal,time,pathlib,sys; "
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                   "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"), str(ready)]
        with (pytest.raises(ValueError, match="injected"),
              owned_process(command, env, tmp_path / "owned.log") as owned):
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert exited(owned) is None and time.monotonic() < deadline
                time.sleep(0.02)
            raise ValueError("injected qualification failure")
        assert owned.returncode == -signal.SIGKILL
        assert exited(foreign) is None
        os.kill(foreign.pid, 0)
        assert (tmp_path / "owned.log").exists()
