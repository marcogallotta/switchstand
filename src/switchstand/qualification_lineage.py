import hashlib
import json
import math
from datetime import datetime
from itertools import pairwise
from typing import Any, cast

RETRY_REASONS = {
    "CANDIDATE_CHANGE", "SCOPE_NARROWING", "ENVIRONMENT_REPAIR", "HARNESS_WRAPPER_REPAIR",
    "FIXTURE_ORACLE_REPAIR", "PROVIDER_RETRY", "EVIDENCE_GATHERING", "UNKNOWN"}
SCOPE_FIELDS = ("candidate_sha", "base_sha", "plan_digest", "harness_digest", "environment_digest")
REWORK_FIELDS = ("harness_seconds", "wrapper_seconds", "fixture_seconds", "setup_seconds")

def _stable_id(kind: str, value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"{kind}-{hashlib.sha256(data).hexdigest()}"

def _dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}

def _provider_id(provider: dict[str, Any]) -> str:
    values = (provider["repository"], provider["workflow"], provider["job"],
              int(provider["run_id"]), int(provider["run_attempt"]))
    if not all(isinstance(value, str) and value for value in values[:3]) or min(values[3:]) <= 0:
        raise ValueError("invalid provider identity")
    return _stable_id("qa", values)

def qualification_attempt_observation(
    identity: dict[str, Any], planner: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project existing provider and test evidence without creating authority."""
    github = _dict(identity.get("github"))
    rework = _dict(identity.get("qualification_rework"))
    environment = _dict(identity.get("environment"))
    failure = _dict(identity.get("failure_classification"))
    try:
        provider: dict[str, Any] = {
            "repository": github["repository"], "workflow": github["workflow"],
            "job": github["job"], "run_id": int(github["run_id"]),
            "run_attempt": int(github["run_attempt"]),
        }
        concern = identity["qualification_subject"]
        if not isinstance(concern, str) or not concern:
            raise ValueError("empty qualification subject")
        provider["executed_attempt_id"] = _provider_id(provider)
        lineage = _stable_id("ql", (provider["repository"], concern, "qualification-lineage-v1"))
    except (KeyError, TypeError, ValueError):
        provider, lineage = dict(github), None
    started, completed = identity.get("started_at"), identity.get("completed_at")
    foreground = None
    if identity.get("foreground") is False:
        foreground = 0.0
    elif identity.get("foreground") is True:
        try:
            foreground = (datetime.fromisoformat(cast(str, completed))
                          - datetime.fromisoformat(cast(str, started))).total_seconds()
            foreground = foreground if foreground >= 0 else None
        except (TypeError, ValueError):
            pass
    harness = {"workflow_blob": environment.get("workflow_blob"),
               "manifest_blobs": environment.get("manifest_blobs"),
               "candidate_image": identity.get("candidate_image")}
    outcome = {"success": "SUCCESS", "failure": "FAILURE"}.get(
        cast(str, identity.get("quality_outcome")), "UNKNOWN")
    return {
        "lineage_id": lineage, "provider": provider,
        "scope": {"candidate_sha": identity.get("subject_sha"),
                  "base_sha": identity.get("base_sha"),
                  "plan_digest": _stable_id("plan", planner) if planner is not None else None,
                  "harness_digest": _stable_id("harness", harness) if all(harness.values()) else None,
                  "environment_digest": identity.get("environment_key")},
        "retry_reason": rework.get("retry_reason", "PROVIDER_RETRY" if str(provider.get("run_attempt", "")).isdigit() and int(provider["run_attempt"]) > 1 else "UNKNOWN"),
        "outcome": outcome, "started_at": started, "completed_at": completed,
        "foreground_seconds": foreground,
        "decisive_failure_at": failure.get("decisive_failure_at"),
        "rework": {key: rework.get(key) for key in REWORK_FIELDS},
        "current": identity.get("current"),
    }

def _unknown(lineage: object, reason: str) -> dict[str, Any]:
    return {"status": "UNKNOWN", "lineage_id": lineage, "reason": reason}

def summarize_lineage(attempts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Deduplicate executed attempts and derive fail-closed cumulative observations."""
    if not attempts:
        raise ValueError("at least one attempt is required")
    lineage = attempts[0].get("lineage_id")
    if not lineage:
        return _unknown(lineage, "evidence-missing")
    if any(item.get("lineage_id") != lineage for item in attempts):
        return _unknown(lineage, "lineage-conflict")
    unique: dict[str, dict[str, Any]] = {}
    try:
        for item in attempts:
            provider = item["provider"]
            attempt_id = _provider_id(provider)
            if provider.setdefault("executed_attempt_id", attempt_id) != attempt_id:
                raise ValueError
            if attempt_id in unique and unique[attempt_id] != item:
                return _unknown(lineage, "attempt-conflict")
            unique[attempt_id] = item
    except (KeyError, TypeError, ValueError):
        return _unknown(lineage, "evidence-missing")
    observed = list(unique.values())
    if any(item.get("current") is not True for item in observed):
        return _unknown(lineage, "currentness-conflict")
    if any(item.get("retry_reason") not in RETRY_REASONS or item.get("outcome") not in {"SUCCESS", "FAILURE"} for item in observed):
        return _unknown(lineage, "evidence-missing")
    if any(item["retry_reason"] == "UNKNOWN" for item in observed):
        return _unknown(lineage, "retry-reason-unknown")
    if any((item["outcome"] == "FAILURE") != bool(item.get("decisive_failure_at")) for item in observed):
        return _unknown(lineage, "failure-classification-unknown")
    try:
        for item in observed:
            item["_scope"] = tuple(item["scope"][key] for key in SCOPE_FIELDS)
            if not all(item["_scope"]):
                raise ValueError
            item["_start"] = datetime.fromisoformat(item["started_at"])
            item["_end"] = datetime.fromisoformat(item["completed_at"])
            if any(value.utcoffset() is None for value in (item["_start"], item["_end"])) or item["_end"] < item["_start"]:
                raise ValueError
            numbers = [item["foreground_seconds"], *(item["rework"][key] for key in REWORK_FIELDS)]
            if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in numbers):
                raise ValueError
            item["_failure"] = datetime.fromisoformat(item["decisive_failure_at"]) if item.get("decisive_failure_at") else None
            if item["_failure"] and not item["_start"] <= item["_failure"] <= item["_end"]:
                return _unknown(lineage, "failure-time-conflict")
    except (KeyError, TypeError, ValueError):
        return _unknown(lineage, "evidence-missing")
    observed.sort(key=lambda item: (item["_start"], item["provider"]["executed_attempt_id"]))
    rework = {key: sum(item["rework"][key] for item in observed) for key in REWORK_FIELDS}
    failures = sorted(item["_failure"] for item in observed if item["_failure"])
    first_failure = failures[0] if failures else None
    return {
        "status": "AVAILABLE", "lineage_id": lineage, "attempt_count": len(observed),
        "cumulative_wall_seconds": sum((item["_end"] - item["_start"]).total_seconds() for item in observed),
        "cumulative_foreground_seconds": sum(item["foreground_seconds"] for item in observed),
        "repeated_scope_retry_count": sum(left["_scope"] == right["_scope"] for left, right in pairwise(observed)),
        "rework_seconds": sum(rework.values()), **rework,
        "time_to_first_decisive_failure_seconds": (first_failure - observed[0]["_start"]).total_seconds() if first_failure else None,
        "time_after_decisive_failure_seconds": (max(item["_end"] for item in observed) - first_failure).total_seconds() if first_failure else None,
    }

def advisory_warning(metrics: dict[str, Any], *, repeated_scope_threshold: int,
                     rework_seconds_threshold: float,
                     emitted_warning_ids: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Return one inert request; a future attention adapter owns delivery."""
    if repeated_scope_threshold < 0 or not math.isfinite(rework_seconds_threshold) or rework_seconds_threshold < 0:
        raise ValueError("invalid advisory threshold")
    warning_id = None if metrics.get("status") != "AVAILABLE" else _stable_id(
        "qw", (metrics["lineage_id"], "CI_QUALIFICATION_REWORK_WARNING")
    )
    if warning_id is None:
        disposition = "UNKNOWN"
    elif warning_id in emitted_warning_ids:
        disposition = "DEDUPED"
    elif (metrics["repeated_scope_retry_count"] >= repeated_scope_threshold
          or metrics["rework_seconds"] >= rework_seconds_threshold):
        disposition = "EMIT"
    else:
        disposition = "NOT_DUE"
    return {"disposition": disposition, "warning_id": warning_id,
            "attention_kind": "CI_QUALIFICATION_REWORK_WARNING", "route": "tests-ci-to-wakeful-attention"}
