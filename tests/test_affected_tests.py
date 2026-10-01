from __future__ import annotations

import json
import subprocess
from pathlib import Path

from _pytest.capture import CaptureFixture

from switchstand.affected_tests import main, plan_exact, plan_local


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q")
    write(tmp_path, "src/switchstand/alpha.py", "VALUE = 1\n")
    write(
        tmp_path,
        "tests/test_alpha.py",
        "import switchstand.alpha\nfrom tests import helpers\n\ndef test_value(): assert True\n",
    )
    write(tmp_path, "tests/test_unrelated.py", "def test_other(): assert True\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def commit(repo: Path, message: str = "change") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def test_exact_plan_uses_supplied_head_tree_not_checkout(tmp_path: Path) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "src/switchstand/alpha.py", "VALUE = 2\n")
    head = commit(repo)
    write(repo, "tests/test_alpha.py", "this is not valid Python\n")

    plan = plan_exact(repo, base, head)

    assert plan.mode == "SELECTED"
    assert plan.base == base
    assert plan.head == head
    assert plan.selected_tests == ("tests/test_alpha.py",)
    assert plan.planner_revision == "affected-tests-v1"
    write(
        repo,
        "tests/test_alpha.py",
        "import switchstand.alpha\nfrom tests import helpers\n\ndef test_value(): assert True\n",
    )
    write(repo, "src/switchstand/alpha.py", "VALUE = 3\n")
    local = plan_local(repo, head)
    assert local.basis_kind == "local"
    assert local.head.startswith(f"worktree:{head}:")


def test_changed_helper_selects_transitive_importer(tmp_path: Path) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "tests/helpers.py", "VALUE = 2\n")
    head = commit(repo)

    plan = plan_exact(repo, base, head)

    assert plan.mode == "SELECTED"
    assert plan.selected_tests == ("tests/test_alpha.py",)
    write(
        repo,
        "tests/test_dynamic.py",
        'import importlib\nNAME = "switchstand.alpha"\nimportlib.import_module(NAME)\n',
    )
    dynamic_base = commit(repo, "dynamic consumer")
    write(repo, "src/switchstand/alpha.py", "VALUE = 3\n")
    dynamic = plan_exact(repo, dynamic_base, commit(repo))
    assert dynamic.mode == "FULL_FALLBACK"


def test_same_name_is_extra_edge_but_unresolved_source_falls_back(tmp_path: Path) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "src/switchstand/helper.py", "VALUE = 2\n")
    head = commit(repo)

    plan = plan_exact(repo, base, head)

    assert plan.mode == "FULL_FALLBACK"
    assert any("no resolved tests" in reason for reason in plan.fallback_reasons)
    assert plan.selected_tests == ("tests/test_alpha.py", "tests/test_unrelated.py")


def test_migration_compose_and_deleted_python_fail_to_full_suite(tmp_path: Path) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "migrations/0001.py", "revision = '1'\n")
    write(repo, "compose.yaml", "services: {}\n")
    (repo / "src/switchstand/alpha.py").unlink()
    head = commit(repo)

    plan = plan_exact(repo, base, head)

    assert plan.mode == "FULL_FALLBACK"
    assert "fallback-class:migrations/0001.py" in plan.fallback_reasons
    assert "fallback-class:compose.yaml" in plan.fallback_reasons
    assert any("deleted or renamed" in reason for reason in plan.fallback_reasons)
    write(repo, "src/switchstand/chatgpt_edge.py", "VALUE = 1\n")
    runtime = plan_exact(repo, head, commit(repo, "runtime"))
    assert runtime.fallback_reasons == (
        "changed Python has no resolved tests:src/switchstand/chatgpt_edge.py",
        "explicit fallback:src/switchstand/chatgpt_edge.py",
    )


def test_unknown_behavioral_input_and_parse_error_fail_closed(tmp_path: Path) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "src/data/schema.json", "{}")
    write(repo, "tests/test_unrelated.py", "not valid Python\n")
    head = commit(repo)

    plan = plan_exact(repo, base, head)

    assert plan.mode == "FULL_FALLBACK"
    assert any("unknown behavioral" in reason for reason in plan.fallback_reasons)
    assert any("unreadable dependency graph" in reason for reason in plan.fallback_reasons)


def test_cli_json_binds_basis_and_revision(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "src/switchstand/alpha.py", "VALUE = 2\n")
    head = commit(repo)

    main(["--repo", str(repo), "--base", base, "--head", head, "--json"])
    output = json.loads(capsys.readouterr().out)

    assert output["basis_kind"] == "exact"
    assert output["base"] == base
    assert output["head"] == head
    assert output["planner_revision"] == "affected-tests-v1"
    write(repo, "README.md", "documentation only\n")
    docs = plan_exact(repo, head, commit(repo, "docs"))
    assert docs.mode == "FULL_FALLBACK"
    assert docs.selected_tests == ("tests/test_alpha.py", "tests/test_unrelated.py")
    missing = plan_exact(repo, base, "0" * 40)
    assert missing.mode == "NO_PLAN"
    assert missing.selected_tests == ()
