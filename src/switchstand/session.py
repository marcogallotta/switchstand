"""Lifetime of one launcher-owned process group (Linux)."""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

GRACE_SECONDS = 1.0


def _live_members(group: int) -> bool:
    # The unreaped group leader pins its PID/PGID until this readback completes.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text()
        except ProcessLookupError:
            continue
        except FileNotFoundError:
            continue
        fields = stat[stat.rindex(")") + 2:].split()
        if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
            return True
    return False


def _signal_group(group: int, signum: int) -> None:
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        pass


def _stop_group(group: int) -> None:
    for signum in (signal.SIGTERM, signal.SIGKILL):
        _signal_group(group, signum)
        # Stopped jobs must be able to process TERM.
        if signum == signal.SIGTERM:
            _signal_group(group, signal.SIGCONT)
        deadline = time.monotonic() + GRACE_SECONDS
        while _live_members(group):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        else:
            return
    raise RuntimeError(f"managed process group {group} cleanup could not be verified")


def supervise(command: list[str], env: dict[str, str], temporary_parent: Path) -> int:
    """Keep durable task state; retire only this child's group and scratch directory."""
    temporary = Path(tempfile.mkdtemp(prefix="run-", dir=temporary_parent))
    child: subprocess.Popen[bytes] | None = None
    previous: dict[signal.Signals, Any] = {}
    termination: int | None = None
    foreground: int | None = None
    cleaned = False

    def cancel(signum: int, _frame: object) -> None:
        nonlocal termination
        if termination is None:
            termination = signum

    try:
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, cancel)
        if os.isatty(0):
            observed_foreground = os.tcgetpgrp(0)
            if observed_foreground != os.getpgrp():
                raise RuntimeError("managed launcher must own the foreground terminal")
            previous[signal.SIGTTOU] = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            foreground = observed_foreground
        child = subprocess.Popen(command, env=env | {"TMPDIR": str(temporary)}, process_group=0)
        if foreground is not None:
            os.tcsetpgrp(0, child.pid)
            _signal_group(child.pid, signal.SIGCONT)
        while termination is None:
            # Do not poll()/wait(): reaping the leader would allow PGID reuse.
            status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WSTOPPED
                               | os.WNOHANG | os.WNOWAIT)
            if status is not None:
                if status.si_code != os.CLD_STOPPED:
                    break
                os.waitid(os.P_PID, child.pid, os.WSTOPPED | os.WNOHANG)
                if foreground is not None:
                    os.tcsetpgrp(0, foreground)
                os.kill(os.getpid(), signal.SIGSTOP)
                if foreground is not None:
                    os.tcsetpgrp(0, child.pid)
                _signal_group(child.pid, signal.SIGCONT)
            time.sleep(0.02)
    finally:
        try:
            if child is not None:
                _stop_group(child.pid)
                child.wait(timeout=GRACE_SECONDS)
            cleaned = True
        finally:
            try:
                if foreground is not None:
                    os.tcsetpgrp(0, foreground)
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
                if cleaned:
                    shutil.rmtree(temporary)
                    if temporary.exists():
                        raise RuntimeError("managed temporary directory cleanup failed readback")
    assert child is not None and child.returncode is not None
    if termination is not None:
        return 128 + termination
    return child.returncode if child.returncode >= 0 else 128 - child.returncode
