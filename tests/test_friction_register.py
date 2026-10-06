from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
REGISTER = ROOT / "scripts/friction-register"


def run(*args: str, cwd: Path, body: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [REGISTER, *args], cwd=cwd, input=body, text=True, capture_output=True,
        check=False,
    )


def body(attempted: str = "claim") -> str:
    return (
        f"- Attempted: {attempted}\n"
        "- Observed: result\n"
        "- State-change truth: none\n"
        "- Clearing action: fix owner\n"
    )


def initialized(tmp_path: Path, index: str = "") -> Path:
    state = tmp_path / "state"
    state.mkdir()
    if index:
        (state / "friction.md").write_text(index)
    result = run("init", str(state), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    (tmp_path / "friction.md").symlink_to(state / "friction.md")
    return state


def test_repository_ignores_friction_directory_symlink(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    subprocess.run(
        ["git", "-C", repository, "init", "-b", "main"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", repository, "add", ".gitignore"],
        check=True, capture_output=True,
    )
    target = tmp_path / "friction-state"
    target.mkdir()
    (repository / "friction").symlink_to(target, target_is_directory=True)

    assert subprocess.check_output(
        ["git", "-C", repository, "status", "--porcelain"], text=True,
    ) == "A  .gitignore\n"


def test_init_preserves_existing_index_and_creates_bounded_register(tmp_path: Path) -> None:
    state = initialized(tmp_path, "legacy evidence\n")

    assert (state / "friction.md").read_text() == "legacy evidence\n"
    for category in ("capability", "process", "implementation"):
        assert (state / f"{category}.md").read_text().startswith("# ")
    assert run("migrate-index", cwd=tmp_path).returncode == 0
    short_index = (state / "friction.md").read_text()
    assert ((state / "legacy-index.md").read_text() == "legacy evidence\n"
            and short_index.startswith("# Friction register\n"))
    assert (run("migrate-index", cwd=tmp_path).returncode == 0
            and (state / "friction.md").read_text() == short_index)


def test_append_routes_explicitly_and_dedupes_key_globally(tmp_path: Path) -> None:
    state = initialized(tmp_path)

    appended = run("append", "capability", "20261006-tool-gap", cwd=tmp_path, body=body())
    assert appended.returncode == 0, appended.stderr
    assert ("### 20261006-tool-gap" in (state / "capability.md").read_text()
            and "### 20261006-tool-gap" not in (state / "process.md").read_text())
    assert run("has", "20261006-tool-gap", cwd=tmp_path).stdout == "capability\n"

    duplicate = run(
        "append", "implementation", "20261006-tool-gap", cwd=tmp_path,
        body=body("different claim"),
    )
    assert duplicate.returncode == 3 and "already exists in capability" in duplicate.stderr
    assert (state / "capability.md").read_text().count("### 20261006-tool-gap") == 1


def test_append_enforces_unicode_and_nonblank_bounds(tmp_path: Path) -> None:
    initialized(tmp_path)
    key = "unicode-bound"
    base = body("")
    padding = 800 - len(f"### {key}\n{base.strip()}\n")
    exact = body("🙂" * padding)
    accepted = run("append", "process", key, cwd=tmp_path, body=exact)
    assert accepted.returncode == 0, accepted.stderr
    over_key = "unicode-over"
    over_base = body("")
    over_padding = 801 - len(f"### {over_key}\n{over_base.strip()}\n")
    too_long = run("append", "process", over_key, cwd=tmp_path,
                   body=body("🙂" * over_padding))
    assert too_long.returncode == 2 and "800 Unicode" in too_long.stderr

    eight_lines = body() + (
        "- Deferred because: not selected\n"
        "\n"
        "- Evidence: work:123\n"
        "- Note: retained\n"
    )
    assert run("append", "implementation", "eight-lines", cwd=tmp_path,
               body=eight_lines).returncode == 0
    nine_lines = eight_lines + "- Extra: rejected\n"
    rejected = run("append", "implementation", "nine-lines", cwd=tmp_path,
                   body=nine_lines)
    assert rejected.returncode == 2 and "eight nonblank" in rejected.stderr


def test_append_requires_complete_deferred_evidence_and_valid_key(tmp_path: Path) -> None:
    initialized(tmp_path)
    missing_evidence = run(
        "append", "process", "deferred", cwd=tmp_path,
        body=body() + "- Deferred because: not selected\n",
    )
    assert (missing_evidence.returncode == 2
            and "Deferred because and one Evidence" in missing_evidence.stderr)
    assert run("has", "deferred", cwd=tmp_path).returncode == 1

    invalid = run("append", "process", "../UPPER", cwd=tmp_path, body=body())
    assert invalid.returncode == 2 and "key must be" in invalid.stderr
    for injected in ("### shadow-key", "### invalid key"):
        poisoned = run("append", "process", "poison", cwd=tmp_path,
                       body=body() + injected + "\n")
        assert poisoned.returncode == 2 and "key heading" in poisoned.stderr
    assert run("has", "shadow-key", cwd=tmp_path).returncode == 1


def test_concurrent_same_key_append_converges_once(tmp_path: Path) -> None:
    state = initialized(tmp_path)
    command = [REGISTER, "append", "capability", "concurrent-key"]
    first = subprocess.Popen(
        command, cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    second = subprocess.Popen(
        command, cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    results = [first.communicate(body()), second.communicate(body())]

    assert sorted(process.returncode for process in (first, second)) == [0, 3], results
    assert (state / "capability.md").read_text().count("### concurrent-key") == 1
