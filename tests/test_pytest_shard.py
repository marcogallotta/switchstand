from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).parents[1]


def run_shard(
    root: Path, index: int, count: int, pytest_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, ROOT / "scripts/pytest-shard", "--index", str(index),
         "--count", str(count), "--membership", root / f"shard-{index}.json", "--",
         "-q", "--junitxml", root / f"shard-{index}.xml", *pytest_args],
        cwd=root, env=os.environ | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True, text=True, timeout=30, check=False,
    )


def cases(path: Path) -> set[tuple[str | None, str | None]]:
    return {(case.get("classname"), case.get("name"))
            for case in ElementTree.parse(path).iter("testcase")}


def test_duration_partition_balances_cost():
    assign = runpy.run_path(str(ROOT / "scripts/pytest-shard"))["module_shards"]
    weights = json.loads((ROOT / "scripts/pytest-shard-weights.json").read_text())
    modules = set(weights["module_seconds"]) | {"new/test_unknown.py"}
    assignments = assign(modules, 4)
    assert set(assignments) == modules
    assert set(assignments.values()) == set(range(4))
    loads = [0.0, 0.0, 0.0, weights["static_seconds"]]
    for module, shard in assignments.items():
        loads[shard] += weights["module_seconds"].get(module, weights["default_seconds"])
    assert max(loads) - min(loads) < 2
    assert set(assign(modules, 1).values()) == {0}


def test_unknown_modules_use_all_shards_and_stable_default():
    assign = runpy.run_path(str(ROOT / "scripts/pytest-shard"))["module_shards"]
    modules = {f"new/test_{index}.py" for index in range(17)}
    assignments = assign(modules, 4)
    assert set(assignments) == modules
    assert set(assignments.values()) == set(range(4))


def test_real_shards_preserve_collection_union_and_whole_modules(tmp_path: Path):
    for index in range(16):
        (tmp_path / f"test_case_{index}.py").write_text(
            "import pytest\n@pytest.mark.parametrize('value', [0, 1])\n"
            "def test_case(value): assert value >= 0\n"
        )
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/discovery_test.py").write_text("def test_nested(): pass\n")
    baseline = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--junitxml", tmp_path / "full.xml"],
        cwd=tmp_path, env=os.environ | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    seen_cases: set[tuple[str | None, str | None]] = set()
    seen_nodes: set[str] = set()
    seen_modules: set[str] = set()
    collected: list[str] | None = None
    for index in range(4):
        result = run_shard(tmp_path, index, 4)
        assert result.returncode == 0, result.stdout + result.stderr
        membership = json.loads((tmp_path / f"shard-{index}.json").read_text())
        assert (membership["index"], membership["count"]) == (index, 4)
        if collected is None:
            collected = membership["collected_nodeids"]
        assert membership["collected_nodeids"] == collected
        assigned = set(membership["assigned_nodeids"])
        modules = set(membership["modules"])
        assert assigned and seen_nodes.isdisjoint(assigned) and seen_modules.isdisjoint(modules)
        assert modules == {node.split("::")[0] for node in assigned}
        seen_nodes.update(assigned)
        seen_modules.update(modules)
        shard_cases = cases(tmp_path / f"shard-{index}.xml")
        assert seen_cases.isdisjoint(shard_cases)
        seen_cases.update(shard_cases)
    assert seen_nodes == set(collected or [])
    assert seen_cases == cases(tmp_path / "full.xml")
    assert "nested/discovery_test.py::test_nested" in seen_nodes
    original = (tmp_path / "shard-0.json").read_text()
    assert run_shard(tmp_path, 0, 4).returncode == 0
    assert (tmp_path / "shard-0.json").read_text() == original
    reversed_files = tuple(sorted(
        (str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*.py")), reverse=True,
    ))
    result = run_shard(tmp_path, 0, 4, reversed_files)
    assert result.returncode == 0, result.stdout + result.stderr
    reordered = json.loads((tmp_path / "shard-0.json").read_text())
    assert reordered["collected_nodeids"] != collected
    assert set(reordered["collected_nodeids"]) == set(collected or [])
    assert set(reordered["assigned_nodeids"]) == set(json.loads(original)["assigned_nodeids"])


def test_selected_single_shard_keeps_exact_subset(tmp_path: Path):
    (tmp_path / "test_selected.py").write_text("def test_selected(): pass\n")
    (tmp_path / "test_other.py").write_text("def test_other(): assert False\n")
    result = run_shard(tmp_path, 0, 1, ("test_selected.py",))
    assert result.returncode == 0, result.stdout + result.stderr
    membership = json.loads((tmp_path / "shard-0.json").read_text())
    assert membership["assigned_nodeids"] == ["test_selected.py::test_selected"]
    assert membership["collected_nodeids"] == membership["assigned_nodeids"]


def test_membership_write_failure_fails_closed(tmp_path: Path):
    (tmp_path / "test_case.py").write_text("def test_case(): pass\n")
    (tmp_path / "shard-0.json").mkdir()
    assert run_shard(tmp_path, 0, 1).returncode != 0


@pytest.mark.parametrize(("content", "index", "count", "expected"), [
    ("def test_fail(): assert False\n", 0, 1, 1),
    ("syntax error\n", 0, 1, 2),
    ("", 0, 1, 5),
    ("def test_ok(): pass\n", 0, 0, 2),
    ("def test_ok(): pass\n", -1, 4, 2),
    ("def test_ok(): pass\n", 4, 4, 2),
])
def test_shards_fail_closed(tmp_path: Path, content: str, index: int, count: int, expected: int):
    (tmp_path / "test_boundary.py").write_text(content)
    assert run_shard(tmp_path, index, count).returncode == expected


@pytest.mark.parametrize(("mode", "plan", "execution", "docker", "success"), [
    ("FULL_FALLBACK", "success", "success", "success", True),
    ("PROMOTE_TEST_MODULE_ONLY_V1", "success", "success", "skipped", True),
    ("FULL_FALLBACK", "failure", "success", "success", False),
    *[("FULL_FALLBACK", "success", outcome, "success", False)
      for outcome in ("failure", "cancelled", "skipped", "")],
    ("FULL_FALLBACK", "success", "success", "skipped", False),
    ("PROMOTE_TEST_MODULE_ONLY_V1", "success", "success", "success", False),
    ("UNKNOWN", "success", "success", "success", False),
])
def test_terminal_requires_successful_matrix_and_mode_gates(
    mode: str, plan: str, execution: str, docker: str, success: bool,
):
    workflow = (ROOT / ".github/workflows/quality.yml").read_text()
    terminal = workflow.split("      - name: Enforce fail-closed terminal Quality policy")[1]
    script = textwrap.dedent(terminal.split("        run: |\n", 1)[1])
    result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, check=False,
                            env=os.environ | {"MODE": mode, "PLAN_RESULT": plan,
                                              "QUALITY_RESULT": execution, "DOCKER_RESULT": docker,
                                              "REASONS": "[]"})
    assert (result.returncode == 0) is success
