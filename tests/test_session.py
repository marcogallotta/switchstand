"""Real Linux process and terminal boundaries for managed session ownership."""

import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from switchstand.session import supervise


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("process boundary did not settle")


def alive(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
        return value[value.rindex(")") + 2:].split()[0] not in {"Z", "X"}
    except FileNotFoundError:
        return False


def launch(parent, command):
    parent.mkdir()
    return subprocess.Popen([
        sys.executable, "-c",
        ("import os,sys; from pathlib import Path; "
        "from switchstand.session import supervise; "
        "sys.exit(supervise(sys.argv[2:], dict(os.environ), Path(sys.argv[1])))"),
        str(parent), *command,
    ], start_new_session=True)


@pytest.mark.parametrize("cancel", [None, signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_exit_and_cancellation_clean_only_owned_group(tmp_path, cancel):
    # An unrelated service and a second active managed run must survive cleanup.
    other_ready = tmp_path / "other-ready"
    other = launch(tmp_path / "other", [sys.executable, "-c",
        f"from pathlib import Path; import time; Path({str(other_ready)!r}).touch(); time.sleep(60)"])
    service = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               start_new_session=True)
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    durable = tmp_path / "dirty-writer"
    durable.write_text("uncommitted progress")
    descendant = (
        "import signal,time,os; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    child_code = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        "Path(os.environ['TMPDIR'], 'scratch').write_text('temporary'); "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
        f"release=Path({str(release)!r})\n"
        "while not release.exists(): time.sleep(.02)\n"
        "sys.exit(7)"
    )
    target = launch(tmp_path / "target", [sys.executable, "-c", child_code])
    descendant_pid = None
    try:
        wait_for(lambda: ready.exists() and ready.read_text() and other_ready.exists())
        descendant_pid = int(ready.read_text())
        if cancel is None:
            release.touch()
        else:
            target.send_signal(cancel)
        assert target.wait(timeout=8) == (7 if cancel is None else 128 + cancel)
        assert not alive(descendant_pid)
        assert list((tmp_path / "target").iterdir()) == []
        assert other.poll() is None and service.poll() is None
        assert any((tmp_path / "other").iterdir())
        assert durable.read_text() == "uncommitted progress"
    finally:
        for process in (target, other, service):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=8)
        if descendant_pid is not None and alive(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_spawn_failure_removes_only_its_scratch(tmp_path):
    retained = tmp_path / "task-state"
    retained.write_text("keep")
    with pytest.raises(FileNotFoundError):
        supervise([str(tmp_path / "missing")], dict(os.environ), tmp_path)
    assert list(tmp_path.iterdir()) == [retained]


def test_unknown_cleanup_retains_scratch(monkeypatch, tmp_path):
    def fail(_group):
        raise RuntimeError("unverified group")

    monkeypatch.setattr("switchstand.session._stop_group", fail)
    with pytest.raises(RuntimeError, match="unverified"):
        supervise([sys.executable, "-c", "pass"], dict(os.environ), tmp_path)
    assert len(list(tmp_path.iterdir())) == 1


def test_real_terminal_io_and_foreground_restoration(tmp_path):
    pid, terminal = pty.fork()
    if pid == 0:
        try:
            result = supervise([sys.executable, "-c",
                ("import os; print('READY', flush=True); "
                "assert input() == 'hello'; "
                "assert os.tcgetpgrp(0) == os.getpgrp()")], dict(os.environ), tmp_path)
            assert result == 0 and os.tcgetpgrp(0) == os.getpgrp()
            os._exit(0)
        except (AssertionError, OSError, RuntimeError):
            os._exit(1)
    try:
        output = b""
        deadline = time.monotonic() + 8
        while b"READY" not in output and time.monotonic() < deadline:
            if select.select([terminal], [], [], .1)[0]:
                output += os.read(terminal, 4096)
        assert b"READY" in output
        os.write(terminal, b"hello\n")
        result = []
        def finished():
            value = os.waitpid(pid, os.WNOHANG)
            if value[0]:
                result.append(value[1])
                return True
            return False
        wait_for(finished)
        assert os.waitstatus_to_exitcode(result[0]) == 0
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(terminal)
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except ProcessLookupError:
            pass
