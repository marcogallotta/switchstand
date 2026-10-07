from __future__ import annotations

import os
import selectors
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "scripts" / "install-codex-shim"


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(autouse=True)
def fake_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = tmp_path / "tools"
    executable(
        tools / "systemctl",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/systemctl-calls"\n',
    )
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")


def install(home: Path) -> Path:
    result = subprocess.run(
        [INSTALLER], env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return home / ".local/bin/codex"


def poisoned_git_environment(primary: Path) -> dict[str, str]:
    return {
        "GIT_DIR": str(primary / ".git"),
        "GIT_WORK_TREE": str(primary),
        "GIT_COMMON_DIR": str(primary / ".git"),
        "GIT_CEILING_DIRECTORIES": str(primary),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "true",
    }


@pytest.mark.parametrize("checkout_state", ["missing", "broken"])
def test_outside_repo_launch_bypasses_missing_or_broken_checkout(
    tmp_path: Path, checkout_state: str,
) -> None:
    home = tmp_path / "home"
    result_file = tmp_path / "result"
    executable(
        home / ".codex/packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "real\\n%s\\n" "$*" > "$RESULT"\n',
    )
    if checkout_state == "broken":
        (home / "switchstand").mkdir(parents=True)
        (home / "switchstand/.git").write_text("not a git directory\n")
    launcher = install(home)
    outside = home / "outside"
    outside.mkdir()

    result = subprocess.run(
        [launcher, "exec", "hello"], cwd=outside,
        env=os.environ | {"HOME": str(home), "RESULT": str(result_file)},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == ["real", "exec hello"]


def test_launch_redirects_updater_visible_command_away_from_shim(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result_file = tmp_path / "result"
    executable(
        home / ".codex/packages/standalone/current/bin/codex",
        '#!/bin/sh\n'
        'case ":$PATH:" in\n'
        '  *":$CODEX_INSTALL_DIR:"*) ;;\n'
        '  *) printf "profile-rewrite\\n" > "$PROFILE_RESULT" ;;\n'
        'esac\n'
        'printf "%s\\n%s\\n" "$CODEX_INSTALL_DIR" "$PATH" > "$RESULT"\n',
    )
    launcher = install(home)
    outside = home / "outside"
    outside.mkdir()

    result = subprocess.run(
        [launcher, "--version"], cwd=outside,
        env=os.environ | {
            "HOME": str(home),
            "RESULT": str(result_file),
            "PROFILE_RESULT": str(tmp_path / "profile-result"),
            "CODEX_INSTALL_DIR": str(home / ".local/bin"),
            "PATH": f"{home / '.local/bin'}:/usr/bin:/bin",
        },
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    updater_bin = home / ".local/state/switchstand/codex/updater-bin"
    install_dir, child_path = result_file.read_text().splitlines()
    assert install_dir == str(updater_bin)
    assert child_path.split(":") == [
        str(home / ".local/bin"), "/usr/bin", "/bin", str(updater_bin),
    ]
    assert not (tmp_path / "profile-result").exists()
    assert not launcher.is_symlink()


def test_inside_canonical_git_common_delegates_to_repo_dispatcher(tmp_path: Path) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", primary, "init", "-b", "main"],
        check=True, capture_output=True,
    )
    git_env = os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "-C", primary, "commit", "--allow-empty", "-m", "initial"],
        env=git_env, check=True, capture_output=True,
    )
    result_file = tmp_path / "result"
    executable(
        primary / "scripts/codex-dispatch",
        '#!/bin/sh\nprintf "dispatcher\\n%s\\n%s\\n%s\\n" "$*" '
        '"$CODEX_INSTALL_DIR" "$SWITCHSTAND_CODEX_WAKEFUL" > "$RESULT"\n',
    )
    executable(
        home / ".codex/packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "real\\n%s\\n" "$*" > "$RESULT"\n',
    )
    launcher = install(home)
    nested = primary / "nested"
    nested.mkdir()

    result = subprocess.run(
        [launcher, "resume", "test-session"], cwd=nested,
        env=os.environ | {
            "HOME": str(home),
            "RESULT": str(result_file),
            "GIT_CEILING_DIRECTORIES": str(nested),
            "GIT_DISCOVERY_ACROSS_FILESYSTEM": "false",
        },
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == [
        "dispatcher", "resume test-session",
        str(home / ".local/state/switchstand/codex/updater-bin"),
        "OFF",
    ]

    result = subprocess.run(
        [launcher, "resume", "explicit-off"], cwd=nested,
        env=os.environ | {
            "HOME": str(home), "RESULT": str(result_file),
            "SWITCHSTAND_CODEX_WAKEFUL": "OFF",
        },
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == [
        "dispatcher", "resume explicit-off",
        str(home / ".local/state/switchstand/codex/updater-bin"),
        "OFF",
    ]

    writer = home / "writer"
    subprocess.run(
        ["git", "-C", primary, "worktree", "add", "-b", "writer", writer],
        env=git_env, check=True, capture_output=True,
    )
    result = subprocess.run(
        [launcher, "exec", "linked"], cwd=writer,
        env=os.environ | {
            "HOME": str(home), "RESULT": str(result_file),
            "SWITCHSTAND_CODEX_WAKEFUL": "PILOT",
        },
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == [
        "dispatcher", "exec linked",
        str(home / ".local/state/switchstand/codex/updater-bin"),
        "PILOT",
    ]


def test_outside_repo_ignores_ambient_git_repository_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    primary = home / "switchstand"
    primary.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", primary, "init", "-b", "main"],
        check=True, capture_output=True,
    )
    result_file = tmp_path / "result"
    executable(
        primary / "scripts/codex-dispatch",
        '#!/bin/sh\nprintf "dispatcher\\n" > "$RESULT"\n',
    )
    executable(
        home / ".codex/packages/standalone/current/bin/codex",
        '#!/bin/sh\nprintf "real\\n%s\\n" "$*" > "$RESULT"\n',
    )
    launcher = install(home)
    outside = home / "outside"
    outside.mkdir()

    result = subprocess.run(
        [launcher, "exec", "outside"], cwd=outside,
        env=os.environ | {
            "HOME": str(home),
            "RESULT": str(result_file),
        } | poisoned_git_environment(primary),
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_file.read_text().splitlines() == ["real", "exec outside"]


def test_installer_replaces_checkout_symlink_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    destination = home / ".local/bin/codex"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(ROOT / "scripts/codex-dispatch")

    launcher = install(home)
    first = launcher.stat()
    first_content = launcher.read_bytes()
    assert not launcher.is_symlink()
    assert first.st_mode & stat.S_IXUSR

    assert install(home) == launcher
    second = launcher.stat()
    assert second.st_ino == first.st_ino
    assert launcher.read_bytes() == first_content


def test_materialized_repair_recovers_updater_overwrite_without_checkout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launcher = install(home)
    original = launcher.read_bytes()
    materialized = home / ".local/state/switchstand/codex/shim/codex-shim"
    assert materialized.read_bytes() == original == (ROOT / "scripts/codex-shim").read_bytes()
    real = home / ".codex/packages/standalone/current/bin/codex"
    executable(real, "#!/bin/sh\nexit 0\n")
    # Actual updater boundary: replace the visible command with a real-binary symlink.
    launcher.unlink()
    launcher.symlink_to(real)
    repair = home / ".local/state/switchstand/codex/shim/install-codex-shim"
    subprocess.run(
        [repair, "--repair-only"], env=os.environ | {"HOME": str(home)}, check=True,
    )
    assert not launcher.is_symlink()
    assert launcher.read_bytes() == original
    assert real.read_text() == "#!/bin/sh\nexit 0\n"
    units = home / ".config/systemd/user"
    assert "PathChanged=%h/.local/bin/codex" in (units / "switchstand-codex-shim.path").read_text()
    assert str(ROOT) not in (units / "switchstand-codex-shim.service").read_text()


def test_installer_reports_unavailable_recovery_manager(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    executable(tools / "systemctl", "#!/bin/sh\nexit 1\n")
    home = tmp_path / "home"
    result = subprocess.run(
        [INSTALLER], env=os.environ | {"HOME": str(home)}, capture_output=True, check=False,
    )
    assert result.returncode != 0
    # Restored routing is not proof of active recurrence protection.
    assert (home / ".local/bin/codex").read_bytes() == (ROOT / "scripts/codex-shim").read_bytes()


def test_replacement_after_repair_mv_is_recovered_by_periodic_check(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launcher = install(home)
    original = launcher.read_bytes()
    repair = home / ".local/state/switchstand/codex/shim/install-codex-shim"
    real = home / ".codex/packages/standalone/current/bin/codex"
    executable(real, "#!/bin/sh\nexit 0\n")
    launcher.unlink()
    launcher.symlink_to(real)

    # Pause the real repair immediately after its atomic mv, before service exit.
    # No path event is delivered after this point: exercise the missed-watch case.
    tools = tmp_path / "boundary-tools"
    mv = shutil.which("mv")
    assert mv is not None
    executable(
        tools / "mv",
        f'#!/bin/sh\n"{mv}" "$@" || exit $?\nprintf "replaced\\n"\nread -r release\n',
    )
    env = os.environ | {"HOME": str(home)}
    process = subprocess.Popen(
        [repair, "--repair-only"],
        env=env | {"PATH": f"{tools}:{env['PATH']}"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "repair did not reach atomic replacement"
        assert process.stdout.readline() == "replaced\n"
        assert launcher.read_bytes() == original
        assert process.poll() is None
        launcher.unlink()
        launcher.symlink_to(real)
        _, error = process.communicate("release\n", timeout=5)
        assert process.returncode == 0, error
        assert launcher.is_symlink()  # first repair missed this second replacement
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    units = home / ".config/systemd/user"
    timer = (units / "switchstand-codex-shim.timer").read_text()
    assert "OnStartupSec=5min" in timer
    assert "OnUnitInactiveSec=5min" in timer
    assert "Unit=switchstand-codex-shim.service" in timer
    assert "WantedBy=timers.target" in timer
    assert (
        "enable --now switchstand-codex-shim.path switchstand-codex-shim.timer"
        in (home / "systemctl-calls").read_text()
    )
    # Invoke the configured periodic service action without activating live units.
    subprocess.run([repair, "--repair-only"], env=env, check=True, timeout=5)
    assert not launcher.is_symlink()
    assert launcher.read_bytes() == original
    restored = launcher.stat()
    subprocess.run([repair, "--repair-only"], env=env, check=True, timeout=5)
    assert launcher.stat().st_ino == restored.st_ino
    assert launcher.stat().st_mtime_ns == restored.st_mtime_ns
    assert real.read_text() == "#!/bin/sh\nexit 0\n"
