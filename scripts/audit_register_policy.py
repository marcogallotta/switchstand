"""Cross-revision audit-register and freeze policy checks."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from audit_evidence import EvidenceError, verify_evidence


class PolicyError(ValueError):
    """A cross-revision audit policy violation."""


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


def validate_gate_repair(scope: dict[str, Any], base: str, root: Path) -> None:
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
