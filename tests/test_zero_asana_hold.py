# pyright: reportPrivateUsage=false

import hashlib
import json
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import switchstand.zero_asana_hold as hold
from switchstand.edge_maintenance import Config, Failed
from switchstand.work_corpus import load_manifest

SHA = "a" * 40
def digested(value: dict[str, object]) -> dict[str, object]:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return value | {"sha256": hashlib.sha256(body).hexdigest()}
class Context:
    def __init__(self, value: object, enter=lambda: None, exit=lambda _error: None):
        self.value, self.enter, self.exit = value, enter, exit
    async def __aenter__(self):
        self.enter()
        return self.value
    async def __aexit__(self, kind, _error, _traceback):
        self.exit(kind)
class Connection:
    def __init__(self):
        self.in_transaction = self.committed = False
        self.calls: list[str] = []
    def begin(self):
        def entered():
            self.in_transaction = True
        def exited(error):
            self.in_transaction = False
            self.committed = error is None
        return Context(None, entered, exited)
    async def execute(self, statement: object):
        assert "LOCK TABLE work_handles, work_event_handles IN SHARE MODE" in str(statement)
        self.calls.append("lock")
class Engine:
    def __init__(self, connection: Connection):
        self.connection = connection
    def connect(self):
        return Context(self.connection)
class Operations:
    def __init__(self, root: Path):
        self.lock_path = root / "exact.lock"
    def preflight(self, _paths, _source_candidate): pass
    def install_gate(self): pass
    def prove_gate(self): pass
    def stop_and_prove(self): pass
    def snapshot_current(self, target):
        target.write_bytes(b"fastmcp")
        return hashlib.sha256(b"fastmcp").hexdigest()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    paths, connection, operations = hold.HoldPaths.create(attempt), Connection(), Operations(tmp_path)
    state = {"flock": False, "captures": 0}
    @contextmanager
    def lock(path):
        assert path == operations.lock_path
        state["flock"] = True
        try: yield
        finally: state["flock"] = False
    async def capture(subject, *_args, **_kwargs):
        assert subject is connection and connection.in_transaction
        state["captures"] += 1
        connection.calls.append(f"corpus-{state['captures']}")
        return digested({"schema_version": 1, "rows": [], "exceptions": []})
    async def source(subject, *_args, **_kwargs):
        assert subject is connection and connection.in_transaction
        connection.calls.append("export")
        return digested({"schema_version": 1, "records": []})
    async def identity(subject):
        assert subject is connection and connection.in_transaction
        return "d" * 64
    original = hold.write_manifest
    def write(path, document):
        if path == paths.final_receipt:
            assert connection.committed and state["flock"]
        original(path, document)
    monkeypatch.setattr(hold, "exclusive_lock", lock)
    monkeypatch.setattr(hold, "capture_manifest_connection", capture)
    monkeypatch.setattr(hold, "source_parity_connection", source)
    monkeypatch.setattr(hold, "_database_identity", identity)
    monkeypatch.setattr(hold, "write_manifest", write)
    return paths, connection, operations, state
async def no_tombstones(_corpus, _path):
    return None
async def test_hold_uses_one_locked_transaction_and_truthful_receipts(tmp_path, monkeypatch):
    paths, connection, operations, state = setup(tmp_path, monkeypatch)
    async def continue_cutover(subject, _artifacts):
        assert subject is connection and connection.in_transaction and state["flock"]
        assert load_manifest(paths.prepared_receipt)["phase"] == "PREPARED"
        assert not paths.final_receipt.exists()
        connection.calls.append("continue")
        return "complete"
    result = await hold.production_hold(
        cast(Any, Engine(connection)), cast(Any, object()), SHA,
        paths, operations, no_tombstones, continue_cutover,
    )
    assert result == "complete" and not state["flock"]
    assert connection.calls == ["lock", "corpus-1", "export", "corpus-2", "continue"]
    assert load_manifest(paths.final_receipt)["postgres_transaction"].endswith(
        "THROUGH_COMMIT_THEN_RELEASED"
    )
async def test_failed_continuation_never_emits_final_receipt(tmp_path, monkeypatch):
    paths, connection, operations, state = setup(tmp_path, monkeypatch)
    async def fail(*_arguments):
        raise RuntimeError("cutover failed")
    with pytest.raises(RuntimeError, match="cutover failed"):
        await hold.production_hold(
            cast(Any, Engine(connection)), cast(Any, object()), SHA,
            paths, operations, no_tombstones, fail,
        )
    assert load_manifest(paths.prepared_receipt)["terminal"] is False
    assert not paths.final_receipt.exists() and not connection.committed and not state["flock"]
async def test_changed_second_corpus_blocks_continuation_and_final_receipt(tmp_path, monkeypatch):
    paths, connection, operations, state = setup(tmp_path, monkeypatch)
    async def changed(subject, *_args, **_kwargs):
        assert subject is connection and connection.in_transaction
        state["captures"] += 1
        return digested({"schema_version": 1, "rows": [state["captures"]]})
    async def must_not_continue(*_arguments):
        raise AssertionError("continuation must not run")
    monkeypatch.setattr(hold, "capture_manifest_connection", changed)
    with pytest.raises(ValueError, match="corpus manifests do not match"):
        await hold.production_hold(
            cast(Any, Engine(connection)), cast(Any, object()), SHA,
            paths, operations, no_tombstones, must_not_continue,
        )
    assert not paths.final_receipt.exists() and not connection.committed
def test_host_commands_use_machine_and_snapshot_is_create_new(tmp_path, monkeypatch):
    assert not hasattr(hold, "deploy")
    config = Config(
        tmp_path / "attempt", tmp_path / "current", "b" * 40,
        tmp_path / "candidate", "c" * 40, tmp_path / "candidate-launcher", "d" * 64,
        tmp_path / "launcher", "e" * 64, tmp_path / "fastmcp", tmp_path / "edge.env",
        "production", lock_path=tmp_path / "exact.lock",
    )
    config.attempt_dir.mkdir()
    operations, commands = hold.HostHoldOperations(config), []
    def run(command, *, check=True):
        del check
        commands.append(command)
        if command[0] == "systemd-run":
            Path(command[command.index("-cf") + 1]).write_bytes(b"snapshot")
        return subprocess.CompletedProcess(command, 0, "inactive\n", "")
    monkeypatch.setattr(hold, "run_host_command", run)
    operations._systemctl("is-active", config.service, check=False)
    target = config.attempt_dir / "fastmcp.after-stop.tar"
    operations.snapshot_current(target)
    assert all(f"--machine={hold.HOST_MACHINE}" in command for command in commands)
    with pytest.raises(Failed, match="already exists"):
        operations.snapshot_current(target)
def test_host_preflight_rejects_artifact_path_outside_attempt(tmp_path, monkeypatch):
    config = Config(
        tmp_path / "attempt", tmp_path / "current", "b" * 40,
        tmp_path / "candidate", "c" * 40, tmp_path / "candidate-launcher", "d" * 64,
        tmp_path / "launcher", "e" * 64, tmp_path / "fastmcp", tmp_path / "edge.env",
        "production", lock_path=tmp_path / "exact.lock",
    )
    monkeypatch.setattr(hold, "validate_target", lambda _config: None)
    escaped = replace(hold.HoldPaths.create(config.attempt_dir), final_receipt=tmp_path / "escape")
    with pytest.raises(Failed, match="attempt or source candidate"):
        hold.HostHoldOperations(config).preflight(escaped, config.candidate_sha)
