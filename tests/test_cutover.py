import copy
import json
import sys
from pathlib import Path

import pytest

from switchstand.cutover import (
    IMPLEMENTATION_OWNER,
    REQUIRED_CATALOG,
    UNCONDITIONAL,
    blockers,
    entries,
    load,
    main,
)

MANIFEST = Path(__file__).parents[1] / "docs/cutover-asana-coverage.json"


def raw_manifest():
    value = json.loads(MANIFEST.read_text())
    assert isinstance(value, dict)
    return value


def terminal_manifest():
    manifest = raw_manifest()
    for group, required in REQUIRED_CATALOG.items():
        for identity in required:
            if identity in UNCONDITIONAL[group]:
                manifest["status"][group][identity] = "COVERED"
                manifest["evidence"][group][identity] = {
                    "current_use_evidence": "test current workflow evidence",
                    "mcp_surface": "test semantic surface",
                    "positive_test": "test positive receipt",
                    "negative_test": "test negative receipt",
                    "evidence": "test merged qualification receipt",
                }
            else:
                manifest["status"][group][identity] = "NOT_REQUIRED"
                manifest["evidence"][group][identity] = {
                    "current_use_evidence": "test bounded exclusion audit",
                    "evidence": "test exclusion receipt",
                }
    return manifest


def write_manifest(tmp_path, value):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(value))
    return path


def test_manifest_exactly_matches_production_catalogue_and_blocks_now():
    manifest = load(MANIFEST)
    rows = entries(manifest)
    assert len(rows) == sum(len(group) for group in REQUIRED_CATALOG.values())
    pending = blockers(manifest)
    assert "connector_operations:search_tasks:GAP" in pending
    assert "non_tool_dependencies:agent_messaging:GAP" in pending
    assert "non_tool_dependencies:mcp_only_real_chatgpt:GAP" in pending


@pytest.mark.parametrize("mutation", ["missing", "extra", "empty", "extra_group"])
def test_catalogue_shape_cannot_be_weakened(mutation):
    manifest = raw_manifest()
    if mutation == "missing":
        del manifest["status"]["connector_operations"]["search_tasks"]
    elif mutation == "extra":
        manifest["status"]["connector_operations"]["invented"] = "COVERED"
    elif mutation == "empty":
        manifest["status"]["semantic_fields"] = {}
    else:
        manifest["status"]["extra_group"] = {}
    with pytest.raises(ValueError):
        entries(manifest)


def test_catalogue_and_required_sets_are_deeply_immutable():
    with pytest.raises(TypeError):
        REQUIRED_CATALOG["connector_operations"]["search_tasks"] = "changed"
    with pytest.raises(TypeError):
        UNCONDITIONAL["connector_operations"] = frozenset()


def test_manifest_owner_is_exact_reviewed_package():
    manifest = raw_manifest()
    assert manifest["implementation_owner"] == IMPLEMENTATION_OWNER
    manifest["implementation_owner"] = "different-owner"
    with pytest.raises(ValueError, match="implementation_owner"):
        entries(manifest)


@pytest.mark.parametrize("status", ["COVERED", "NOT_REQUIRED"])
def test_terminal_status_requires_evidence_or_required_coverage(status):
    manifest = raw_manifest()
    manifest["status"]["connector_operations"]["search_tasks"] = status
    with pytest.raises(ValueError):
        entries(manifest)


def test_unconditional_rows_can_never_be_not_required():
    manifest = terminal_manifest()
    manifest["status"]["non_tool_dependencies"]["agent_messaging"] = "NOT_REQUIRED"
    manifest["evidence"]["non_tool_dependencies"]["agent_messaging"] = {
        "current_use_evidence": "fake exclusion",
        "evidence": "fake exclusion receipt",
    }
    with pytest.raises(ValueError, match="unconditional"):
        entries(manifest)


def test_all_not_required_can_never_reach_ready():
    manifest = raw_manifest()
    for group, required in REQUIRED_CATALOG.items():
        for identity in required:
            manifest["status"][group][identity] = "NOT_REQUIRED"
            manifest["evidence"][group][identity] = {
                "current_use_evidence": "fake audit",
                "evidence": "fake exclusion",
            }
    with pytest.raises(ValueError, match="unconditional"):
        blockers(manifest)


def test_covered_requires_use_surface_positive_negative_and_receipt_evidence():
    manifest = raw_manifest()
    manifest["status"]["connector_operations"]["search_tasks"] = "COVERED"
    proof = manifest["evidence"]["connector_operations"]["search_tasks"] = {
        "current_use_evidence": "current workflow audit",
        "mcp_surface": "work_search",
        "positive_test": "representative search returns WorkIds",
        "negative_test": "wrong scope denied",
        "evidence": "merged head + qualification receipt",
    }
    entries(manifest)
    for field in tuple(proof):
        broken = copy.deepcopy(manifest)
        del broken["evidence"]["connector_operations"]["search_tasks"][field]
        with pytest.raises(ValueError, match=field):
            entries(broken)


def test_conditional_not_required_requires_current_use_exclusion_evidence():
    manifest = raw_manifest()
    manifest["status"]["connector_operations"]["delete_task"] = "NOT_REQUIRED"
    manifest["evidence"]["connector_operations"]["delete_task"] = {
        "current_use_evidence": "bounded current workflow audit",
        "evidence": "no live consumer in audited routes",
    }
    entries(manifest)
    del manifest["evidence"]["connector_operations"]["delete_task"]["evidence"]
    with pytest.raises(ValueError, match="evidence"):
        entries(manifest)


def test_invalid_types_fail_closed():
    manifest = raw_manifest()
    manifest["status"]["connector_operations"] = []
    with pytest.raises(TypeError):
        entries(manifest)


@pytest.mark.parametrize("kind", ["top", "identity", "evidence"])
def test_raw_duplicate_json_keys_are_rejected(tmp_path, kind):
    if kind == "top":
        raw = MANIFEST.read_text().replace('"status": {', '"status": {}, "status": {', 1)
    elif kind == "identity":
        raw = MANIFEST.read_text().replace(
            '"search_tasks": "GAP"', '"search_tasks": "GAP", "search_tasks": "GAP"', 1
        )
    else:
        raw = json.dumps(terminal_manifest())
        raw = raw.replace(
            '"current_use_evidence": "test current workflow evidence"',
            '"current_use_evidence": "first", "current_use_evidence": "second"',
            1,
        )
    path = tmp_path / f"{kind}.json"
    path.write_text(raw)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load(path)


def test_cli_reports_blocked_invalid_and_ready(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["switchstand.cutover", str(MANIFEST)])
    with pytest.raises(SystemExit) as blocked:
        main()
    assert blocked.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"version": 1, "version": 1}')
    monkeypatch.setattr(sys, "argv", ["switchstand.cutover", str(invalid)])
    with pytest.raises(SystemExit) as rejected:
        main()
    assert rejected.value.code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "INVALID"

    ready = write_manifest(tmp_path, terminal_manifest())
    monkeypatch.setattr(sys, "argv", ["switchstand.cutover", str(ready)])
    main()
    assert json.loads(capsys.readouterr().out) == {"status": "READY"}
