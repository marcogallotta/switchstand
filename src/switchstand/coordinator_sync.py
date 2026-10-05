"""Private fixed-scope Coordinator control for canonical-main synchronization."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .contracts import ClosedModel
from .mcp import closed_tool

SHA = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT_SECONDS = 30
GIT = "/usr/bin/git"
CANONICAL_REMOTE = "https://github.com/marcogallotta/switchstand.git"
CANONICAL_ORIGINS = frozenset({
    CANONICAL_REMOTE,
    CANONICAL_REMOTE.removesuffix(".git"),
    "git@github.com:marcogallotta/switchstand.git",
    "ssh://git@github.com/marcogallotta/switchstand.git",
})
ALLOWED_LOCAL_CONFIG_KEYS = frozenset({
    "core.bare", "core.filemode", "core.ignorecase", "core.logallrefupdates",
    "core.precomposeunicode", "core.repositoryformatversion", "core.sshcommand",
    "remote.origin.fetch", "remote.origin.url", "user.email", "user.name",
})


class CurrentnessResult(ClosedModel):
    status: Literal["CURRENT", "SYNC_REQUIRED", "BLOCKED", "UNKNOWN"]
    current_sha: str | None = None
    target_sha: str | None = None
    reason: str


class SyncResult(ClosedModel):
    status: Literal["ok", "not_applied", "unknown"]
    effect: Literal["applied", "not_sent", "unknown"]
    previous_sha: str | None = None
    target_sha: str
    resulting_sha: str | None = None
    reason: str


class HandoffSyncResult(ClosedModel):
    status: Literal["ready", "blocked", "unknown"]
    effect: Literal["applied", "not_sent", "unknown"]
    previous_sha: str | None = None
    target_sha: str | None = None
    resulting_sha: str | None = None
    reason: str


class GitFailure(RuntimeError):
    pass


class CoordinatorSync:
    """Synchronize only ``$HOME/switchstand`` main to its exact observed remote main."""

    def __init__(
        self, home: Path, *, remote_source: str = CANONICAL_REMOTE,
        accepted_origins: frozenset[str] = CANONICAL_ORIGINS,
        remote_protocol: Literal["https", "file"] = "https",
    ):
        self.repo = home / "switchstand"
        self.remote_source = remote_source
        self.accepted_origins = accepted_origins
        self.remote_protocol = remote_protocol

    def _environment(self) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_PROTOCOL_FROM_USER": "0",
        }

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [
                    GIT, "--no-replace-objects", f"--git-dir={self.repo / '.git'}",
                    f"--work-tree={self.repo}",
                    "-c", f"core.worktree={self.repo}",
                    "-c", "core.fsmonitor=false",
                    "-c", "core.hooksPath=/dev/null",
                    "-c", "core.attributesFile=/dev/null",
                    "-c", "core.sshCommand=",
                    "-c", "credential.helper=",
                    "-c", "fetch.recurseSubmodules=false",
                    "-c", "submodule.recurse=false",
                    "-c", "maintenance.auto=false",
                    "-c", "gc.auto=0",
                    "-c", "protocol.allow=never",
                    "-c", f"protocol.{self.remote_protocol}.allow=always",
                    "-c", "protocol.ext.allow=never",
                    *arguments,
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as error:
            raise GitFailure("git_timeout") from error
        if check and result.returncode:
            raise GitFailure("git_failed")
        return result

    def _validate_repo(self) -> str:
        try:
            if self.repo.is_symlink() or not self.repo.is_dir():
                raise GitFailure("canonical_checkout_unavailable")
            root = self._git(
                "rev-parse", "--path-format=absolute", "--show-toplevel"
            ).stdout.strip()
            common = self._git(
                "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
            branch = self._git("branch", "--show-current").stdout.strip()
            if Path(root) != self.repo or Path(common) != self.repo / ".git" or branch != "main":
                raise GitFailure("canonical_checkout_identity_mismatch")
            head = self._git("rev-parse", "HEAD^{commit}").stdout.strip()
            if not SHA.fullmatch(head):
                raise GitFailure("invalid_current_sha")
            return head
        except OSError as error:
            raise GitFailure("canonical_checkout_unavailable") from error

    def _clean(self) -> bool:
        return not self._git("status", "--porcelain", "--untracked-files=all").stdout

    def _validate_local_config(self) -> None:
        names = self._git(
            "config", "--local", "--no-includes", "--name-only", "--list"
        ).stdout.splitlines()
        lowered = tuple(name.lower() for name in names)
        for key in lowered:
            branch_tracking = (
                key.startswith("branch.") and key.endswith((".remote", ".merge"))
            )
            if key not in ALLOWED_LOCAL_CONFIG_KEYS and not branch_tracking:
                raise GitFailure("unsafe_local_git_config")
        origin = self._git(
            "config", "--local", "--no-includes", "--get", "remote.origin.url"
        ).stdout.strip()
        if origin not in self.accepted_origins:
            raise GitFailure("canonical_remote_identity_mismatch")

    @contextmanager
    def _config_guard(self) -> Generator[None]:
        path = self.repo / ".git" / "config.lock"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as error:
            raise GitFailure("git_config_busy") from error
        try:
            os.close(descriptor)
            yield
        finally:
            path.unlink(missing_ok=True)

    def _remote_main(self) -> str:
        result = self._git(
            "ls-remote", "--exit-code", self.remote_source, "refs/heads/main"
        )
        fields = result.stdout.split()
        if len(fields) != 2 or fields[1] != "refs/heads/main" or not SHA.fullmatch(fields[0]):
            raise GitFailure("remote_main_unavailable")
        return fields[0]

    def currentness(self) -> CurrentnessResult:
        try:
            with self._config_guard():
                self._validate_local_config()
                current = self._validate_repo()
                target = self._remote_main()
                if not self._clean():
                    return CurrentnessResult(
                        status="BLOCKED", current_sha=current, target_sha=target,
                        reason="dirty_canonical_checkout",
                    )
                if current == target:
                    return CurrentnessResult(
                        status="CURRENT", current_sha=current, target_sha=target,
                        reason="canonical_main_current",
                    )
                return CurrentnessResult(
                    status="SYNC_REQUIRED", current_sha=current, target_sha=target,
                    reason="canonical_main_differs",
                )
        except (GitFailure, OSError) as error:
            return CurrentnessResult(status="UNKNOWN", reason=str(error))

    def sync(self, target_sha: str) -> SyncResult:
        if not SHA.fullmatch(target_sha):
            return SyncResult(
                status="not_applied", effect="not_sent", target_sha=target_sha,
                reason="invalid_target_sha",
            )
        if (self.repo.is_symlink() or not self.repo.is_dir()
                or not (self.repo / ".git").is_dir()):
            return SyncResult(
                status="not_applied", effect="not_sent", target_sha=target_sha,
                reason="canonical_checkout_unavailable",
            )
        lock_path = self.repo / ".git" / "switchstand-coordinator-sync.lock"
        try:
            with lock_path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self._config_guard():
                    self._validate_local_config()
                    current = self._validate_repo()
                    return self._sync_locked(current, target_sha)
        except (OSError, GitFailure) as error:
            return SyncResult(
                status="not_applied", effect="not_sent", target_sha=target_sha,
                reason=str(error) if isinstance(error, GitFailure) else "sync_lock_unavailable",
            )

    def handoff(self, started_sha: str) -> HandoffSyncResult:
        """Synchronize canonical main and validate one outgoing generation boundary."""
        if not SHA.fullmatch(started_sha):
            return HandoffSyncResult(
                status="blocked", effect="not_sent", reason="invalid_start_sha"
            )
        if (self.repo.is_symlink() or not self.repo.is_dir()
                or not (self.repo / ".git").is_dir()):
            return HandoffSyncResult(
                status="blocked", effect="not_sent", reason="canonical_checkout_unavailable"
            )
        lock_path = self.repo / ".git" / "switchstand-coordinator-sync.lock"
        current: str | None = None
        target: str | None = None
        synced: SyncResult | None = None
        try:
            with lock_path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self._config_guard():
                    self._validate_local_config()
                    current = self._validate_repo()
                    if not self._clean():
                        return HandoffSyncResult(
                            status="blocked", effect="not_sent", previous_sha=current,
                            resulting_sha=current, reason="dirty_canonical_checkout",
                        )
                    if self._git(
                        "cat-file", "-e", f"{started_sha}^{{commit}}", check=False
                    ).returncode:
                        return HandoffSyncResult(
                            status="blocked", effect="not_sent", previous_sha=current,
                            resulting_sha=current, reason="recorded_start_commit_unavailable",
                        )
                    target = self._remote_main()
                    synced = self._sync_locked(current, target)
                    if synced.status == "unknown":
                        return HandoffSyncResult(
                            status="unknown", effect=synced.effect,
                            previous_sha=synced.previous_sha, target_sha=target,
                            resulting_sha=synced.resulting_sha, reason=synced.reason,
                        )
                    if synced.status != "ok":
                        return HandoffSyncResult(
                            status="blocked", effect=synced.effect,
                            previous_sha=synced.previous_sha, target_sha=target,
                            resulting_sha=synced.resulting_sha, reason=synced.reason,
                        )
                    resulting = synced.resulting_sha or current
                    if self._git(
                        "merge-base", "--is-ancestor", started_sha, resulting, check=False
                    ).returncode:
                        return HandoffSyncResult(
                            status="blocked", effect=synced.effect,
                            previous_sha=synced.previous_sha, target_sha=target,
                            resulting_sha=resulting,
                            reason="recorded_start_commit_is_not_ancestor",
                        )
                    return HandoffSyncResult(
                        status="ready", effect=synced.effect,
                        previous_sha=synced.previous_sha, target_sha=target,
                        resulting_sha=resulting, reason="handoff_git_boundary_ready",
                    )
        except (OSError, GitFailure) as error:
            effect = "not_sent" if synced is None else synced.effect
            return HandoffSyncResult(
                status="unknown" if effect != "not_sent" else "blocked",
                effect=effect, previous_sha=current, target_sha=target,
                resulting_sha=None if synced is None else synced.resulting_sha,
                reason=str(error) if isinstance(error, GitFailure) else "sync_lock_unavailable",
            )

    def _sync_locked(self, observed_current: str, target_sha: str) -> SyncResult:
        merge_attempted = False
        try:
            self._validate_local_config()
            current = self._validate_repo()
            if current != observed_current:
                return self._unchanged(current, target_sha, "current_head_changed")
            if not self._clean():
                return self._unchanged(current, target_sha, "dirty_canonical_checkout")
            if self._remote_main() != target_sha:
                return self._unchanged(current, target_sha, "observed_target_is_no_longer_remote_main")
            if current == target_sha:
                return SyncResult(
                    status="ok", effect="not_sent", previous_sha=current,
                    target_sha=target_sha, resulting_sha=current, reason="already_current",
                )
            self._git(
                "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                self.remote_source, "refs/heads/main",
            )
            if self._remote_main() != target_sha:
                return self._unchanged(current, target_sha, "remote_main_moved_during_sync")
            ancestor = self._git(
                "merge-base", "--is-ancestor", current, target_sha, check=False
            )
            if ancestor.returncode:
                return self._unchanged(current, target_sha, "canonical_main_is_not_ancestor")
            if self._validate_repo() != current or not self._clean():
                return self._unchanged(current, target_sha, "canonical_checkout_changed_during_sync")
            merge_attempted = True
            merged = self._git("merge", "--ff-only", "--quiet", target_sha, check=False)
            resulting = self._validate_repo()
            clean = self._clean()
            if merged.returncode:
                if resulting == current and clean:
                    return self._unchanged(current, target_sha, "fast_forward_failed")
                return SyncResult(
                    status="unknown", effect="unknown", previous_sha=current,
                    target_sha=target_sha, resulting_sha=resulting,
                    reason="fast_forward_failed_and_state_changed",
                )
            if resulting != target_sha or not clean:
                return SyncResult(
                    status="unknown", effect="unknown", previous_sha=current,
                    target_sha=target_sha, resulting_sha=resulting,
                    reason="fast_forward_readback_failed",
                )
            return SyncResult(
                status="ok", effect="applied", previous_sha=current,
                target_sha=target_sha, resulting_sha=resulting, reason="fast_forward_applied",
            )
        except GitFailure as error:
            if not merge_attempted:
                return self._unchanged(observed_current, target_sha, str(error))
            try:
                resulting = self._validate_repo()
                clean = self._clean()
            except GitFailure:
                return SyncResult(
                    status="unknown", effect="unknown", previous_sha=observed_current,
                    target_sha=target_sha, reason=str(error),
                )
            if resulting == observed_current and clean:
                return self._unchanged(observed_current, target_sha, str(error))
            return SyncResult(
                status="unknown", effect="unknown", previous_sha=observed_current,
                target_sha=target_sha, resulting_sha=resulting,
                reason=f"{error}_state_changed",
            )

    @staticmethod
    def _unchanged(current: str, target: str, reason: str) -> SyncResult:
        return SyncResult(
            status="not_applied", effect="not_sent", previous_sha=current,
            target_sha=target, resulting_sha=current, reason=reason,
        )


def build_server(control: CoordinatorSync) -> MCPServer:
    server = MCPServer("Switchstand Coordinator Control")

    async def _coordinator_currentness_get(
        api_version: Literal["1"],
    ) -> CurrentnessResult:
        """Observe fixed canonical-main currentness and its exact remote target SHA."""
        return control.currentness()

    async def _coordinator_main_sync(
        api_version: Literal["1"],
        target_sha: str = Field(pattern=r"^[0-9a-f]{40}$"),
    ) -> SyncResult:
        """Fast-forward fixed canonical main only to the exact observed remote target SHA."""
        return control.sync(target_sha)

    closed_tool(server, "coordinator_currentness_get", _coordinator_currentness_get,
                ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False))
    closed_tool(server, "coordinator_main_sync", _coordinator_main_sync,
                ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False))
    return server


def main(argv: list[str] | None = None) -> int:
    home = Path(os.environ["HOME"])
    if not home.is_absolute():
        raise SystemExit("HOME must be absolute")
    control = CoordinatorSync(home.resolve())
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        if len(arguments) != 2 or arguments[0] != "handoff":
            raise SystemExit("usage: coordinator_sync [handoff <start-sha>]")
        result = control.handoff(arguments[1])
        print(json.dumps(result.model_dump(), sort_keys=True))
        return 0 if result.status == "ready" else 2
    build_server(control).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
