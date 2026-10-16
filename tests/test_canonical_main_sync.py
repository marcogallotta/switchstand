from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/sync-canonical-main"
INSTALLER = ROOT / "scripts/install-canonical-main-sync"


def load_script() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("sync_canonical_main", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def setup_repositories(tmp_path: Path) -> tuple[Path, Path, str, str]:
    remote = tmp_path / "remote.git"
    canonical = tmp_path / "canonical"
    source = tmp_path / "source"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", remote],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "clone", remote, canonical], check=True, capture_output=True)
    git(canonical, "switch", "-c", "main")
    git(canonical, "config", "user.name", "Test")
    git(canonical, "config", "user.email", "test@example.invalid")
    (canonical / "tracked.txt").write_text("start\n")
    git(canonical, "add", "tracked.txt")
    git(canonical, "commit", "-m", "start")
    started = git(canonical, "rev-parse", "HEAD")
    git(canonical, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", remote, source], check=True, capture_output=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("final\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "final")
    final = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "main")
    return canonical, source, started, final


def test_clean_behind_checkout_fast_forwards(tmp_path: Path) -> None:
    canonical, _source, started, final = setup_repositories(tmp_path)
    messages: list[str] = []

    result = load_script().sync(canonical, git="git", emit=messages.append)

    assert result == 0
    assert git(canonical, "rev-parse", "HEAD") == final
    assert git(canonical, "status", "--porcelain", "--untracked-files=all") == ""
    assert messages == [f"updated: canonical main {started} -> {final}"]


def test_dirty_and_diverged_checkouts_are_unchanged(tmp_path: Path) -> None:
    canonical, source, started, _final = setup_repositories(tmp_path)
    subject = load_script()
    dirty_messages: list[str] = []
    (canonical / "untracked.txt").write_text("preserve\n")

    assert subject.sync(canonical, git="git", emit=dirty_messages.append) == 0
    assert git(canonical, "rev-parse", "HEAD") == started
    assert (canonical / "untracked.txt").read_text() == "preserve\n"
    assert dirty_messages == ["skip: canonical main is dirty"]

    (canonical / "untracked.txt").unlink()
    (canonical / "local.txt").write_text("local\n")
    git(canonical, "add", "local.txt")
    git(canonical, "commit", "-m", "local")
    local = git(canonical, "rev-parse", "HEAD")
    (source / "tracked.txt").write_text("new remote\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "new remote")
    git(source, "push", "origin", "main")
    diverged_messages: list[str] = []

    assert subject.sync(canonical, git="git", emit=diverged_messages.append) == 0
    assert git(canonical, "rev-parse", "HEAD") == local
    assert git(canonical, "status", "--porcelain", "--untracked-files=all") == ""
    assert diverged_messages == ["skip: canonical main is diverged from origin/main"]


def executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def test_installer_renders_and_enables_bounded_user_timer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    executable(repo / "scripts/sync-canonical-main", "#!/bin/sh\nexit 0\n")
    tools = tmp_path / "tools"
    executable(
        tools / "systemctl",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/systemctl-calls"\n',
    )
    env = os.environ | {"HOME": str(home), "PATH": f"{tools}:{os.environ['PATH']}"}

    subprocess.run([INSTALLER, repo], env=env, check=True)

    unit_dir = home / ".config/systemd/user"
    service = (unit_dir / "switchstand-canonical-main-sync.service").read_text()
    timer = (unit_dir / "switchstand-canonical-main-sync.timer").read_text()
    runner = home / ".local/share/switchstand/bin/sync-canonical-main"
    assert f'ExecStart="{runner}"' in service
    assert "TimeoutStartSec=2min" in service
    assert "OnUnitInactiveSec=5min" in timer
    assert "RandomizedDelaySec=30s" in timer
    assert "enable --now switchstand-canonical-main-sync.timer" in (
        home / "systemctl-calls"
    ).read_text()
