"""Validate the canonical audit register and enforce recovery freeze scope."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from audit_evidence import assert_worker_quarantine, EvidenceError, verify_evidence  # noqa: E402
import audit_register_policy as policy  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH, SCOPE_PATH = Path("audit/findings.json"), Path("audit/change-scope.json")
SCHEMA_VERSION, STATES = 1, {"OPEN", "CONTAINED", "FIXED_AWAITING_VERIFICATION", "CLOSED"}
SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
REACHABILITY = {"REACHABLE", "DISABLED", "REMOVED", "NOT_APPLICABLE"}
RECOVERY_KINDS = {"registration", "containment", "remediation", "revert", "runnable_repair", "gate_repair"}
FINDING_ID = re.compile(r"^[A-Z]+-[0-9]{2,3}$")
GOVERNANCE_PATHS = set(
    ".github/workflows/quality.yml audit/change-scope.json audit/findings.json docs/development.md "
    "scripts/audit_evidence.py scripts/audit_register_policy.py scripts/check_audit_register.py "
    "scripts/quality tests/test_audit_register.py tests/test_audit_register_adversarial.py".split()
)
RECOVERY_METADATA_PATHS = {"audit/change-scope.json", "audit/findings.json", "docs/development.md"}
GATE_REPAIR_PATHS = GOVERNANCE_PATHS - {"audit/findings.json"}
GATE_AUTHORITY_PATHS = GATE_REPAIR_PATHS - {"audit/change-scope.json", "docs/development.md"}


class ValidationError(ValueError):
    """A stable, operator-readable register validation failure."""


def _git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout.strip()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"missing required file: {path.as_posix()}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid JSON in {path.as_posix()}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{path.as_posix()} must contain one JSON object")
    return value


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValidationError(f"{field} must use UTC")
    return parsed


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a nonempty string")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValidationError(f"{field} has wrong keys; missing={missing}, extra={extra}")


def _validate_paths(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(path, str) or not path for path in value):
        raise ValidationError(f"{field} must be a nonempty string array")
    if len(value) != len(set(value)):
        raise ValidationError(f"{field} contains duplicates")
    if any(
        path.startswith(("/", "../"))
        or "/../" in path
        or any(character in path for character in "*?[]")
        for path in value
    ):
        raise ValidationError(f"{field} must contain exact repository-relative paths")
    return value


def _validate_evidence(
    value: object,
    field: str,
    *,
    durable: bool = False,
    role: str | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    try:
        return verify_evidence(
            value,
            field,
            ROOT,
            durable=durable,
            expected_role=role,
            expected_subject=subject,
        )
    except EvidenceError as exc:
        raise ValidationError(str(exc)) from exc


def _validate_finding(finding: object, index: int) -> None:
    field = f"findings[{index}]"
    if not isinstance(finding, dict):
        raise ValidationError(f"{field} must be an object")
    required = {
        "id",
        "title",
        "severity",
        "severity_rationale",
        "category",
        "state",
        "reachability",
        "affected_capability",
        "affected_paths",
        "discovered_at",
        "discovery_sha",
        "deadline",
        "owner",
        "successor",
        "next_action",
        "source_audit",
        "discovery_evidence",
        "containment",
        "fix",
        "closure",
        "severity_change_receipts",
    }
    _exact_keys(finding, required, field)
    for name in (
        "id",
        "title",
        "severity_rationale",
        "category",
        "affected_capability",
        "discovery_sha",
        "owner",
        "successor",
        "next_action",
        "source_audit",
    ):
        _nonempty(finding[name], f"{field}.{name}")
    if not FINDING_ID.fullmatch(finding["id"]):
        raise ValidationError(f"{field}.id has invalid format")
    if finding["severity"] not in SEVERITIES:
        raise ValidationError(f"{field}.severity is invalid")
    if finding["state"] not in STATES:
        raise ValidationError(f"{field}.state is invalid")
    if finding["reachability"] not in REACHABILITY:
        raise ValidationError(f"{field}.reachability is invalid")
    _validate_paths(finding["affected_paths"], f"{field}.affected_paths")
    discovered_at = _parse_time(finding["discovered_at"], f"{field}.discovered_at")
    deadline = _parse_time(finding["deadline"], f"{field}.deadline")
    if deadline < discovered_at:
        raise ValidationError(f"{field}.deadline precedes discovery")
    if finding["severity"] == "CRITICAL" and deadline != discovered_at:
        raise ValidationError(f"{field}.deadline for a Critical must equal discovery time")
    if finding["severity"] == "HIGH" and deadline > discovered_at + timedelta(hours=72):
        raise ValidationError(f"{field}.deadline for a High cannot exceed 72 hours")
    _validate_evidence(
        finding["discovery_evidence"],
        f"{field}.discovery_evidence",
        role="discovery",
        subject=finding["id"],
    )
    receipts = finding["severity_change_receipts"]
    if not isinstance(receipts, list):
        raise ValidationError(f"{field}.severity_change_receipts must be an array")
    for receipt_index, receipt in enumerate(receipts):
        _validate_evidence(
            receipt,
            f"{field}.severity_change_receipts[{receipt_index}]",
            durable=True,
            role="severity_review",
        )
    state = finding["state"]
    if state == "CONTAINED":
        _validate_evidence(
            finding["containment"], f"{field}.containment", durable=True, role="containment", subject=finding["id"]
        )
        if finding["reachability"] not in {"DISABLED", "REMOVED"}:
            raise ValidationError(f"{field} containment requires disabled or removed reachability")
        if finding["fix"] is not None or finding["closure"] is not None:
            raise ValidationError(f"{field} cannot record fix or closure evidence while CONTAINED")
    elif state == "FIXED_AWAITING_VERIFICATION":
        _validate_evidence(finding["fix"], f"{field}.fix", durable=True, role="fix", subject=finding["id"])
        if finding["containment"] is not None:
            _validate_evidence(
                finding["containment"],
                f"{field}.containment",
                durable=True,
                role="containment",
                subject=finding["id"],
            )
        if finding["closure"] is not None:
            raise ValidationError(f"{field}.closure must be null while awaiting verification")
        if finding["severity"] == "CRITICAL":
            _validate_evidence(
                finding["containment"],
                f"{field}.containment",
                durable=True,
                role="containment",
                subject=finding["id"],
            )
            if finding["reachability"] not in {"DISABLED", "REMOVED"}:
                raise ValidationError(f"{field} Critical must stay disabled until CLOSED")
    elif state == "CLOSED":
        _validate_evidence(finding["fix"], f"{field}.fix", durable=True, role="fix", subject=finding["id"])
        closure = finding["closure"]
        if not isinstance(closure, dict):
            raise ValidationError(f"{field}.closure must be an object")
        _exact_keys(closure, {"reproducer", "regression", "ci", "review", "receipt"}, f"{field}.closure")
        role_by_name = {
            "reproducer": "reproducer",
            "regression": "regression",
            "ci": "ci",
            "review": "independent_review",
            "receipt": "closure",
        }
        evidence_identities = []
        evidence_producers = {}
        for name, evidence_role in role_by_name.items():
            evidence = _validate_evidence(
                closure[name],
                f"{field}.closure.{name}",
                durable=True,
                role=evidence_role,
                subject=finding["id"],
            )
            evidence_identities.append((evidence["reference"], evidence["sha256"]))
            evidence_producers[name] = evidence["producer"]
        if len(set(evidence_identities)) != len(evidence_identities):
            raise ValidationError(f"{field}.closure evidence receipts must be distinct")
        if evidence_producers["review"] in {
            finding["fix"]["producer"], evidence_producers["receipt"]
        }:
            raise ValidationError(f"{field}.closure independent review producer is not independent")
    else:
        for name in ("containment", "fix", "closure"):
            if finding[name] is not None:
                raise ValidationError(f"{field}.{name} must be null in OPEN")
        if finding["reachability"] in {"DISABLED", "REMOVED"}:
            raise ValidationError(f"{field} disabled or removed reachability must be CONTAINED")


def validate_register(register: dict[str, Any]) -> list[dict[str, Any]]:
    _exact_keys(
        register,
        {
            "schema_version",
            "findings",
            "audit_runs",
            "runnable_surface_paths",
            "runnable_blockers",
            "capability_protections",
        },
        "register",
    )
    if register["schema_version"] != SCHEMA_VERSION:
        raise ValidationError(f"unsupported register schema_version: {register['schema_version']!r}")
    findings = register["findings"]
    if not isinstance(findings, list):
        raise ValidationError("findings must be an array")
    ids: set[str] = set()
    for index, finding in enumerate(findings):
        _validate_finding(finding, index)
        finding_id = finding["id"]
        if finding_id in ids:
            raise ValidationError(f"duplicate finding id: {finding_id}")
        ids.add(finding_id)
    audit_runs = register["audit_runs"]
    if not isinstance(audit_runs, list):
        raise ValidationError("audit_runs must be an array")
    run_ids = set()
    for index, run in enumerate(audit_runs):
        if not isinstance(run, dict):
            raise ValidationError(f"audit_runs[{index}] must be an object")
        _exact_keys(run, {"id", "task", "status", "reason"}, f"audit_runs[{index}]")
        run_id = _nonempty(run["id"], f"audit_runs[{index}].id")
        if run_id in run_ids:
            raise ValidationError(f"duplicate audit run id: {run_id}")
        run_ids.add(run_id)
        _nonempty(run["task"], f"audit_runs[{index}].task")
        _nonempty(run["reason"], f"audit_runs[{index}].reason")
        if run["status"] not in {"COMPLETE", "NOT_EXECUTED"}:
            raise ValidationError(f"audit_runs[{index}].status is invalid")
    runnable_paths = _validate_paths(register["runnable_surface_paths"], "runnable_surface_paths")
    blockers = register["runnable_blockers"]
    if not isinstance(blockers, list):
        raise ValidationError("runnable_blockers must be an array")
    blocker_ids = set()
    for index, blocker in enumerate(blockers):
        field = f"runnable_blockers[{index}]"
        if not isinstance(blocker, dict):
            raise ValidationError(f"{field} must be an object")
        _exact_keys(blocker, {"id", "title", "status", "affected_paths", "owner", "source_evidence", "closure"}, field)
        blocker_id = _nonempty(blocker["id"], f"{field}.id")
        if blocker_id in blocker_ids:
            raise ValidationError(f"duplicate runnable blocker id: {blocker_id}")
        blocker_ids.add(blocker_id)
        _nonempty(blocker["title"], f"{field}.title")
        _nonempty(blocker["owner"], f"{field}.owner")
        blocker_paths = _validate_paths(blocker["affected_paths"], f"{field}.affected_paths")
        if any(path not in runnable_paths for path in blocker_paths):
            raise ValidationError(f"{field}.affected_paths exceeds runnable_surface_paths")
        _validate_evidence(
            blocker["source_evidence"],
            f"{field}.source_evidence",
            durable=True,
            role="runnable_blocker",
            subject=blocker_id,
        )
        if blocker["status"] == "OPEN" and blocker["closure"] is not None:
            raise ValidationError(f"{field}.closure must be null while OPEN")
        if blocker["status"] == "CLOSED":
            _validate_evidence(
                blocker["closure"],
                f"{field}.closure",
                durable=True,
                role="runnable_blocker_closure",
                subject=blocker_id,
            )
        elif blocker["status"] != "OPEN":
            raise ValidationError(f"{field}.status is invalid")
    protections = register["capability_protections"]
    if not isinstance(protections, list) or not protections:
        raise ValidationError("capability_protections must be a nonempty array")
    protection_ids = set()
    for index, protection in enumerate(protections):
        field = f"capability_protections[{index}]"
        if not isinstance(protection, dict):
            raise ValidationError(f"{field} must be an object")
        _exact_keys(protection, {"capability", "state", "paths", "disabled_assertion"}, field)
        capability = _nonempty(protection["capability"], f"{field}.capability")
        if capability in protection_ids:
            raise ValidationError(f"duplicate capability protection: {capability}")
        protection_ids.add(capability)
        _validate_paths(protection["paths"], f"{field}.paths")
        _nonempty(protection["disabled_assertion"], f"{field}.disabled_assertion")
        if protection["state"] not in {"ENABLED", "DISABLED"}:
            raise ValidationError(f"{field}.state is invalid")
        if protection["state"] == "DISABLED":
            if protection["disabled_assertion"] != "worker_quarantine_v1":
                raise ValidationError(f"{field}.disabled_assertion is unsupported")
            try:
                assert_worker_quarantine(ROOT)
            except EvidenceError as exc:
                raise ValidationError(str(exc)) from exc
    by_capability = {protection["capability"]: protection for protection in protections}
    for finding in findings:
        protection = by_capability.get(finding["affected_capability"])
        if finding["severity"] == "CRITICAL" and finding["state"] in {
            "CONTAINED",
            "FIXED_AWAITING_VERIFICATION",
        }:
            if protection is None or protection["state"] != "DISABLED":
                raise ValidationError(f"{finding['id']} containment lacks a disabled capability assertion")
    return findings


def _load_base_register(base: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["git", "show", f"{base}:{REGISTER_PATH.as_posix()}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("base audit register contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("base audit register must contain one JSON object")
    return value


def _base_revision(argument: str | None) -> str:
    requested = argument or os.environ.get("QUALITY_BASE_REF")
    if requested:
        try:
            return _git("rev-parse", "--verify", f"{requested}^{{commit}}")
        except subprocess.CalledProcessError as exc:
            raise ValidationError(f"base ref is not a commit: {requested}") from exc
    for candidate in ("main", "origin/main"):
        resolved = _git("rev-parse", "--verify", f"{candidate}^{{commit}}", check=False)
        if resolved:
            return resolved
    return _git("rev-parse", "HEAD")


def _changed_paths(base: str) -> set[str]:
    output = _git("diff", "--name-only", base, "--")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    return {line for line in (*output.splitlines(), *untracked.splitlines()) if line and line != "node_modules"}


def validate_scope(
    base: str,
    base_register: dict[str, Any] | None,
    findings: list[dict[str, Any]],
    runnable_paths: list[str],
    runnable_blockers: list[dict[str, Any]],
    reasons: list[str],
    transition_receipts: set[str] | None = None,
) -> None:
    changed = _changed_paths(base)
    if not changed:
        return
    gate_authority_changed = changed & GATE_AUTHORITY_PATHS
    if not reasons and not gate_authority_changed:
        return
    scope = _load_json(ROOT / SCOPE_PATH)
    _exact_keys(
        scope,
        set("schema_version base_sha kind finding_ids runnable_blocker_id gate_repair_receipts rationale".split()),
        "change scope",
    )
    if scope["schema_version"] != SCHEMA_VERSION:
        raise ValidationError("change scope schema_version is unsupported")
    if scope["base_sha"] != base:
        raise ValidationError(f"change scope base_sha must equal exact base {base}")
    if scope["kind"] not in RECOVERY_KINDS:
        raise ValidationError(f"change scope kind must be one of {sorted(RECOVERY_KINDS)}")
    _nonempty(scope["rationale"], "change scope rationale")
    finding_ids = scope["finding_ids"]
    if not isinstance(finding_ids, list) or any(not isinstance(item, str) for item in finding_ids):
        raise ValidationError("change scope finding_ids must be a string array")
    by_id = {finding["id"]: finding for finding in findings}
    unknown = sorted(set(finding_ids) - set(by_id))
    if unknown:
        raise ValidationError(f"change scope cites unknown findings: {unknown}")
    if scope["kind"] in {"containment", "remediation"} and not finding_ids:
        raise ValidationError(f"{scope['kind']} requires at least one finding id")
    runnable_blocker = scope["runnable_blocker_id"]
    if scope["kind"] == "runnable_repair":
        blocker_id = _nonempty(runnable_blocker, "change scope runnable_blocker_id")
        base_blockers = base_register["runnable_blockers"] if base_register is not None else []
        blocker = next((item for item in base_blockers if item["id"] == blocker_id), None)
        if blocker is None or blocker["status"] != "OPEN":
            raise ValidationError("change scope runnable_blocker_id must name one OPEN blocker")
    elif runnable_blocker is not None:
        raise ValidationError("change scope runnable_blocker_id is only valid for runnable_repair")
    if scope["kind"] == "revert" and not finding_ids:
        raise ValidationError("revert requires at least one finding id")
    policy.validate_gate_repair(scope, base, ROOT)
    policy.reserve_gate_authority(scope, base_register is not None, bool(gate_authority_changed))
    allowed_receipts = (transition_receipts or set()) | policy.transition_receipt_paths(scope)
    allowed = RECOVERY_METADATA_PATHS | allowed_receipts
    if scope["kind"] in {"containment", "remediation", "revert"}:
        patterns = [pattern for finding_id in finding_ids for pattern in by_id[finding_id]["affected_paths"]]
        disallowed = sorted(path for path in changed if path not in allowed and path not in patterns)
    elif scope["kind"] == "registration":
        registration_paths = GOVERNANCE_PATHS if base_register is None else RECOVERY_METADATA_PATHS | allowed_receipts
        disallowed = sorted(changed - registration_paths)
    elif scope["kind"] == "runnable_repair":
        assert blocker is not None
        blocker_paths = blocker["affected_paths"]
        if any(path not in runnable_paths for path in blocker_paths):
            raise ValidationError("runnable blocker exceeds the immutable runnable surface")
        disallowed = sorted(path for path in changed if path not in allowed and path not in blocker_paths)
    elif scope["kind"] == "gate_repair":
        disallowed = sorted(changed - GATE_REPAIR_PATHS - allowed_receipts)
    else:
        raise ValidationError(f"unsupported recovery kind: {scope['kind']}")
    if disallowed:
        raise ValidationError(f"changed paths exceed declared {scope['kind']} scope: {disallowed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref")
    parser.add_argument("--now", help="UTC timestamp for deterministic tests; defaults to current UTC")
    arguments = parser.parse_args(argv)
    try:
        now = _parse_time(arguments.now, "--now") if arguments.now else datetime.now(timezone.utc)
        base = _base_revision(arguments.base_ref)
        register = _load_json(ROOT / REGISTER_PATH)
        findings = validate_register(register)
        base_register = _load_base_register(base)
        policy.validate_transitions(base_register, register, ROOT, validate_register)
        reasons = [
            *policy.freeze_reasons(findings, now),
            *policy.protected_capability_reasons(findings, _changed_paths(base)),
        ]
        validate_scope(
            base,
            base_register,
            findings,
            register["runnable_surface_paths"],
            register["runnable_blockers"],
            reasons,
            policy.transition_receipt_paths(register)
            - policy.transition_receipt_paths(base_register or {}),
        )
    except (ValidationError, policy.PolicyError, subprocess.CalledProcessError) as exc:
        print(f"AUDIT REGISTER FAIL: {exc}", file=sys.stderr)
        return 1
    if reasons:
        print("AUDIT FREEZE ACTIVE: " + "; ".join(reasons))
        print("RECOVERY SCOPE PASS")
    else:
        print("AUDIT REGISTER PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
