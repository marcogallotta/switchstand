from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).parents[1]


def run_shard(root: Path, index: int, count: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, ROOT / "scripts/pytest-shard", "--index", str(index),
         "--count", str(count), "--membership", root / f"shard-{index}.json", "--",
         "-q", "--junitxml", root / f"shard-{index}.xml"],
        cwd=root, env=os.environ | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True, text=True, timeout=30, check=False,
    )


def cases(path: Path) -> set[tuple[str | None, str | None]]:
    return {(case.get("classname"), case.get("name"))
            for case in ElementTree.parse(path).iter("testcase")}


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
