"""Cross-revision audit-register and freeze policy checks."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

from audit_evidence import EvidenceError, verify_evidence


class PolicyError(ValueError):
    """A cross-revision audit policy violation."""


def validate_path_extensions(
    register: dict[str, Any],
    validate_paths: Callable[[object, str], list[str]],
    nonempty: Callable[[object, str], str],
) -> None:
    findings = register["findings"]
    extensions = register.get("affected_path_extensions", [])
    if not isinstance(extensions, list):
        raise PolicyError("affected_path_extensions must be an array")
    paths_by_finding = {finding["id"]: set(finding["affected_paths"]) for finding in findings}
    for index, extension in enumerate(extensions):
        field = f"affected_path_extensions[{index}]"
        if not isinstance(extension, dict) or set(extension) != {"finding_id", "paths", "rationale"}:
            raise PolicyError(f"{field} has wrong shape")
        finding_id = nonempty(extension["finding_id"], f"{field}.finding_id")
        if finding_id not in paths_by_finding:
            raise PolicyError(f"{field} cites unknown finding: {finding_id}")
        paths = validate_paths(extension["paths"], f"{field}.paths")
        overlap = sorted(paths_by_finding[finding_id] & set(paths))
        if overlap:
            raise PolicyError(f"{field}.paths duplicate existing scope: {overlap}")
        paths_by_finding[finding_id].update(paths)
        nonempty(extension["rationale"], f"{field}.rationale")


def validate_register_keys(register: dict[str, Any]) -> None:
    required = {
        "schema_version", "findings", "audit_runs", "runnable_surface_paths",
        "runnable_blockers", "capability_protections",
    }
    allowed = required | {"affected_path_extensions", "gate_repair_history"}
    actual = set(register)
    if not required.issubset(actual) or not actual.issubset(allowed):
        raise PolicyError(
            f"register has wrong keys; missing={sorted(required - actual)}, "
            f"extra={sorted(actual - allowed)}"
        )


def extension_digest(extensions: list[dict[str, Any]]) -> str:
    raw = json.dumps(extensions, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def added_extension_digest(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> str | None:
    return extension_digest(new[len(old) :]) if old != new else None


def validate_gate_repair_history(register: dict[str, Any], root: Path) -> None:
    history = register.get("gate_repair_history", [])
    if not isinstance(history, list):
        raise PolicyError("gate_repair_history must be an array")
    for index, record in enumerate(history):
        field = f"gate_repair_history[{index}]"
        if not isinstance(record, dict) or set(record) != {
            "base_sha", "extensions_sha256", "review_receipts",
        }:
            raise PolicyError(f"{field} has wrong shape")
        for name in ("base_sha", "extensions_sha256"):
            value = record[name]
            if not isinstance(value, str) or len(value) != 64 and name == "extensions_sha256":
                raise PolicyError(f"{field}.{name} is invalid")
        if len(record["base_sha"]) != 40 or any(c not in "0123456789abcdef" for c in record["base_sha"]):
            raise PolicyError(f"{field}.base_sha is invalid")
        digest = record["extensions_sha256"]
        if any(c not in "0123456789abcdef" for c in digest):
            raise PolicyError(f"{field}.extensions_sha256 is invalid")
        if not isinstance(record["review_receipts"], list):
            raise PolicyError(f"{field}.review_receipts must be an array")
        receipts = record["review_receipts"]
        if len(receipts) != 2:
            raise PolicyError(f"{field} requires exactly two reviews")
        subject = f"gate-repair:{record['base_sha']}:{record['extensions_sha256']}"
        evidence = [
            _evidence(
                receipt,
                f"{field}.review_receipts[{receipt_index}]",
                root,
                role="gate_repair_review",
                subject=subject,
            )
            for receipt_index, receipt in enumerate(receipts)
        ]
        identities = {(item["reference"], item["sha256"]) for item in evidence}
        producers = {item["producer"] for item in evidence}
        if len(identities) != 2 or len(producers) != 2:
            raise PolicyError(f"{field} requires two independent reviews")


def affected_paths(register: dict[str, Any], finding_ids: list[str]) -> list[str]:
    selected = set(finding_ids)
    paths = {
        path
        for finding in register["findings"]
        if finding["id"] in selected
        for path in finding["affected_paths"]
    }
    paths.update(
        path
        for extension in register.get("affected_path_extensions", [])
        if extension["finding_id"] in selected
        for path in extension["paths"]
    )
    return sorted(paths)


def base_revision(root: Path, argument: str | None) -> str:
    requested = argument or os.environ.get("QUALITY_BASE_REF")
    if requested:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{requested}^{{commit}}"],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if result.returncode:
            raise PolicyError(f"base ref is not a commit: {requested}")
        return result.stdout.strip()
    for candidate in ("main", "origin/main"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{candidate}^{{commit}}"],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if result.stdout.strip():
            return result.stdout.strip()
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def changed_paths(root: Path, base: str) -> set[str]:
    output = subprocess.check_output(["git", "diff", "--name-only", base, "--"], cwd=root, text=True)
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=root, text=True
    )
    return {line for line in (*output.splitlines(), *untracked.splitlines()) if line and line != "node_modules"}


def transition_receipt_paths(value: object) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        reference = value.get("reference")
        if isinstance(reference, str) and reference.startswith("audit/receipts/"):
            paths.add(reference)
        for nested in value.values():
            paths.update(transition_receipt_paths(nested))
    elif isinstance(value, list):
        for nested in value:
            paths.update(transition_receipt_paths(nested))
    return paths


def validate_gate_repair(
    scope: dict[str, Any], base: str, root: Path, extension_sha256: str | None = None
) -> None:
    receipts = scope["gate_repair_receipts"]
    if not isinstance(receipts, list):
        raise PolicyError("change scope gate_repair_receipts must be an array")
    if scope["kind"] != "gate_repair":
        if receipts:
            raise PolicyError("gate_repair_receipts are only valid for gate_repair")
        return
    if scope["finding_ids"] or scope["runnable_blocker_id"] is not None or len(receipts) != 2:
        raise PolicyError("gate_repair requires exactly two reviews and no finding or blocker")
    subject = f"gate-repair:{base}"
    if extension_sha256 is not None:
        subject += f":{extension_sha256}"
    evidence = [
        _evidence(
            receipt,
            f"change scope gate_repair_receipts[{index}]",
            root,
            role="gate_repair_review",
            subject=subject,
        )
        for index, receipt in enumerate(receipts)
    ]
    identities = {(item["reference"], item["sha256"]) for item in evidence}
    producers = {item["producer"] for item in evidence}
    if len(identities) != 2 or len(producers) != 2:
        raise PolicyError("gate_repair requires two independent reviews")


def reserve_gate_authority(scope: dict[str, Any], base_exists: bool, changed: bool) -> None:
    if changed and scope["kind"] != "gate_repair" and not (
        scope["kind"] == "registration" and not base_exists
    ):
        raise PolicyError("gate authority paths require a two-review gate_repair")


def _evidence(
    value: object,
    field: str,
    root: Path,
    *,
    role: str,
    subject: str,
) -> dict[str, Any]:
    try:
        return verify_evidence(
            value,
            field,
            root,
            durable=True,
            expected_role=role,
            expected_subject=subject,
        )
    except EvidenceError as exc:
        raise PolicyError(str(exc)) from exc


def validate_transitions(
    base: dict[str, Any] | None,
    current_register: dict[str, Any],
    root: Path,
    validate_register: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    if base is None:
        return
    old_findings = {finding["id"]: finding for finding in validate_register(base)}
    current = current_register["findings"]
    new_findings = {finding["id"]: finding for finding in current}
    removed = sorted(set(old_findings) - set(new_findings))
    if removed:
        raise PolicyError(f"findings cannot be removed: {removed}")
    immutable = {
        "id",
        "title",
        "category",
        "affected_capability",
        "affected_paths",
        "discovered_at",
        "discovery_sha",
        "owner",
        "successor",
        "source_audit",
        "discovery_evidence",
    }
    transitions = {
        "OPEN": {"OPEN", "CONTAINED", "FIXED_AWAITING_VERIFICATION"},
        "CONTAINED": {"CONTAINED", "FIXED_AWAITING_VERIFICATION"},
        "FIXED_AWAITING_VERIFICATION": {
            "FIXED_AWAITING_VERIFICATION",
            "CLOSED",
            "OPEN",
            "CONTAINED",
        },
        "CLOSED": {"CLOSED"},
    }
    for finding_id, old in old_findings.items():
        new = new_findings[finding_id]
        for name in immutable:
            if old[name] != new[name]:
                raise PolicyError(f"{finding_id}.{name} is immutable")
        if new["state"] not in transitions[old["state"]]:
            raise PolicyError(f"illegal state transition for {finding_id}: {old['state']} -> {new['state']}")
        if old["state"] == new["state"] == "OPEN" and old["reachability"] != new["reachability"]:
            raise PolicyError(f"{finding_id}.reachability cannot change while OPEN")
        old_receipts = old["severity_change_receipts"]
        new_receipts = new["severity_change_receipts"]
        if new_receipts[: len(old_receipts)] != old_receipts:
            raise PolicyError(f"{finding_id}.severity_change_receipts is not append-only")
        if old["severity"] == new["severity"]:
            if new_receipts != old_receipts:
                raise PolicyError(f"{finding_id} cannot add severity receipts without a severity change")
            if old["severity_rationale"] != new["severity_rationale"]:
                raise PolicyError(f"{finding_id}.severity_rationale changed without severity review")
        else:
            added = new_receipts[len(old_receipts) :]
            if len(added) != 2:
                raise PolicyError(f"severity change for {finding_id} requires exactly two new receipts")
            subject = f"{finding_id}:{old['severity']}->{new['severity']}"
            identities = []
            producers = set()
            for index, receipt in enumerate(added):
                evidence = _evidence(
                    receipt,
                    f"{finding_id}.severity_change_receipts[{len(old_receipts) + index}]",
                    root,
                    role="severity_review",
                    subject=subject,
                )
                identities.append((evidence["reference"], evidence["sha256"]))
                producers.add(evidence["producer"])
            if len(set(identities)) != 2 or len(producers) != 2:
                raise PolicyError(f"severity change for {finding_id} requires two independent receipts")
        if old["deadline"] != new["deadline"]:
            if not (old["severity"] == "MEDIUM" and new["severity"] == "HIGH"):
                raise PolicyError(f"{finding_id}.deadline is immutable")
            if _parse_time(new["deadline"]) >= _parse_time(old["deadline"]):
                raise PolicyError(f"{finding_id}.deadline escalation must shorten the deadline")
    for finding_id in sorted(set(new_findings) - set(old_findings)):
        _evidence(
            new_findings[finding_id]["discovery_evidence"],
            f"{finding_id}.discovery_evidence",
            root,
            role="discovery",
            subject=finding_id,
        )
    old_runs = base["audit_runs"]
    new_runs = current_register["audit_runs"]
    if new_runs[: len(old_runs)] != old_runs:
        raise PolicyError("audit_runs must be append-only")
    if current_register["runnable_surface_paths"] != base["runnable_surface_paths"]:
        raise PolicyError("runnable_surface_paths is immutable")
    old_extensions = base.get("affected_path_extensions", [])
    new_extensions = current_register.get("affected_path_extensions", [])
    if new_extensions[: len(old_extensions)] != old_extensions:
        raise PolicyError("affected_path_extensions must be append-only")
    old_history = base.get("gate_repair_history", [])
    new_history = current_register.get("gate_repair_history", [])
    if new_history[: len(old_history)] != old_history:
        raise PolicyError("gate_repair_history must be append-only")
    added_extensions = new_extensions[len(old_extensions) :]
    added_history = new_history[len(old_history) :]
    if added_extensions:
        if len(added_history) != 1:
            raise PolicyError("new affected path extensions require one durable gate repair record")
        record = added_history[0]
        if record["extensions_sha256"] != extension_digest(added_extensions):
            raise PolicyError("gate repair history does not bind the appended extensions")
    elif added_history:
        raise PolicyError("gate_repair_history cannot grow without path extensions")
    old_protections = {item["capability"]: item for item in base["capability_protections"]}
    new_protections = {
        item["capability"]: item for item in current_register["capability_protections"]
    }
    missing_protections = sorted(set(old_protections) - set(new_protections))
    if missing_protections:
        raise PolicyError(f"capability protections cannot be removed: {missing_protections}")
    for capability, old in old_protections.items():
        new = new_protections[capability]
        for field in ("capability", "paths", "disabled_assertion"):
            if old[field] != new[field]:
                raise PolicyError(f"capability protection {capability}.{field} is immutable")
        if old["state"] == "DISABLED" and new["state"] == "ENABLED":
            unresolved = [
                finding["id"]
                for finding in current
                if finding["affected_capability"] == capability
                and finding["severity"] == "CRITICAL"
                and finding["state"] != "CLOSED"
            ]
            if unresolved:
                raise PolicyError(f"capability {capability} cannot re-enable before closure: {unresolved}")
    old_blockers = {item["id"]: item for item in base["runnable_blockers"]}
    new_blockers = {item["id"]: item for item in current_register["runnable_blockers"]}
    missing_blockers = sorted(set(old_blockers) - set(new_blockers))
    if missing_blockers:
        raise PolicyError(f"runnable blockers cannot be removed: {missing_blockers}")
    for blocker_id, old in old_blockers.items():
        new = new_blockers[blocker_id]
        for field in ("id", "title", "affected_paths", "owner", "source_evidence"):
            if old[field] != new[field]:
                raise PolicyError(f"runnable blocker {blocker_id}.{field} is immutable")
        if old["status"] == "CLOSED" and new["status"] != "CLOSED":
            raise PolicyError(f"runnable blocker {blocker_id} cannot reopen")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def validate_gate_register_repair(
    base: dict[str, Any] | None,
    current: dict[str, Any],
) -> None:
    if base is None:
        raise PolicyError("gate_repair cannot bootstrap the audit register")
    base_without_extensions = dict(base)
    current_without_extensions = dict(current)
    base_without_extensions.pop("affected_path_extensions", None)
    current_without_extensions.pop("affected_path_extensions", None)
    base_without_extensions.pop("gate_repair_history", None)
    current_without_extensions.pop("gate_repair_history", None)
    if base_without_extensions != current_without_extensions:
        raise PolicyError("gate_repair may only append affected_path_extensions in the register")
    old_extensions = base.get("affected_path_extensions", [])
    new_extensions = current.get("affected_path_extensions", [])
    if len(new_extensions) <= len(old_extensions):
        raise PolicyError("gate_repair register change must append an affected path extension")


def validate_extension_gate_link(
    base_sha: str,
    base: dict[str, Any],
    current: dict[str, Any],
    scope: dict[str, Any],
) -> None:
    old_history = base.get("gate_repair_history", [])
    added_history = current.get("gate_repair_history", [])[len(old_history) :]
    if len(added_history) != 1 or added_history[0]["base_sha"] != base_sha:
        raise PolicyError("path extensions require one exact-base gate repair history record")
    if added_history[0]["review_receipts"] != scope["gate_repair_receipts"]:
        raise PolicyError("gate repair history reviews must match change scope")


def freeze_reasons(findings: list[dict[str, Any]], now: datetime) -> list[str]:
    reasons = []
    open_high = 0
    for finding in findings:
        if finding["state"] == "CLOSED":
            continue
        if finding["severity"] == "CRITICAL" and finding["reachability"] == "REACHABLE":
            reasons.append(f"reachable Critical {finding['id']}")
        if finding["severity"] == "HIGH":
            open_high += 1
            if _parse_time(finding["deadline"]) < now:
                reasons.append(f"overdue High {finding['id']}")
    if open_high > 2:
        reasons.append(f"High WIP is {open_high}, maximum is 2")
    missing_receipts = sum(
        finding["discovery_evidence"]["sha256"] is None
        for finding in findings
        if finding["state"] != "CLOSED"
    )
    if missing_receipts:
        reasons.append(f"{missing_receipts} findings lack immutable discovery receipts")
    return reasons


def protected_capability_reasons(
    findings: list[dict[str, Any]], changed: set[str]
) -> list[str]:
    reasons = []
    for finding in findings:
        if (
            finding["severity"] == "CRITICAL"
            and finding["state"] in {"CONTAINED", "FIXED_AWAITING_VERIFICATION"}
            and any(path in finding["affected_paths"] for path in changed)
        ):
            reasons.append(f"protected Critical capability {finding['id']} changed before closure")
    return reasons
