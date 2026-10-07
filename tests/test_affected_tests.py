from __future__ import annotations

import json
import subprocess
from pathlib import Path

from _pytest.capture import CaptureFixture

from switchstand.affected_tests import (
    BroadQualityRun,
    Plan,
    foreground_authority,
    main,
    plan_exact,
    plan_local,
    selector_health_clear,
)


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


def test_selected_test_absent_from_execution_tree_falls_back(
    tmp_path: Path, capsys: CaptureFixture[str],
) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "src/switchstand/alpha.py", "VALUE = 2\n")
    head = commit(repo, "candidate")
    git(repo, "checkout", "-q", base)
    (repo / "tests/test_alpha.py").unlink()
    execution_tree = commit(repo, "target deleted selected test")

    main([
        "--repo", str(repo), "--base", base, "--head", head,
        "--execution-tree", execution_tree, "--json",
    ])
    plan = json.loads(capsys.readouterr().out)

    assert plan["mode"] == "FULL_FALLBACK"
    assert plan["selected_tests"] == ["tests/test_unrelated.py"]
    assert plan["fallback_reasons"] == [
        "selected test absent from execution tree:tests/test_alpha.py"
    ]


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
    assert plan.destructive
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


def test_shadow_push_identity_rejects_forced_or_non_ancestor_bases() -> None:
    workflow = Path(".github/workflows/affected-test-shadow.yml").read_text()

    assert "PUSH_FORCED: ${{ github.event.forced }}" in workflow
    assert 'test "$PUSH_FORCED" = true' in workflow
    assert 'git merge-base --is-ancestor "$planner_base_sha" "$planner_head_sha"' in workflow
    assert "identity_mode=NO_PLAN" in workflow
    assert "NO_PLAN_FORCED_PUSH" in workflow
    assert "NO_PLAN_UNAVAILABLE_OR_NON_ANCESTOR_PUSH_BASE" in workflow
    assert 'else os.environ["IDENTITY_MODE"]' in workflow
    assert "Build planner and selected-test development image" in workflow
    assert "/app/.venv/bin/python -c 'from switchstand.affected_tests import main; main()'" in workflow
    assert "--repo /workspace" in workflow
    assert '--execution-tree "$EXECUTION_SHA"' in workflow
    assert "GIT_CONFIG_KEY_0=safe.directory" in workflow
    assert "GIT_CONFIG_VALUE_0=/workspace" in workflow


def test_only_direct_test_modules_can_receive_foreground_authority(tmp_path: Path) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "tests/test_alpha.py", "def test_value(): assert True\n")
    plan = plan_exact(repo, base, commit(repo))

    promoted = foreground_authority(
        plan, subject_verified=True, selector_health_clear=True
    )
    assert promoted.mode == "PROMOTE_TEST_MODULE_ONLY_V1"
    assert promoted.reasons == ()

    assert foreground_authority(
        plan, subject_verified=False, selector_health_clear=True
    ).mode == "FULL_FALLBACK"
    assert foreground_authority(
        plan, subject_verified=True, selector_health_clear=False
    ).mode == "FULL_FALLBACK"
    assert foreground_authority(
        plan,
        subject_verified=True,
        selector_health_clear=True,
        cumulative_stack_top=True,
    ).mode == "FULL_FALLBACK"
    backstop = foreground_authority(
        plan,
        subject_verified=True,
        selector_health_clear=True,
        broad_backstop_due=True,
    )
    assert backstop.mode == "FULL_FALLBACK"
    assert backstop.reasons == ("broad-backstop-requires-full",)


def test_serial_sensitive_modules_always_fall_back_on_changed_and_selected_paths() -> None:
    selected_sensitive = Plan(
        "SELECTED", "exact", "a" * 40, "b" * 40,
        ("tests/test_alpha.py",),
        ("tests/test_alpha.py", "tests/test_update_gateway.py"),
        {}, (),
    )
    selected = foreground_authority(
        selected_sensitive, subject_verified=True, selector_health_clear=True
    )
    assert selected.mode == "FULL_FALLBACK"
    assert selected.reasons == ("serial-sensitive-selected-path",)

    changed_sensitive = Plan(
        "SELECTED", "exact", "a" * 40, "b" * 40,
        ("tests/test_chatgpt_edge_process.py",),
        ("tests/test_chatgpt_edge_process.py",),
        {}, (),
    )
    changed = foreground_authority(
        changed_sensitive, subject_verified=True, selector_health_clear=True
    )
    assert changed.mode == "FULL_FALLBACK"
    assert changed.reasons == (
        "serial-sensitive-changed-path",
        "serial-sensitive-selected-path",
    )


def test_direct_test_change_does_not_parse_unrelated_dependency_graph(
    tmp_path: Path,
) -> None:
    repo, _ = fixture_repo(tmp_path)
    write(repo, "src/switchstand/unrelated.py", "this is not valid Python\n")
    base = commit(repo, "unrelated parser-incompatible source")
    write(repo, "tests/test_alpha.py", "def test_value(): assert True\n")

    plan = plan_exact(repo, base, commit(repo))

    assert plan.mode == "SELECTED"
    assert plan.selected_tests == ("tests/test_alpha.py",)
    assert plan.fallback_reasons == ()


def test_test_helpers_nested_modules_and_deletes_are_not_promoted(tmp_path: Path) -> None:
    repo, base = fixture_repo(tmp_path)
    write(repo, "tests/helpers.py", "VALUE = 2\n")
    helper = plan_exact(repo, base, commit(repo, "helper"))
    assert foreground_authority(
        helper, subject_verified=True, selector_health_clear=True
    ).mode == "FULL_FALLBACK"

    nested_base = helper.head
    write(repo, "tests/nested/test_extra.py", "def test_extra(): assert True\n")
    nested = plan_exact(repo, nested_base, commit(repo, "nested"))
    assert foreground_authority(
        nested, subject_verified=True, selector_health_clear=True
    ).mode == "FULL_FALLBACK"

    delete_base = nested.head
    (repo / "tests/test_alpha.py").unlink()
    deleted = plan_exact(repo, delete_base, commit(repo, "delete"))
    assert deleted.destructive
    assert foreground_authority(
        deleted, subject_verified=True, selector_health_clear=True
    ).mode == "FULL_FALLBACK"


def test_selector_health_requires_current_default_green_and_no_generation_failure() -> None:
    green = BroadQualityRun("push", "a" * 40, "success", "current")

    def healthy(*runs: BroadQualityRun, complete: bool = True) -> bool:
        return selector_health_clear(
            history_complete=complete,
            current_workflow_blob="current",
            default_sha="a" * 40,
            runs=runs,
        )

    assert healthy(green, BroadQualityRun("schedule", "a" * 40, "success", "current"))
    unhealthy = (
        (BroadQualityRun("push", "b" * 40, "success", "current"),),
        (green, BroadQualityRun("schedule", "a" * 40, "failure", "current")),
        (green, BroadQualityRun("push", "b" * 40, "cancelled", "current")),
        (green, BroadQualityRun("schedule", "b" * 40, "failure", None)),
    )
    assert all(not healthy(*runs) for runs in unhealthy)
    assert healthy(green, BroadQualityRun("push", "b" * 40, "failure", "old"))
    assert not healthy(green, complete=False)
