"""Fail-closed supervisor for one finite local Codex assignment."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

from switchstand.agent_tree import THREAD_SOURCE_KINDS
from switchstand.app_server import CodexAppServer

from .candidate import build_candidate, initialize_local_git, materialize_checkout
from .protocol import Claim, CoordinatorClient, CoordinatorPort, ProtocolError


BOOTSTRAP_INPUT = "Respond exactly READY."
SAFE_ENVIRONMENT = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}


@dataclass(frozen=True)
class WorkerConfig:
    coordinator_url: str
    worker_key: str
    state_root: Path
    worker_id: str
    instance_id: str
    codex_binary: Path
    auth_file: Path
    bwrap_binary: Path

    @classmethod
    def create(
        cls,
        coordinator_url: str,
        worker_key: str,
        state_root: Path,
        *,
        worker_id: str | None = None,
        instance_id: str | None = None,
    ) -> "WorkerConfig":
        codex = shutil.which("codex")
        bwrap = shutil.which("bwrap")
        auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        if not codex or not bwrap or not auth.is_file():
            raise RuntimeError("host_prerequisite_missing")
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state_root, 0o700)
        return cls(
            coordinator_url,
            worker_key,
            state_root,
            worker_id or str(uuid.uuid4()),
            instance_id or str(uuid.uuid4()),
            Path(codex).resolve(),
            auth,
            Path(bwrap).resolve(),
        )


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    thread_id: str | None
    review_verdict: str | None


class RunnerPort(Protocol):
    def bootstrap(self) -> str: ...

    def task_turn(self, workspace: Path, thread_id: str) -> ProcessResult: ...


class LeaseGuard:
    """Renew independently and kill the attached process when authority is lost."""

    def __init__(self, client: CoordinatorPort, claim: Claim, clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.claim = claim
        self.clock = clock
        self._last_success = clock()
        self._unavailable = 0
        self._lost = threading.Event()
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread = threading.Thread(target=self._loop, name="lease-renewal", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=2)

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def require_live(self) -> None:
        if self.lost:
            raise ProtocolError("stale_or_invalid_lease", 409)

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._process = process
            lost = self.lost
        if lost:
            self._kill(process)

    def detach(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def _loop(self) -> None:
        while not self._stopped.wait(1.0):
            try:
                self.client.renew(self.claim.authority)
            except ProtocolError as exc:
                if exc.code != "temporary_failure":
                    self._lose()
                    return
                self._unavailable += 1
                if self._unavailable >= 2 or self.clock() - self._last_success >= 3:
                    self._lose()
                    return
            else:
                self._unavailable = 0
                self._last_success = self.clock()

    def _lose(self) -> None:
        self._lost.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._kill(process)

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                pass


class CodexRunner:
    """Run Codex in the qualified selective-mount bubblewrap boundary."""

    def __init__(self, config: WorkerConfig, claim: Claim, guard: LeaseGuard) -> None:
        self.config = config
        self.claim = claim
        self.guard = guard
        self.job_state = config.state_root / "provider" / claim.authority.work_id
        self.job_state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.job_state, 0o700)

    def _boundary(self, workspace: Path, command: Sequence[str]) -> list[str]:
        args = [
            str(self.config.bwrap_binary),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--share-net",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/workspace",
            "--dir",
            "/codex-home",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--ro-bind",
            str(self.config.codex_binary),
            "/codex",
        ]
        if Path("/lib64").exists():
            args += ["--symlink", "usr/lib64", "/lib64"]
        for source in ("/etc/ssl", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
            if Path(source).exists():
                args += ["--ro-bind", source, source]
        args += [
            "--bind",
            str(workspace),
            "/workspace",
            "--bind",
            str(self.job_state),
            "/codex-home",
            "--ro-bind",
            str(self.config.auth_file),
            "/codex-home/auth.json",
            "--clearenv",
            "--setenv",
            "HOME",
            "/codex-home",
            "--setenv",
            "CODEX_HOME",
            "/codex-home",
        ]
        for key, value in SAFE_ENVIRONMENT.items():
            args += ["--setenv", key, value]
        return [*args, "--chdir", "/workspace", "--", "/codex", *command]

    def _run(self, workspace: Path, command: Sequence[str], prompt: str) -> ProcessResult:
        process = subprocess.Popen(
            self._boundary(workspace, command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            start_new_session=True,
        )
        self.guard.attach(process)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(prompt.encode("utf-8"))
        process.stdin.close()
        thread_id: str | None = None
        verdict: str | None = None
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "thread.started":
                    candidate = event.get("thread_id")
                    if (
                        isinstance(candidate, str)
                        and 1 <= len(candidate) <= 256
                        and candidate.isascii()
                        and candidate.isprintable()
                    ):
                        if thread_id is not None and thread_id != candidate:
                            self.guard._kill(process)
                            raise ProtocolError("invalid_request")
                        thread_id = candidate
                verdict = _find_verdict(event) or verdict
            exit_code = process.wait()
            process.stdout.close()
        finally:
            self.guard.detach(process)
        return ProcessResult(exit_code, thread_id, verdict)

    def bootstrap(self) -> str:
        workspace = Path(tempfile.mkdtemp(prefix="bootstrap-", dir=self.config.state_root))
        try:
            result = self._run(
                workspace,
                [
                    "exec",
                    "--json",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--color",
                    "never",
                    "-",
                ],
                BOOTSTRAP_INPUT,
            )
            if result.exit_code or result.thread_id is None or not self.thread_in_state_db(result.thread_id, workspace):
                raise ProtocolError("temporary_failure", 503)
            return result.thread_id
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def task_turn(self, workspace: Path, thread_id: str) -> ProcessResult:
        prompt = _task_input(self.claim)
        sandbox = "workspace-write" if self.claim.work_type == "implementation" else "read-only"
        result = self._run(
            workspace,
            [
                "exec",
                "resume",
                "--all",
                "--json",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "-c",
                f'sandbox_mode="{sandbox}"',
                thread_id,
                "-",
            ],
            prompt,
        )
        if result.thread_id is not None and result.thread_id != thread_id:
            raise ProtocolError("codex_exact_resume_failed")
        return ProcessResult(
            result.exit_code, thread_id if result.exit_code == 0 else result.thread_id, result.review_verdict
        )

    def thread_in_state_db(self, thread_id: str, workspace: Path) -> bool:
        socket_path = self.job_state / f"state-{uuid.uuid4().hex}.sock"
        process = subprocess.Popen(
            self._boundary(workspace, ["app-server", "--listen", f"unix:///codex-home/{socket_path.name}"]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={},
            start_new_session=True,
        )
        self.guard.attach(process)
        try:
            deadline = time.monotonic() + 5
            while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not socket_path.exists():
                return False
            client = CodexAppServer(
                str(socket_path), client_name="switchstand-worker", client_title="Switchstand Worker"
            )
            try:
                cursor: str | None = None
                seen: set[str] = set()
                found = False
                for _ in range(100):
                    params: dict[str, Any] = {
                        "limit": 100,
                        "sourceKinds": list(THREAD_SOURCE_KINDS),
                        "useStateDbOnly": True,
                    }
                    if cursor is not None:
                        params["cursor"] = cursor
                    response = client.thread_list(params)
                    data = response.get("data")
                    if not isinstance(data, list) or len(data) > 100:
                        return False
                    if any(isinstance(item, Mapping) and item.get("id") == thread_id for item in data):
                        found = True
                    next_cursor = response.get("nextCursor")
                    if next_cursor is None:
                        return found
                    if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                        return False
                    seen.add(next_cursor)
                    cursor = next_cursor
                return False
            finally:
                client.close()
        except (OSError, RuntimeError):
            return False
        finally:
            self.guard.detach(process)
            LeaseGuard._kill(process)
            socket_path.unlink(missing_ok=True)


def _find_verdict(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() in {"PASS", "BLOCK"}:
        return value.strip()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in {"text", "output_text"}:
                verdict = _find_verdict(nested)
                if verdict:
                    return verdict
            elif key in {"item", "message", "content"}:
                verdict = _find_verdict(nested)
                if verdict:
                    return verdict
    if isinstance(value, list):
        for nested in value:
            verdict = _find_verdict(nested)
            if verdict:
                return verdict
    return None


def _task_input(claim: Claim) -> str:
    acceptance = "\n".join(f"- {item}" for item in claim.acceptance)
    review = "\nFinish with exactly PASS or BLOCK." if claim.work_type == "review" else ""
    return f"{claim.source_text}\n\nAcceptance:\n{acceptance}{review}"


class Worker:
    """Poll and execute at most one claim per ``run_once`` call."""

    def __init__(
        self,
        config: WorkerConfig,
        *,
        client: CoordinatorPort | None = None,
        runner_factory: Callable[[WorkerConfig, Claim, LeaseGuard], RunnerPort] = CodexRunner,
    ) -> None:
        self.config = config
        config.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(config.state_root, 0o700)
        self.client = client or CoordinatorClient(config.coordinator_url, config.worker_key)
        self.runner_factory = runner_factory

    def run_once(self) -> bool:
        self.client.register(self.config.worker_id, self.config.instance_id)
        claim = self.client.claim(self.config.worker_id, self.config.instance_id)
        if claim is None:
            return False
        guard = LeaseGuard(self.client, claim)
        guard.start()
        try:
            self._execute(claim, guard)
        finally:
            guard.stop()
        return True

    def _execute(self, claim: Claim, guard: LeaseGuard) -> None:
        authority = claim.authority
        if claim.accepted_candidate is not None:
            guard.require_live()
            self.client.complete(
                authority,
                _operation("complete"),
                status="succeeded",
                candidate_id=claim.accepted_candidate["candidate_id"],
                summary_code="accepted_candidate_recovered",
            )
            return
        workspace = self.config.state_root / "workspaces" / authority.work_id / f"attempt-{authority.fence}"
        workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        sequence = claim.prior_checkpoint["sequence"] if claim.prior_checkpoint else 0
        thread_id = claim.codex_thread_id
        try:
            payload, headers = self.client.checkout(claim)
            guard.require_live()
            materialize_checkout(claim, payload, headers, workspace)
            initialize_local_git(workspace)
            sequence += 1
            self.client.checkpoint(
                authority, _operation("checkout"), sequence, "checkout_ready", thread_id, "checkout_validated"
            )
            runner = self.runner_factory(self.config, claim, guard)
            if thread_id is None:
                thread_id = runner.bootstrap()
                guard.require_live()
                sequence += 1
                self.client.checkpoint(
                    authority, _operation("adopt"), sequence, "codex_started", thread_id, "thread_adopted"
                )
            sequence += 1
            self.client.checkpoint(
                authority, _operation("working"), sequence, "working", thread_id, "finite_turn_started"
            )
            result = runner.task_turn(workspace, thread_id)
            guard.require_live()
            if result.exit_code:
                self.client.complete(
                    authority, _operation("complete"), status="scope_return", summary_code="codex_exact_resume_failed"
                )
                return
            if claim.work_type == "review":
                if result.review_verdict not in {"PASS", "BLOCK"}:
                    self.client.complete(
                        authority, _operation("complete"), status="failed", summary_code="review_verdict_invalid"
                    )
                    return
                self.client.complete(
                    authority,
                    _operation("complete"),
                    status="succeeded",
                    review_verdict=result.review_verdict,
                    summary_code="review_complete",
                )
                return
            sequence += 1
            self.client.checkpoint(
                authority, _operation("testing"), sequence, "testing", thread_id, "candidate_validation"
            )
            manifest = build_candidate(
                claim,
                workspace,
                operation_id=_operation("candidate"),
                message="bounded worker candidate",
                check_summaries=[{"name": "codex_turn", "outcome": "PASS", "summary": "finite turn completed"}],
            )
            response = self.client.submit_candidate(authority, manifest)
            if set(response) != {"candidate_id", "manifest_sha", "status"} or response["status"] != "candidate_ready":
                raise ProtocolError("invalid_request")
            guard.require_live()
            self.client.complete(
                authority,
                _operation("complete"),
                status="succeeded",
                candidate_id=response["candidate_id"],
                summary_code="candidate_complete",
                checks=[{"name": "codex_turn", "outcome": "PASS"}],
            )
        except ProtocolError as exc:
            if guard.lost or exc.code in {
                "stale_or_invalid_lease",
                "terminal_immutable",
                "publication_already_authorized",
                "temporary_failure",
            }:
                return
            try:
                guard.require_live()
                self.client.complete(
                    authority, _operation("complete"), status="scope_return", summary_code=_summary_code(exc.code)
                )
            except ProtocolError:
                return


def _operation(kind: str) -> str:
    return f"{kind}:{uuid.uuid4()}"


def _summary_code(code: str) -> str:
    normalized = "".join(character if character.isalnum() or character == "_" else "_" for character in code.lower())
    return (normalized or "worker_failure")[:80]
