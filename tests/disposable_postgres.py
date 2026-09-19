"""Owned local PostgreSQL for qualification; never consumes a supplied database URL.

Run: python tests/disposable_postgres.py scripts/check tests/test_chatgpt_edge_process.py
All durable state and logs remain in the private writer's .qualification directory.
"""

import ctypes
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"not an owner-only directory: {path}")


def clean_environment():
    # No ambient provider credentials, database settings, libpq defaults or proxy settings.
    keys = ("PATH", "HOME", "LANG", "SWITCHSTAND_CHECK_VENV", "SWITCHSTAND_CHECK_MANIFEST")
    return {key: os.environ[key] for key in keys if key in os.environ} | {
        "PYTHONPATH": str(ROOT / "src"),
    }


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def exited(process):
    # Observe without reaping: the child PID continues to pin the owned process group.
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


@contextmanager
def owned_process(command, env, log, *, new_session=True):
    with log.open("xb") as output:
        process = subprocess.Popen(command, env=env, cwd=ROOT, stdout=output, stderr=output,
                                   start_new_session=new_session)
        send = os.killpg if new_session else os.kill
        try:
            yield process
        finally:
            try:
                send(process.pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while exited(process) is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                # Also stop any remaining descendants before releasing the leader's PID.
                send(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


@contextmanager
def disposable_postgres():
    parent = ROOT / ".qualification"
    private_directory(parent)
    run = parent / secrets.token_hex(16)
    run.mkdir(mode=0o700)  # Collision is a failure, never adoption or deletion.
    env = clean_environment()
    scratch = run / "scratch"
    scratch.mkdir(mode=0o700)
    env["TMPDIR"] = str(scratch)
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], env=env, text=True).strip())
    password = secrets.token_hex(32)
    password_file = run / "password"
    password_file.touch(mode=0o600)
    password_file.write_text(password)
    data = run / "data"
    with (run / "initdb.log").open("xb") as log:
        subprocess.run([str(bindir / "initdb"), "-D", str(data), "-U", "fixture",
                        "--auth=scram-sha-256", "--pwfile", str(password_file)],
                       env=env, stdout=log, stderr=log, check=True, timeout=30)
    password_file.unlink()
    port = free_port()
    connection = {"host": "127.0.0.1", "port": port, "user": "fixture", "password": password,
                  "dbname": "postgres", "connect_timeout": 1}
    command = [str(bindir / "postgres"), "-D", str(data), "-h", "127.0.0.1", "-p", str(port),
               "-k", "", "-c", "external_pid_file=", "-c", "ssl=off"]
    with owned_process(command, env, run / "postgres.log") as process:
        deadline = time.monotonic() + 15
        while True:
            if exited(process) is not None:
                raise RuntimeError(f"owned PostgreSQL exited; see {run}")
            try:
                with psycopg.connect(**connection, autocommit=True) as database:
                    actual = database.execute("SHOW data_directory").fetchone()[0]
                    if Path(actual).resolve() != data.resolve():
                        raise RuntimeError("PostgreSQL ownership mismatch")
                    database.execute("CREATE DATABASE switchstand_test")
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"owned PostgreSQL did not start; see {run}") from None
                time.sleep(0.05)
        url = f"postgresql+psycopg://fixture:{password}@127.0.0.1:{port}/switchstand_test"
        env |= {"TEST_DATABASE_URL": url, "DATABASE_URL": url,
                "SWITCHSTAND_REQUIRE_TEST_DATABASE": "1", "QUALIFICATION_DIRECTORY": str(run)}
        with (run / "migrations.log").open("xb") as log:
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                           env=env, stdout=log, stderr=log, check=True, timeout=30)
        yield env, run


@contextmanager
def reap_descendants():
    """Linux subreaper contains nested timeout/session groups on runner cancellation."""
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    if children.read_text().strip():
        raise RuntimeError("qualification supervisor requires no pre-existing children")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot become qualification subreaper")
    try:
        yield
    finally:
        # Only our direct children are listed; orphaned descendants are adopted here.
        # Pin each before signalling. Killing a parent exposes its children next pass.
        deadline = time.monotonic() + 10
        while children.read_text().strip():
            for value in children.read_text().split():
                pid = int(value)
                try:
                    descriptor = os.pidfd_open(pid)
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    finally:
                        os.close(descriptor)
                    os.waitpid(pid, os.WNOHANG)
                except ProcessLookupError:
                    pass
            if time.monotonic() >= deadline:
                raise RuntimeError("owned descendant cleanup incomplete; preserve evidence")
            time.sleep(0.02)
        if libc.prctl(36, 0, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "cannot restore subreaper setting")


def main():
    if len(sys.argv) < 2:
        raise SystemExit("provide the qualification command to run")
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, interrupted)
    with reap_descendants(), disposable_postgres() as (env, run):
        print(f"Disposable PostgreSQL evidence: {run}", flush=True)
        with owned_process(sys.argv[1:], env, run / "qualification.log") as process:
            result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
            status = result.si_status if result.si_code == os.CLD_EXITED else 128 + result.si_status
        print((run / "qualification.log").read_text(), end="")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
