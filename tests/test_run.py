import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from switchstand.run import (
    RECEIPT,
    RunReceipt,
    create_receipt,
    inspect_receipt,
    process_start_token,
    stop_receipt,
)


def stat(token, state="S"):
    return f"123 (command with ) spaces) {state} {' '.join(['0'] * 18)} {token} 0\n"


def receipt(tmp_path, token=42):
    value = RunReceipt(
        run_id=uuid4(), active_work_id=uuid4(), worktree=str(tmp_path), branch="owned",
        pid=123, start_token=token, started_at="2026-09-08T12:00:00Z",
    )
    path = tmp_path / "switchstand-run.json"
    path.write_text(value.model_dump_json())
    return value, path


def test_process_start_token_handles_parentheses_in_command(tmp_path):
    path = tmp_path / "123"
    path.mkdir()
    (path / "stat").write_text(stat(42))
    assert process_start_token(123, tmp_path) == 42


def test_stop_wrapper_uses_dependency_complete_repository_python(tmp_path):
    wrapper = Path(__file__).parents[1] / "scripts" / "switchstand-run-stop"
    completed = subprocess.run(
        [wrapper, "unexpected"], cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 1
    assert completed.stderr.strip() == "usage: switchstand-run-stop"


def test_inspect_receipt_reports_running_stopped_lost_and_unknown(tmp_path):
    value, path = receipt(tmp_path)
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    (process / "stat").write_text(stat(42))
    assert inspect_receipt(path, tmp_path, "owned", proc).status == "running"
    (process / "stat").write_text(stat(43))
    assert inspect_receipt(path, tmp_path, "owned", proc).status == "lost"
    (process / "stat").write_text(stat(42, "Z"))
    assert inspect_receipt(path, tmp_path, "owned", proc).status == "stopped"
    (process / "stat").unlink()
    assert inspect_receipt(path, tmp_path, "owned", proc).status == "stopped"
    path.write_text(json.dumps({"run_id": str(value.run_id)}))
    assert inspect_receipt(path, tmp_path, "owned", proc).status == "unknown"


def test_create_receipt_is_private_and_refuses_to_replace_live_run(tmp_path):
    work_id = uuid4()
    first = create_receipt(tmp_path, "owned", work_id, tmp_path)
    path = tmp_path / RECEIPT
    assert RunReceipt.model_validate_json(path.read_text()) == first
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="must be stopped"):
        create_receipt(tmp_path, "owned", work_id, tmp_path)
    path.write_text(first.model_copy(update={"pid": 2**30}).model_dump_json())
    second = create_receipt(tmp_path, "owned", work_id, tmp_path)
    assert second.run_id != first.run_id and second.pid == os.getpid()


def test_lost_receipt_can_be_replaced_without_touching_reused_process(tmp_path):
    first = RunReceipt(
        run_id=uuid4(), active_work_id=uuid4(), worktree=str(tmp_path), branch="owned",
        pid=os.getpid(), start_token=process_start_token(os.getpid()) + 1,
        started_at="2026-09-08T12:00:00Z",
    )
    path = tmp_path / RECEIPT
    path.write_text(first.model_dump_json())
    assert inspect_receipt(path, tmp_path, "owned").status == "lost"
    second = create_receipt(tmp_path, "owned", first.active_work_id, tmp_path)
    assert second.run_id != first.run_id


def test_stop_receipt_forces_only_exact_owned_process(monkeypatch, tmp_path):
    monkeypatch.setattr("switchstand.run.GRACE_SECONDS", 0.01)
    owned = subprocess.Popen(
        [sys.executable, "-c",
         "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True,
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert owned.stdout is not None and owned.stdout.readline() == "ready\n"
        value = RunReceipt(
            run_id=uuid4(), active_work_id=uuid4(), worktree=str(tmp_path), branch="owned",
            pid=owned.pid, start_token=process_start_token(owned.pid),
            started_at="2026-09-08T12:00:00Z",
        )
        path = tmp_path / RECEIPT
        path.write_text(value.model_dump_json())
        assert stop_receipt(path, tmp_path, "owned").status == "stopped"
        assert owned.wait(timeout=1) == -signal.SIGKILL
        assert unrelated.poll() is None
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait()


def test_stop_receipt_returns_after_graceful_exit(monkeypatch, tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    sent = []
    send = signal.pidfd_send_signal
    try:
        value = RunReceipt(
            run_id=uuid4(), active_work_id=uuid4(), worktree=str(tmp_path), branch="owned",
            pid=process.pid, start_token=process_start_token(process.pid),
            started_at="2026-09-08T12:00:00Z",
        )
        path = tmp_path / RECEIPT
        path.write_text(value.model_dump_json())
        monkeypatch.setattr(signal, "pidfd_send_signal",
                            lambda fd, sig: sent.append(sig) or send(fd, sig))
        assert stop_receipt(path, tmp_path, "owned").status == "stopped"
        assert sent == [signal.SIGTERM]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_stop_receipt_reports_lost_and_unknown_without_signalling(monkeypatch, tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        value = RunReceipt(
            run_id=uuid4(), active_work_id=uuid4(), worktree=str(tmp_path), branch="owned",
            pid=process.pid, start_token=process_start_token(process.pid) + 1,
            started_at="2026-09-08T12:00:00Z",
        )
        path = tmp_path / RECEIPT
        path.write_text(value.model_dump_json())
        monkeypatch.setattr(signal, "pidfd_send_signal",
                            lambda *args: pytest.fail("lost process must not be signalled"))
        assert stop_receipt(path, tmp_path, "owned").status == "lost"
        assert process.poll() is None
        path.write_text("not json")
        assert stop_receipt(path, tmp_path, "owned").status == "unknown"
    finally:
        process.kill()
        process.wait()
