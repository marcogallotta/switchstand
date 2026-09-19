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
                 "ASANA_TOKEN", "SWITCHSTAND_TEST_PROJECT_GID", "HTTPS_PROXY",
                 "SWITCHSTAND_CHECK_VENV", "SWITCHSTAND_CHECK_MANIFEST"):
        monkeypatch.setenv(name, "production-must-not-be-used")
    monkeypatch.setenv("SWITCHSTAND_CHECK_UV", "/control/pinned-uv")
    assert "production-must-not-be-used" not in clean_environment().values()
    assert clean_environment()["SWITCHSTAND_CHECK_UV"] == "/control/pinned-uv"


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


def test_supervisor_contains_timeout_group_and_orphaned_edge(tmp_path):
    from pathlib import Path

    env = clean_environment()
    env["PYTHONPATH"] = str(Path(__file__).parent.resolve()) + os.pathsep + env["PYTHONPATH"]
    child_pid = tmp_path / "child.pid"
    supervisor_code = '''
import signal,sys,time
from pathlib import Path
from disposable_postgres import owned_process,reap_descendants,clean_environment
root = Path(sys.argv[1])
def interrupted(signum, frame):
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, interrupted)
with reap_descendants():
    with owned_process(
        ["sh", "-c", 'timeout 60 "$@" & wait', "fixture", sys.executable, "-c",
         'import os,sys,time; open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(60)',
         str(root / "child.pid")], clean_environment(), root / "nested.log"):
        time.sleep(60)
'''
    with (owned_process([sys.executable, "-c", "import time; time.sleep(60)"], env,
                        tmp_path / "sentinel.log") as sentinel,
          owned_process([sys.executable, "-c", supervisor_code, str(tmp_path)], env,
                        tmp_path / "supervisor.log") as supervisor):
        deadline = time.monotonic() + 10
        while not child_pid.exists():
            assert exited(supervisor) is None and time.monotonic() < deadline
            time.sleep(0.02)
        pid = int(child_pid.read_text())
        os.kill(supervisor.pid, signal.SIGTERM)
        while exited(supervisor) is None:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert exited(supervisor).si_status == 128 + signal.SIGTERM
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert exited(sentinel) is None
