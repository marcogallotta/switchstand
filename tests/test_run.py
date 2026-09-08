import json
import os
from uuid import uuid4

import pytest

from switchstand.run import (
    RECEIPT,
    RunReceipt,
    create_receipt,
    inspect_receipt,
    process_start_token,
)


def stat(token):
    return f"123 (command with ) spaces) S {' '.join(['0'] * 18)} {token} 0\n"


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


def test_inspect_receipt_reports_running_stopped_lost_and_unknown(tmp_path):
    value, path = receipt(tmp_path)
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    (process / "stat").write_text(stat(42))
    assert inspect_receipt(path, tmp_path, "owned", proc).status == "running"
    (process / "stat").write_text(stat(43))
    assert inspect_receipt(path, tmp_path, "owned", proc).status == "lost"
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
