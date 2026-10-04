"""Private fixed-scope Coordinator control for canonical-main synchronization."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .contracts import ClosedModel
from .mcp import closed_tool

SHA = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT_SECONDS = 30


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


class GitFailure(RuntimeError):
    pass


class CoordinatorSync:
    """Synchronize only ``$HOME/switchstand`` main to its exact observed remote main."""

    def __init__(self, home: Path):
        self.repo = home / "switchstand"

    @staticmethod
    def _environment() -> dict[str, str]:
        return os.environ | {"GIT_TERMINAL_PROMPT": "0"}

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *arguments],
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

    def _remote_main(self) -> str:
        result = self._git("ls-remote", "--exit-code", "origin", "refs/heads/main")
        fields = result.stdout.split()
        if len(fields) != 2 or fields[1] != "refs/heads/main" or not SHA.fullmatch(fields[0]):
            raise GitFailure("remote_main_unavailable")
        return fields[0]

    def currentness(self) -> CurrentnessResult:
        try:
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
        except GitFailure as error:
            return CurrentnessResult(status="UNKNOWN", reason=str(error))

    def sync(self, target_sha: str) -> SyncResult:
        if not SHA.fullmatch(target_sha):
            return SyncResult(
                status="not_applied", effect="not_sent", target_sha=target_sha,
                reason="invalid_target_sha",
            )
        try:
            current = self._validate_repo()
        except GitFailure as error:
            return SyncResult(
                status="not_applied", effect="not_sent", target_sha=target_sha,
                reason=str(error),
            )
        lock_path = self.repo / ".git" / "switchstand-coordinator-sync.lock"
        try:
            with lock_path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return self._sync_locked(current, target_sha)
        except OSError:
            return SyncResult(
                status="not_applied", effect="not_sent", previous_sha=current,
                target_sha=target_sha, resulting_sha=current, reason="sync_lock_unavailable",
            )

    def _sync_locked(self, observed_current: str, target_sha: str) -> SyncResult:
        try:
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
                "origin", "refs/heads/main",
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


def main() -> None:
    home = Path(os.environ["HOME"])
    if not home.is_absolute():
        raise SystemExit("HOME must be absolute")
    build_server(CoordinatorSync(home.resolve())).run()


if __name__ == "__main__":
    main()
