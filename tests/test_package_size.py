import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "package-size.py"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def write_lines(path: Path, count: int, prefix: str = "line") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{prefix}-{index}\n" for index in range(count)))


def repo(tmp_path: Path) -> tuple[Path, str]:
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    write_lines(work / "src" / "app.py", 10)
    write_lines(work / "tests" / "test_app.py", 10)
    (work / "src" / "asset.bin").write_bytes(b"\x00binary\xff")
    (work / "README.md").write_text("base\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "base")
    return work, git(work, "rev-parse", "HEAD")


def report(
    work: Path,
    base: str,
    *,
    production_forecast: int = 100,
    support_forecast: int = 100,
    production_cap: int = 400,
    support_cap: int = 400,
    total_cap: int = 800,
) -> tuple[int, dict]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--repo",
        str(work),
        "--base",
        base,
        "--head",
        "HEAD",
        "--package",
        "src/**",
        "--package",
        "tests/**",
        "--support",
        "tests/**",
        "--production-forecast",
        str(production_forecast),
        "--support-forecast",
        str(support_forecast),
        "--production-cap",
        str(production_cap),
        "--support-cap",
        str(support_cap),
        "--total-cap",
        str(total_cap),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    return result.returncode, json.loads(result.stdout)


def test_forecast_trigger_requires_ratio_and_100_line_floor(tmp_path):
    work, base = repo(tmp_path)
    with (work / "src" / "app.py").open("a") as handle:
        handle.writelines(f"added-{index}\n" for index in range(220))
    git(work, "add", ".")
    git(work, "commit", "-qm", "production growth")

    code, payload = report(work, base, production_forecast=120)
    assert code == 0
    assert payload["production"]["actual"] == 220
    assert payload["production"]["forecast_miss_trigger"] is True
    assert payload["production"]["hard_cap_breached"] is False

    _, payload = report(work, base, production_forecast=140)
    assert payload["production"]["forecast_miss_trigger"] is False

    _, payload = report(
        work,
        base,
        production_forecast=200,
        production_cap=210,
    )
    assert payload["production"]["forecast_miss_trigger"] is False
    assert payload["production"]["hard_cap_breached"] is True


def test_production_and_support_triggers_are_separate(tmp_path):
    work, base = repo(tmp_path)
    with (work / "tests" / "test_app.py").open("a") as handle:
        handle.writelines(f"support-{index}\n" for index in range(220))
    git(work, "add", ".")
    git(work, "commit", "-qm", "support growth")

    code, payload = report(work, base, support_forecast=120)
    assert code == 0
    assert payload["production"]["forecast_miss_trigger"] is False
    assert payload["support"]["forecast_miss_trigger"] is True
    assert payload["total"]["forecast_miss_trigger"] is True


def test_rename_and_delete_use_retained_fixed_base(tmp_path):
    work, base = repo(tmp_path)
    git(work, "mv", "src/app.py", "src/renamed.py")
    with (work / "src" / "renamed.py").open("a") as handle:
        handle.write("renamed-change\n")
    (work / "tests" / "test_app.py").unlink()
    git(work, "add", "-A")
    git(work, "commit", "-qm", "combined retained result")

    code, payload = report(work, base)
    assert code == 0
    assert payload["status"] == "OK"
    assert payload["production"]["actual"] == 1
    assert payload["support"]["actual"] == 10


def test_unmatched_changed_path_fails_closed_without_cap_result(tmp_path):
    work, base = repo(tmp_path)
    (work / "README.md").write_text("uncovered\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "uncovered change")

    code, payload = report(work, base)
    assert code == 2
    assert payload["status"] == "UNKNOWN"
    assert "path outside package coverage" in payload["unknown"][0]
    assert "total" not in payload


def test_stacked_commits_are_counted_once_from_original_base(tmp_path):
    work, base = repo(tmp_path)
    with (work / "src" / "app.py").open("a") as handle:
        handle.writelines(f"first-{index}\n" for index in range(30))
    git(work, "add", ".")
    git(work, "commit", "-qm", "first slice")
    with (work / "tests" / "test_app.py").open("a") as handle:
        handle.writelines(f"second-{index}\n" for index in range(40))
    git(work, "add", ".")
    git(work, "commit", "-qm", "second slice")

    _, payload = report(work, base)
    assert payload["production"]["actual"] == 30
    assert payload["support"]["actual"] == 40
    assert payload["total"]["actual"] == 70


def test_rename_across_package_scope_fails_closed(tmp_path):
    work, base = repo(tmp_path)
    git(work, "mv", "src/app.py", "outside.py")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "cross boundary")

    code, payload = report(work, base)
    assert code == 2
    assert payload["status"] == "UNKNOWN"
    assert "rename crosses package scope" in payload["unknown"][0]


def test_nonancestor_fails_closed(tmp_path):
    work, base = repo(tmp_path)
    git(work, "checkout", "--orphan", "unrelated")
    git(work, "rm", "-qrf", ".")
    (work / "other.txt").write_text("unrelated root\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "unrelated root")

    code, payload = report(work, base)
    assert code == 2
    assert payload == {"error": "base is not an ancestor of head", "status": "UNKNOWN"}


def test_binary_content_change_fails_closed(tmp_path):
    work, base = repo(tmp_path)
    (work / "src" / "added.bin").write_bytes(b"\x00new binary")
    git(work, "add", ".")
    git(work, "commit", "-qm", "binary addition")

    code, payload = report(work, base)
    assert code == 2
    assert "binary content or mode change" in payload["unknown"][0]
    assert "total" not in payload


def test_unchanged_binary_rename_is_reported_at_zero_gross(tmp_path):
    work, base = repo(tmp_path)
    git(work, "mv", "src/asset.bin", "src/renamed.bin")
    git(work, "commit", "-qm", "binary rename")

    code, payload = report(work, base)
    assert code == 0
    assert payload["total"]["actual"] == 0
    assert payload["binary"]["unchanged_renames"] == [
        {
            "category": "production",
            "gross": 0,
            "kind": "unchanged_binary_rename",
            "new": "src/renamed.bin",
            "old": "src/asset.bin",
        }
    ]


def test_binary_rename_with_mode_change_fails_closed(tmp_path):
    work, base = repo(tmp_path)
    git(work, "mv", "src/asset.bin", "src/renamed.bin")
    git(work, "update-index", "--chmod=+x", "src/renamed.bin")
    git(work, "commit", "-qm", "binary rename and mode change")

    code, payload = report(work, base)
    assert code == 2
    assert "binary content or mode change" in payload["unknown"][0]
    assert "total" not in payload


def test_report_is_stateless_and_reproducible(tmp_path):
    work, base = repo(tmp_path)
    with (work / "src" / "app.py").open("a") as handle:
        handle.write("change\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "one change")

    first_code, first = report(work, base)
    second_code, second = report(work, base)

    assert first_code == second_code == 0
    assert first == second
    assert not list(work.rglob("*ledger*"))
